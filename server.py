import os
import asyncio
import jwt
from types import SimpleNamespace
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

# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------
JWT_SECRET  = os.getenv("JWT_SECRET", "change-this-for-prod")
ICE_SERVERS = [
    {"urls": "stun:stun.l.google.com:19302"},
    {"urls": "stun:global.stun.twilio.com:3478"}
]

# -----------------------------------------------------------------------------
# APP SETUP
# -----------------------------------------------------------------------------
app   = FastAPI()
relay = MediaRelay()
rooms = {}  # room_id -> { waiting: set, peers: dict, tracks: list, admins: set }

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def index():
    return FileResponse("static/index.html")

# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------
async def authenticate(token: str) -> str:
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except Exception:
        return "user"

async def do_admit(ws: WebSocket, room: dict):
    """Remove from waiting, notify client, build PeerConnection, wire tracks."""
    room["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    # build a real RTCConfiguration
    ice_servers = [RTCIceServer(**s) for s in ICE_SERVERS]
    config      = RTCConfiguration(iceServers=ice_servers)

    pc = RTCPeerConnection(configuration=config)
    room["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            sub = relay.subscribe(track)
            room["tracks"].append(sub)
            # forward this track to all existing peers
            for other in room["peers"].values():
                if other is not pc:
                    other.addTrack(sub)

    # forward any already-collected tracks
    for t in room["tracks"]:
        pc.addTrack(t)

    # tell client we’re ready for its offer
    await ws.send_json({"type": "ready_for_offer"})

# -----------------------------------------------------------------------------
# WEBSOCKET ENDPOINT
# -----------------------------------------------------------------------------
@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str,
                      token: str = Query(...)):
    await ws.accept()
    role = await authenticate(token)

    # init or fetch room state
    room = rooms.setdefault(room_id, {
        "waiting": set(),
        "peers":   {},
        "tracks":  [],
        "admins":  set()
    })

    if role == "admin":
        room["admins"].add(ws)
        await do_admit(ws, room)
        # let the new admin know who’s waiting
        for w in room["waiting"]:
            await ws.send_json({"type": "new_waiting", "peer_id": id(w)})
    else:
        room["waiting"].add(ws)
        await ws.send_json({"type": "waiting"})
        # notify all admins
        for a in room["admins"]:
            await a.send_json({"type": "new_waiting", "peer_id": id(ws)})
        # block until an admin calls do_admit()
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
                await ws.send_json({
                    "type": "answer",
                    "sdp":  pc.localDescription.sdp
                })

            elif t == "ice":
                # HERE’S THE FIX: wrap the dict in an object
                raw = msg["candidate"]
                if raw:
                    cand = SimpleNamespace(
                        sdpMid        = raw.get("sdpMid"),
                        sdpMLineIndex = raw.get("sdpMLineIndex"),
                        candidate     = raw.get("candidate")
                    )
                    pc = room["peers"][ws]
                    await pc.addIceCandidate(cand)

            elif t == "chat":
                for p in room["peers"]:
                    await p.send_json({
                        "type": "chat",
                        "from": msg["from"],
                        "text": msg["text"]
                    })

            elif t == "admit" and ws in room["admins"]:
                target = msg["peer_id"]
                pend   = next((w for w in room["waiting"]
                               if id(w) == target), None)
                if pend:
                    await do_admit(pend, room)

            elif t == "material_event" and ws in room["admins"]:
                for p in room["peers"]:
                    await p.send_json({
                        "type":    "material_event",
                        "event":   msg["event"],
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
