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
from aiortc.sdp import candidate_from_sdp

# --- debug ---
print(">>> SERVER running under:", sys.executable)
print(">>> jwt module path:", getattr(jwt, "__file__", None))

# --- CONFIG ---
ICE_SERVERS = [
    {"urls": "stun:stun.l.google.com:19302"},
]
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")

# In-memory room state
rooms = {}  # room_id → { admins, waiting, peers, audio_tracks, pending_ice }

app = FastAPI()

@app.get("/")
async def index():
    return FileResponse("static/index.html")

async def authenticate(token: str):
    try:
        data = jwt.decode(token or "", JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except:
        return "user"

async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    # build ICE config
    rtc_ice_servers = [RTCIceServer(**e) for e in ICE_SERVERS]
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

    # replay any already‐live audio
    for t in state["audio_tracks"]:
        pc.addTrack(t)

    # kick off SDP handshake
    await ws.send_json({"type": "ready_for_offer"})

# mount static
app.mount("/static", StaticFiles(directory="static"), name="static")

relay = MediaRelay()

@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()

    role = await authenticate(ws.query_params.get("token", ""))
    state = rooms.setdefault(room_id, {
        "admins": set(),
        "waiting": set(),
        "peers": {},
        "audio_tracks": [],
        "pending_ice": {},
    })

    if role == "admin":
        state["admins"].add(ws)
        await _admit(ws, room_id)
        # inform admin of existing waiters
        for pend in state["waiting"]:
            await ws.send_json({"type": "new_waiting", "peer_id": id(pend)})
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type": "waiting"})
        for a in state["admins"]:
            await a.send_json({"type": "new_waiting", "peer_id": id(ws)})
        # block until admin admits
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            if typ == "offer":
                pc = state["peers"][ws]
                offer = RTCSessionDescription(sdp=msg["sdp"], type="offer")
                await pc.setRemoteDescription(offer)
                # drain any queued ICE
                for ice in state["pending_ice"].pop(ws, []):
                    await pc.addIceCandidate(ice)
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})

            elif typ == "ice":
                cdict = msg["candidate"]
                # parse SDP string into RTCIceCandidate
                ice = candidate_from_sdp(cdict["candidate"])
                ice.sdpMid = cdict.get("sdpMid")
                ice.sdpMLineIndex = cdict.get("sdpMLineIndex")

                pc = state["peers"].get(ws)
                # if offer not yet applied, queue it
                if pc is None or pc.remoteDescription is None:
                    state["pending_ice"].setdefault(ws, []).append(ice)
                else:
                    await pc.addIceCandidate(ice)

            elif typ == "chat":
                for peer in state["peers"]:
                    await peer.send_json({
                        "type": "chat",
                        "from": msg["from"],
                        "text": msg["text"]
                    })

            elif typ == "admit" and ws in state["admins"]:
                target = next((w for w in state["waiting"] if id(w) == msg["peer_id"]), None)
                if target:
                    await _admit(target, room_id)

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
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws, None)
        if pc:
            await pc.close()
        print("closed", id(ws))
