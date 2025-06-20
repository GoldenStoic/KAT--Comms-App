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
# CONFIGURATION
# -----------------------------------------------------------------------------
JWT_SECRET  = os.getenv("JWT_SECRET", "change-this-for-production")
ICE_SERVERS = [
    {"urls": "stun:stun.l.google.com:19302"},
    {"urls": "stun:global.stun.twilio.com:3478"}
]

# -----------------------------------------------------------------------------
# APPLICATION SETUP
# -----------------------------------------------------------------------------
app   = FastAPI()
relay = MediaRelay()
# room state holds waiting sockets, admitted peers →PC, relayed tracks, admin sockets
rooms = {}  # room_id -> { "waiting":set, "peers":dict, "tracks":list, "admins":set }

# serve index.html and static/
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def index():
    return FileResponse("static/index.html")

# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------
async def authenticate(token: str) -> str:
    """Decode JWT and return 'admin' or 'user'."""
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except Exception:
        return "user"

async def admit(ws: WebSocket, room: dict):
    """
    Remove `ws` from waiting, notify client, create a PeerConnection,
    wire up audio tracks (SFU-style) and request an offer.
    """
    room["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    # build real RTCConfiguration
    ice_servers = [RTCIceServer(**s) for s in ICE_SERVERS]
    config      = RTCConfiguration(iceServers=ice_servers)

    pc = RTCPeerConnection(configuration=config)
    room["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            relay_track = relay.subscribe(track)
            room["tracks"].append(relay_track)
            # forward to every other peer
            for other in room["peers"].values():
                if other is not pc:
                    other.addTrack(relay_track)

    # forward any already-collected audio
    for t in room["tracks"]:
        pc.addTrack(t)

    # tell client to createOffer()
    await ws.send_json({"type": "ready_for_offer"})

# -----------------------------------------------------------------------------
# WEBSOCKET / SIGNALING
# -----------------------------------------------------------------------------
@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str,
                      token: str = Query(...)):
    await ws.accept()
    role = await authenticate(token)

    # create or grab our room state
    room = rooms.setdefault(room_id, {
        "waiting": set(),
        "peers":   {},
        "tracks":  [],
        "admins":  set()
    })

    if role == "admin":
        # auto-admit admin
        room["admins"].add(ws)
        await admit(ws, room)
        # let new admin know who’s waiting
        for w in room["waiting"]:
            await ws.send_json({"type": "new_waiting", "peer_id": id(w)})
    else:
        # normal user → waiting list
        room["waiting"].add(ws)
        await ws.send_json({"type": "waiting"})
        # notify every admin
        for a in room["admins"]:
            await a.send_json({"type": "new_waiting", "peer_id": id(ws)})
        # block until admin calls admit()
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
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await ws.send_json({
                    "type": "answer",
                    "sdp":  pc.localDescription.sdp
                })

            elif t == "ice":
                raw = msg.get("candidate")
                if raw:
                    # wrap into an object that aiortc expects:
                    cand = SimpleNamespace(
                        sdpMid        = raw.get("sdpMid"),
                        sdpMLineIndex = raw.get("sdpMLineIndex"),
                        candidate     = raw.get("candidate"),
                        component     = 1         # required by aiortc’s candidate_to_aioice()
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
                    await admit(pend, room)

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
