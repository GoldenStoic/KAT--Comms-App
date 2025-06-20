import os, asyncio, jwt
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCIceServer,
    RTCConfiguration
)
from aiortc.contrib.media import MediaRelay

# Load your JWT secret and port from env
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-for-prod")
PORT       = int(os.getenv("PORT", 8000))

ICE_SERVERS = [
    { "urls": "stun:stun.l.google.com:19302" },
    { "urls": "stun:global.stun.twilio.com:3478" }
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

    # Correctly build RTCConfiguration
    rtc_ice = [RTCIceServer(**s) for s in ICE_SERVERS]
    config  = RTCConfiguration(iceServers=rtc_ice)

    pc = RTCPeerConnection(configuration=config)
    room["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            relay_track = relay.subscribe(track)
            room["tracks"].append(relay_track)
            for other in room["peers"].values():
                if other is not pc:
                    other.addTrack(relay_track)

    # forward existing tracks
    for t in room["tracks"]:
        pc.addTrack(t)

    await ws.send_json({"type": "ready_for_offer"})

@app.websocket("/ws/{room_id}")
async def ws_endpoint(
    ws: WebSocket,
    room_id: str,
    token: str = Query(...)
):
    await ws.accept()
    role = await authenticate(token)
    room = rooms.setdefault(room_id, {
        "waiting": set(),
        "peers":    {},
        "tracks":   [],
        "admins":   set()
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
        # block until admin admits
        while ws not in room["peers"]:
            await asyncio.sleep(0.1)

    try:
        while True:
            msg = await ws.receive_json()
            t   = msg.get("type")

            if t == "offer":
                pc    = room["peers"][ws]
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
                pending = next((w for w in room["waiting"] if id(w)==pid), None)
                if pending:
                    await admit(pending, room)

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
