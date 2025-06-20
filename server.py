import os
import asyncio
import jwt
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRelay

# Load secrets/ports from environment
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-for-prod")
PORT       = int(os.getenv("PORT", 8000))

ICE_SERVERS = [
    {"urls": "stun:stun.l.google.com:19302"},
    {"urls": "stun:global.stun.twilio.com:3478?transport=udp"},
]
relay = MediaRelay()

rooms = {}  # room_id → { waiting, peers, tracks, admins }

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def index():
    return FileResponse("static/index.html")

async def authenticate(token: str) -> str:
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except:
        return "user"

async def admit(ws: WebSocket, room: dict):
    room["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})
    pc = RTCPeerConnection({"iceServers": ICE_SERVERS})
    room["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            a = relay.subscribe(track)
            room["tracks"].append(a)
            for other in room["peers"].values():
                if other is not pc:
                    other.addTrack(a)

    for t in room["tracks"]:
        pc.addTrack(t)

    await ws.send_json({"type": "ready_for_offer"})

@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str, token: str = Query(...)):
    await ws.accept()
    role = await authenticate(token)
    room = rooms.setdefault(room_id, {
        "waiting": set(), "peers": {}, "tracks": [], "admins": set()
    })

    if role == "admin":
        room["admins"].add(ws)
        await admit(ws, room)
        for w in list(room["waiting"]):
            await ws.send_json({"type": "new_waiting", "peer_id": id(w)})
    else:
        room["waiting"].add(ws)
        await ws.send_json({"type": "waiting"})
        for a in room["admins"]:
            await a.send_json({"type": "new_waiting", "peer_id": id(ws)})
        while ws not in room["peers"]:
            await asyncio.sleep(0.1)

    try:
        while True:
            msg = await ws.receive_json()
            t   = msg.get("type")

            if t == "offer":
                pc = room["peers"][ws]
                offer = RTCSessionDescription(sdp=msg["sdp"], type="offer")
                await pc.setRemoteDescription(offer)
                ans = await pc.createAnswer()
                await pc.setLocalDescription(ans)
                await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})

            elif t == "ice":
                pc = room["peers"][ws]
                await pc.addIceCandidate(msg["candidate"])

            elif t == "chat":
                for p in room["peers"]:
                    await p.send_json({
                        "type": "chat",
                        "from": msg["from"],
                        "text": msg["text"]
                    })

            elif t == "admit" and ws in room["admins"]:
                pid = msg["peer_id"]
                target = next((w for w in room["waiting"] if id(w)==pid), None)
                if target:
                    await admit(target, room)

            elif t == "material_event" and ws in room["admins"]:
                for p in room["peers"]:
                    await p.send_json({
                        "type": "material_event",
                        "event": msg["event"],
                        "payload": msg.get("payload", {})
                    })

    except WebSocketDisconnect:
        pass
    finally:
        room["admins"].discard(ws)
        room["waiting"].discard(ws)
        pc = room["peers"].pop(ws, None)
        if pc:
            await pc.close()

# We don’t invoke uvicorn.run() here—Render will run via Procfile.
