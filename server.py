import os
import sys
import jwt
import asyncio

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState

from aiortc import (
    RTCPeerConnection,
    RTCConfiguration,
    RTCIceServer,
    RTCSessionDescription,
)
from aiortc.contrib.media import MediaRelay

# debug:
print(">>> running under", sys.executable)
print(">>> jwt from", getattr(jwt, "__file__", None))

# --- CONFIG ---
ICE_SERVERS = [
    RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
    RTCIceServer(urls=["stun:global.stun.twilio.com:3478?transport=udp"])
]
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")

# in-memory state
rooms = {}  # room_id -> {"admins":set(ws), "waiting":set(ws), "peers":{ws:pc}, "audio": [track,...]}
relay = MediaRelay()

app = FastAPI()


async def authenticate(token: str) -> str:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload.get("role", "user")
    except Exception:
        return "user"


async def safe_send(ws: WebSocket, msg: dict):
    """
    Send msg to ws only if still open; otherwise drop it.
    """
    if ws.client_state == WebSocketState.CONNECTED:
        try:
            await ws.send_json(msg)
        except Exception:
            # ensure we don't keep a broken socket around
            await ws.close()
            raise


async def broadcast_admins(state: dict, msg: dict):
    for admin in list(state["admins"]):
        try:
            await safe_send(admin, msg)
        except Exception:
            state["admins"].discard(admin)


async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)

    # notify the client
    await safe_send(ws, {"type": "admitted", "peer_id": id(ws)})

    # create PeerConnection
    config = RTCConfiguration(iceServers=ICE_SERVERS)
    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            r = relay.subscribe(track)
            state["audio"].append(r)
            # forward to everyone else
            for other in state["peers"].values():
                if other is not pc:
                    other.addTrack(r)

    # forward existing audio to the newcomer
    for t in state["audio"]:
        pc.addTrack(t)

    # tell the client to createOffer()
    await safe_send(ws, {"type": "ready_for_offer"})


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    token = ws.query_params.get("token", "")
    role = await authenticate(token)

    state = rooms.setdefault(room_id, {
        "admins": set(), "waiting": set(),
        "peers": {}, "audio": []
    })

    # --- ADMIN ARRIVES ---
    if role == "admin":
        state["admins"].add(ws)
        # admit self
        await _admit(ws, room_id)
        # tell this admin about anyone already waiting
        for pending in list(state["waiting"]):
            await safe_send(ws, {"type": "new_waiting", "peer_id": id(pending)})

    # --- USER ARRIVES ---
    else:
        state["waiting"].add(ws)
        await safe_send(ws, {"type": "waiting"})
        # inform all admins
        await broadcast_admins(state, {"type": "new_waiting", "peer_id": id(ws)})
        # block until an admin actually admits us
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    # --- MAIN LOOP for admitted peers ---
    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            # SDP offer from client
            if typ == "offer":
                pc = state["peers"][ws]
                offer = RTCSessionDescription(sdp=msg["sdp"], type="offer")
                await pc.setRemoteDescription(offer)
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await safe_send(ws, {"type": "answer", "sdp": pc.localDescription.sdp})

            # ICE candidate from client
            elif typ == "ice":
                pc = state["peers"][ws]
                candidate = msg.get("candidate")
                if candidate:
                    # aiortc accepts a dict here
                    await pc.addIceCandidate(candidate)

            # chat messages
            elif typ == "chat":
                for peer in list(state["peers"].keys()):
                    await safe_send(peer, {
                        "type": "chat",
                        "from": msg.get("from", "user"),
                        "text": msg.get("text", "")
                    })

            # manual admit via admin UI
            elif typ == "admit" and ws in state["admins"]:
                target_id = msg.get("peer_id")
                pending = next((w for w in state["waiting"] if id(w) == target_id), None)
                if pending:
                    await _admit(pending, room_id)

            # material events (slides/quizzes)
            elif typ == "material_event" and ws in state["admins"]:
                for peer in list(state["peers"].keys()):
                    await safe_send(peer, {
                        "type": "material_event",
                        "event": msg.get("event"),
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


# --- STATIC FILES ---
app.mount("/static", StaticFiles(directory="static"), name="static")
