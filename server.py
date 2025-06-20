import os
import asyncio
import jwt
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCIceCandidate,
    RTCIceServer,
    RTCConfiguration,
)
from aiortc.contrib.media import MediaRelay

# ——— CONFIG —————————————————————————————————————————————————————————————
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")

# your STUN/TURN servers
ICE_SERVERS = [
    RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
    RTCIceServer(urls=["stun:global.stun.twilio.com:3478"])
]
RTC_CONFIGURATION = RTCConfiguration(iceServers=ICE_SERVERS)

# in-memory room state
rooms = {}  # room_id → { admins, waiting, peers, audio_tracks }
relay = MediaRelay()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


# ——— HELPERS —————————————————————————————————————————————————————————————
async def authenticate(token: str):
    """
    Decode a JWT {"role":"admin"|"user"}.
    """
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except Exception:
        return "user"


async def admit(ws: WebSocket, room_id: str):
    """
    Promote a waiting socket into the SFU:
    1) remove from waiting
    2) notify client with {"type":"admitted"}
    3) create RTCPeerConnection + relay tracks
    4) tell client to send us its offer
    """
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    pc = RTCPeerConnection(configuration=RTC_CONFIGURATION)
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

    # forward existing audio into the newcomer
    for t in state["audio_tracks"]:
        pc.addTrack(t)

    # now ask the client to send an offer
    await ws.send_json({"type": "ready_for_offer"})


# ——— HTTP ENDPOINT ——————————————————————————————————————————————————————
@app.get("/")
async def index():
    return FileResponse("static/index.html")


# ——— WebSocket ENDPOINT ————————————————————————————————————————————————————
@app.websocket("/ws/{room_id}")
async def ws_endpoint(
    ws: WebSocket,
    room_id: str,
    token: str = Query(...)
):
    role = await authenticate(token)
    await ws.accept()

    # initialize room if needed
    state = rooms.setdefault(room_id, {
        "admins": set(), "waiting": set(),
        "peers": {}, "audio_tracks": []
    })

    # admins get auto-admitted
    if role == "admin":
        state["admins"].add(ws)
        await admit(ws, room_id)
        # let the admin know who’s already waiting
        for w in list(state["waiting"]):
            await ws.send_json({"type": "new_waiting", "peer_id": id(w)})
    else:
        # normal user → waiting room
        state["waiting"].add(ws)
        await ws.send_json({"type": "waiting"})
        # notify all admins
        for admin_ws in state["admins"]:
            await admin_ws.send_json({
                "type": "new_waiting",
                "peer_id": id(ws)
            })
        # block until admitted
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    pc = state["peers"][ws]

    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            # — WebRTC offer/answer —
            if typ == "offer":
                offer = RTCSessionDescription(sdp=msg["sdp"], type="offer")
                await pc.setRemoteDescription(offer)
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})

            elif typ == "ice":
                c = msg.get("candidate", {})
                # build a real RTCIceCandidate
                ice = RTCIceCandidate(
                    c["sdpMid"],
                    c["sdpMLineIndex"],
                    c["candidate"]
                )
                await pc.addIceCandidate(ice)

            # — text chat broadcast —
            elif typ == "chat":
                for peer_ws in state["peers"]:
                    await peer_ws.send_json({
                        "type": "chat",
                        "from": msg["from"],
                        "text": msg["text"]
                    })

            # — manual admit (admin only) —
            elif typ == "admit" and ws in state["admins"]:
                pid = msg.get("peer_id")
                pending = next((w for w in state["waiting"] if id(w) == pid), None)
                if pending:
                    await admit(pending, room_id)

            # — training material events —
            elif typ == "material_event" and ws in state["admins"]:
                ev = {
                    "type": "material_event",
                    "event": msg["event"],
                    "payload": msg.get("payload", {})
                }
                for peer_ws in state["peers"]:
                    await peer_ws.send_json(ev)

    except WebSocketDisconnect:
        pass
    finally:
        # cleanup
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws, None)
        if pc:
            await pc.close()
