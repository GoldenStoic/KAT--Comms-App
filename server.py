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
)
from aiortc.contrib.media import MediaRelay
from aiortc.sdp import candidate_from_sdp  # <<— pull in the SDP helper

# Debug: verify interpreter & PyJWT import path
print(">>> SERVER running under:", sys.executable)
print(">>> jwt module path:", getattr(jwt, "__file__", None))

# --- CONFIG ---
ICE_SERVERS = [
    {"urls": "stun:stun.l.google.com:19302"},
    {"urls": "stun:global.stun.twilio.com:3478"},
]
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")

# In-memory rooms
rooms = {}  # room_id → { "admins": set(ws), "waiting": set(ws), "peers": {ws:pc}, "audio_tracks": [] }
relay = MediaRelay()

app = FastAPI()

# serve index.html
@app.get("/")
async def index():
    return FileResponse("static/index.html")

# JWT-based dummy auth
async def authenticate(token: str):
    try:
        data = jwt.decode(token or "", JWT_SECRET, algorithms=["HS256"])
        print("🛡️ authenticate:", data)
        return data.get("role", "user")
    except Exception as e:
        print("⚠️ authenticate error:", repr(e))
        return "user"

# lift a socket out of the waiting room
async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    # build a real RTCConfiguration
    rtc_ice_servers = [RTCIceServer(**srv) for srv in ICE_SERVERS]
    config = RTCConfiguration(iceServers=rtc_ice_servers)

    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            relay_track = relay.subscribe(track)
            state["audio_tracks"].append(relay_track)
            # forward to everyone else
            for other in state["peers"].values():
                if other is not pc:
                    other.addTrack(relay_track)

    # replay any existing audio streams
    for t in state["audio_tracks"]:
        pc.addTrack(t)

    # tell client ready to negotiate
    await ws.send_json({"type": "ready_for_offer"})


@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    token = ws.query_params.get("token", "")
    role = await authenticate(token)
    print(f"[ws] conn room={room_id} role={role!r}")

    state = rooms.setdefault(room_id, {
        "admins": set(), "waiting": set(),
        "peers": {}, "audio_tracks": []
    })

    if role == "admin":
        # become an admin peer immediately
        state["admins"].add(ws)
        print(f"[ws] admitting admin {id(ws)}")
        await _admit(ws, room_id)
        # send current waiting list
        for pend in state["waiting"]:
            await ws.send_json({"type": "new_waiting", "peer_id": id(pend)})
    else:
        # normal user → waiting
        state["waiting"].add(ws)
        await ws.send_json({"type": "waiting"})
        for a in state["admins"]:
            await a.send_json({"type": "new_waiting", "peer_id": id(ws)})
        print(f"[ws] user waiting {id(ws)}")
        # block until admin calls _admit(ws)
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
                await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})

            elif typ == "ice":
                pc = state["peers"][ws]
                c = msg["candidate"]
                # parse the SDP candidate string
                ice = candidate_from_sdp(c["candidate"])
                # copy over mid/index
                ice.sdpMid = c.get("sdpMid")
                ice.sdpMLineIndex = c.get("sdpMLineIndex")
                await pc.addIceCandidate(ice)

            elif typ == "chat":
                for peer in state["peers"]:
                    await peer.send_json({
                        "type": "chat",
                        "from": msg["from"],
                        "text": msg["text"]
                    })

            elif typ == "admit" and ws in state["admins"]:
                target_id = msg["peer_id"]
                pend = next((w for w in state["waiting"] if id(w) == target_id), None)
                if pend:
                    await _admit(pend, room_id)

            elif typ == "material_event" and ws in state["admins"]:
                for peer in state["peers"]:
                    await peer.send_json({
                        "type": "material_event",
                        "event": msg["event"],
                        "payload": msg.get("payload", {})
                    })

    except WebSocketDisconnect:
        pass
    finally:
        # cleanup
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws, None)
        if pc:
            await pc.close()
        print(f"[ws] closed {id(ws)}")

# static files under /static
app.mount("/static", StaticFiles(directory="static"), name="static")
