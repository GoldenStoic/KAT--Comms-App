import os, sys, jwt, asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.contrib.media import MediaRelay
from aiortc.sdp import candidate_from_sdp  # <— the big win

JWT_SECRET = os.environ.get("JWT_SECRET", "change-this")
ICE_SERVERS = [
    {"urls": "stun:stun.l.google.com:19302"},
    {"urls": "stun:global.stun.twilio.com:3478"},
]

rooms = {}  # room_id -> {admins, waiting, peers, audio_tracks}
relay = MediaRelay()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

async def authenticate(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload.get("role", "user")
    except:
        return "user"

async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    pc = RTCPeerConnection({"iceServers": ICE_SERVERS})
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            r = relay.subscribe(track)
            state["audio_tracks"].append(r)
            for other in state["peers"].values():
                if other is not pc:
                    other.addTrack(r)

    for t in state["audio_tracks"]:
        pc.addTrack(t)

    await ws.send_json({"type": "ready_for_offer"})

@app.get("/")
async def index():
    return FileResponse("static/index.html")

@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role = await authenticate(ws.query_params.get("token", ""))
    state = rooms.setdefault(room_id, {
        "admins": set(), "waiting": set(), "peers": {}, "audio_tracks": []
    })

    if role == "admin":
        state["admins"].add(ws)
        await _admit(ws, room_id)
        for w in list(state["waiting"]):
            await ws.send_json({"type": "new_waiting", "peer_id": id(w)})
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type": "waiting"})
        for a in state["admins"]:
            await a.send_json({"type": "new_waiting", "peer_id": id(ws)})
        # pause here until admin admits
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    try:
        while True:
            msg = await ws.receive_json()
            typ = msg["type"]

            if typ == "offer":
                pc = state["peers"][ws]
                offer = RTCSessionDescription(sdp=msg["sdp"], type="offer")
                await pc.setRemoteDescription(offer)
                ans = await pc.createAnswer()
                await pc.setLocalDescription(ans)
                await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})

            elif typ == "ice":
                pc = state["peers"][ws]
                raw = msg["candidate"]
                # parse the SDP string into a real aiortc candidate
                c = candidate_from_sdp(raw["candidate"])
                c.sdpMid = raw["sdpMid"]
                c.sdpMLineIndex = raw["sdpMLineIndex"]
                await pc.addIceCandidate(c)

            elif typ == "chat":
                for p in state["peers"]:
                    await p.send_json({"type":"chat","from":msg["from"],"text":msg["text"]})

            elif typ == "admit" and ws in state["admins"]:
                target = next((w for w in state["waiting"] if id(w)==msg["peer_id"]), None)
                if target:
                    await _admit(target, room_id)

            elif typ == "material_event" and ws in state["admins"]:
                for p in state["peers"]:
                    await p.send_json({
                        "type":"material_event","event":msg["event"],
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
