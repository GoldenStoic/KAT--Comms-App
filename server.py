# server.py
import os
import sys
import asyncio
import jwt

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCIceServer,
    RTCConfiguration,
)
from aiortc.contrib.media import MediaRelay
from twilio.rest import Client

print(">>> SERVER running under:", sys.executable)
print(">>> jwt module path:", getattr(jwt, "__file__", None))

# ─── Twilio Network Traversal Setup ───────────────────────────────────────────
TW_ACCOUNT_SID    = os.environ["TWILIO_ACCOUNT_SID"]
TW_API_KEY_SID    = os.environ["TWILIO_API_KEY_SID"]
TW_API_KEY_SECRET = os.environ["TWILIO_API_KEY_SECRET"]
twilio_client = Client(TW_API_KEY_SID, TW_API_KEY_SECRET, TW_ACCOUNT_SID)

# ─── JWT CONFIG ───────────────────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")

# ─── In-memory room state ─────────────────────────────────────────────────────
# tracks will hold the original MediaStreamTrack for future peers
rooms = {}  # room_id → { admins:set, waiting:set, peers:dict, tracks:list }
relay = MediaRelay()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


def normalize_ice_server(s: dict) -> dict:
    d = dict(s)
    if "url" in d:
        # Twilio sometimes returns "url" instead of "urls"
        if "urls" not in d:
            d["urls"] = [d.pop("url")]
        else:
            d.pop("url")
    return d


@app.get("/ice")
async def ice():
    """Give the browser fresh Twilio ICE (STUN+TURN) credentials."""
    token = twilio_client.tokens.create()
    return JSONResponse([normalize_ice_server(s) for s in token.ice_servers])


@app.get("/")
async def index():
    return FileResponse("static/index.html")


async def authenticate(token: str):
    """Just decode a JWT `?token=` if you want roles; defaults to 'user'."""
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except:
        return "user"


async def _admit(ws: WebSocket, room_id: str):
    """Create a new RTCPeerConnection for this WebSocket, forward tracks."""
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    # get fresh TURN creds
    token = twilio_client.tokens.create()
    ice_servers = [RTCIceServer(**normalize_ice_server(s)) for s in token.ice_servers]
    config = RTCConfiguration(iceServers=ice_servers)

    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        # only audio
        if track.kind == "audio":
            # save the original track for any future joiners
            state["tracks"].append(track)
            # immediately forward *new* audio frames to everyone else
            for peer_ws, peer_pc in state["peers"].items():
                if peer_ws is not ws:
                    peer_pc.addTrack(relay.subscribe(track))

    # if there are already live tracks (from the admin), pipe *only live*
    # into this brand-new peer
    for orig in state["tracks"]:
        pc.addTrack(relay.subscribe(orig))

    # signal the client to start the WebRTC offer/answer
    await ws.send_json({"type": "ready_for_offer"})


@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role = await authenticate(ws.query_params.get("token", ""))
    state = rooms.setdefault(
        room_id,
        {"admins": set(), "waiting": set(), "peers": {}, "tracks": []},
    )

    # admins get in immediately
    if role == "admin":
        state["admins"].add(ws)
        await _admit(ws, room_id)
        # let them see who else is waiting
        for pending in state["waiting"]:
            await ws.send_json({"type": "new_waiting", "peer_id": id(pending)})
    else:
        # normal users wait for an admin
        state["waiting"].add(ws)
        await ws.send_json({"type": "waiting"})
        for a in state["admins"]:
            await a.send_json({"type": "new_waiting", "peer_id": id(ws)})
        # spin until an admin admits them
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    try:
        while True:
            msg = await ws.receive_json()
            t = msg.get("type")

            if t == "offer":
                pc = state["peers"][ws]
                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=msg["sdp"], type="offer")
                )
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})

            elif t == "ice":
                pc = state["peers"][ws]
                # let aiortc parse the candidate for you
                await pc.addIceCandidate(msg["candidate"])

            elif t == "chat":
                # simple broadcast
                for p in state["peers"]:
                    await p.send_json(
                        {"type": "chat", "from": msg["from"], "text": msg["text"]}
                    )

            elif t == "admit" and ws in state["admins"]:
                # admin admitting a waiting user
                target = msg["peer_id"]
                pending = next((w for w in state["waiting"] if id(w) == target), None)
                if pending:
                    await _admit(pending, room_id)

    except WebSocketDisconnect:
        pass
    finally:
        # cleanup
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws, None)
        if pc:
            await pc.close()
