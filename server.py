# server.py
import os
import sys
import jwt
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCIceServer,
    RTCConfiguration,
    RTCIceCandidate,
)
from aiortc.contrib.media import MediaRelay

print(">>> SERVER running under:", sys.executable)
print(">>> jwt module path:", getattr(jwt, "__file__", None))

# --- CONFIG from environment ---
STUN_URL  = os.getenv("STUN_URL", "stun:stun.l.google.com:19302")
TURN_URL  = os.getenv("TURN_URL")  # e.g. "turn:your.turn.host:3478?transport=udp"
TURN_USER = os.getenv("TURN_USER")
TURN_PASS = os.getenv("TURN_PASS")
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")

ICE_SERVERS = [{"urls": STUN_URL}]
if TURN_URL and TURN_USER and TURN_PASS:
    ICE_SERVERS.append({
        "urls": TURN_URL,
        "username": TURN_USER,
        "credential": TURN_PASS
    })

# In-memory room state
rooms = {}  # room_id → {admins, waiting, peers, audio_tracks}
relay = MediaRelay()

app = FastAPI()

# --- Helper: decode JWT ---
async def authenticate(token: str):
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        print("🛡️ authenticate:", data)
        return data.get("role", "user")
    except Exception as e:
        print("⚠️ authenticate error:", repr(e))
        return "user"

# --- Admit helper ---
async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    # Build an RTCConfiguration with our STUN/TURN list
    rtc_ice_servers = [RTCIceServer(**s) for s in ICE_SERVERS]
    config = RTCConfiguration(iceServers=rtc_ice_servers)

    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            relay_track = relay.subscribe(track)
            state["audio_tracks"].append(relay_track)
            for other in state["peers"].values():
                if other is not pc:
                    other.addTrack(relay_track)

    # forward any existing audio
    for t in state["audio_tracks"]:
        pc.addTrack(t)

    # tell client we're ready
    await ws.send_json({"type": "ready_for_offer"})

# --- Serve client ---
@app.get("/")
async def index():
    return FileResponse("static/index.html")

@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    token = ws.query_params.get("token", "")
    role  = await authenticate(token)
    print(f"[ws] conn room={room_id} role={role}")

    state = rooms.setdefault(room_id, {
        "admins": set(), "waiting": set(),
        "peers": {}, "audio_tracks": []
    })

    # admin or user path
    if role == "admin":
        state["admins"].add(ws)
        print(f"[ws] admitting admin {id(ws)}")
        await _admit(ws, room_id)
        # notify about any waiting users
        for pending in state["waiting"]:
            await ws.send_json({"type":"new_waiting","peer_id":id(pending)})
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type":"waiting"})
        for admin_ws in state["admins"]:
            await admin_ws.send_json({"type":"new_waiting","peer_id":id(ws)})
        print(f"[ws] user waiting {id(ws)}")
        # block until admitted
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)
        print(f"[ws] user admitted {id(ws)}")

    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            if typ == "offer":
                pc = state["peers"][ws]
                offer = RTCSessionDescription(sdp=msg["sdp"], type="offer")
                await pc.setRemoteDescription(offer)
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await ws.send_json({"type":"answer","sdp":pc.localDescription.sdp})

            elif typ == "ice":
                pc = state["peers"][ws]
                c  = msg["candidate"]
                cand_str = c["candidate"]
                parts    = cand_str.split()

                # proper parsing:
                foundation = parts[0].split(":",1)[1]
                component  = int(parts[1])
                protocol   = parts[2].lower()
                priority   = int(parts[3])
                ip         = parts[4]
                port       = int(parts[5])
                cand_type  = parts[7]

                ice = RTCIceCandidate(
                    foundation=foundation,
                    component=component,
                    protocol=protocol,
                    priority=priority,
                    ip=ip,
                    port=port,
                    type=cand_type,
                    sdpMid=c.get("sdpMid"),
                    sdpMLineIndex=c.get("sdpMLineIndex")
                )
                try:
                    await pc.addIceCandidate(ice)
                except Exception:
                    pass  # transport may already be closed

            elif typ == "chat":
                for peer in state["peers"]:
                    await peer.send_json({
                        "type":"chat", "from":msg["from"], "text":msg["text"]
                    })

            elif typ == "admit" and ws in state["admins"]:
                target = msg["peer_id"]
                pending = next((w for w in state["waiting"] if id(w)==target), None)
                if pending:
                    await _admit(pending, room_id)

            elif typ == "material_event" and ws in state["admins"]:
                for peer in state["peers"]:
                    await peer.send_json({
                        "type":"material_event",
                        "event":msg["event"],
                        "payload":msg.get("payload",{})
                    })

    except WebSocketDisconnect:
        pass

    finally:
        # clean up
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws, None)
        if pc:
            await pc.close()
        print(f"[ws] closed {id(ws)}")

# Serve static files
app.mount("/static", StaticFiles(directory="static"), name="static")
