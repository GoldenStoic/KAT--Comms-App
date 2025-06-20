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

print(">>> SERVER under:", sys.executable)
print(">>> jwt at:", getattr(jwt, "__file__", None))

ICE_SERVERS = [
    {"urls": "stun:stun.l.google.com:19302"},
    {"urls": "stun:global.stun.twilio.com:3478"},
]
JWT_SECRET = "change-this-to-a-strong-secret"

rooms = {}
relay = MediaRelay()
app = FastAPI()

async def authenticate(token: str):
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except Exception:
        return "user"

async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    config = RTCConfiguration(
        iceServers=[RTCIceServer(urls=e["urls"]) for e in ICE_SERVERS]
    )
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

    for t in state["audio_tracks"]:
        pc.addTrack(t)

    await ws.send_json({"type": "ready_for_offer"})

async def _add_ice(pc: RTCPeerConnection, c):
    if c is None:
        await pc.addIceCandidate(None)
        return

    line = c.get("candidate", "")
    parts = line.split()
    foundation     = parts[0].split(":",1)[1]
    component      = int(parts[1])
    protocol       = parts[2].lower()
    priority       = int(parts[3])
    ip             = parts[4]
    port           = int(parts[5])
    typ            = parts[7]
    # tcpType only for TCP candidates:
    tcpType        = None

    candidate = RTCIceCandidate(
        foundation,
        component,
        protocol,
        priority,
        ip,
        port,
        typ,
        tcpType,
        sdpMid=c.get("sdpMid"),
        sdpMLineIndex=c.get("sdpMLineIndex"),
    )
    await pc.addIceCandidate(candidate)

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

    if role == "admin":
        state["admins"].add(ws)
        await _admit(ws, room_id)
        for pending in state["waiting"]:
            await ws.send_json({"type":"new_waiting","peer_id":id(pending)})
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type":"waiting"})
        for a in state["admins"]:
            await a.send_json({"type":"new_waiting","peer_id":id(ws)})
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    pending_ice = []

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
                for c in pending_ice:
                    await _add_ice(pc, c)
                pending_ice.clear()

            elif typ == "ice":
                c  = msg.get("candidate")
                pc = state["peers"].get(ws)
                if pc and pc.remoteDescription:
                    await _add_ice(pc, c)
                else:
                    pending_ice.append(c)

            elif typ == "chat":
                for peer in state["peers"]:
                    await peer.send_json({
                        "type":"chat","from":msg["from"],"text":msg["text"]
                    })

            elif typ == "admit" and ws in state["admins"]:
                target  = msg["peer_id"]
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
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws, None)
        if pc:
            await pc.close()

app.mount("/static", StaticFiles(directory="static"), name="static")
