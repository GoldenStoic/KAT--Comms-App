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
    RTCConfiguration,
    RTCIceServer,
)
from aiortc.contrib.media import MediaRelay

from twilio.rest import Client as TwilioClient

print(">>> SERVER running under:", sys.executable)
print(">>> jwt module path:", getattr(jwt, "__file__", None))

# —————————————————————————————
# 1) Configuration from ENV
# —————————————————————————————
JWT_SECRET         = os.environ["JWT_SECRET"]
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN")

twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# In-memory room state
rooms = {}  # room_id → {admins, waiting, peers, audio_tracks}
relay = MediaRelay()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


async def authenticate(token: str) -> str:
    """Decode JWT and return role, or 'user' on failure."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload.get("role", "user")
    except Exception:
        return "user"


async def get_ice_servers():
    """Fetch ICE servers from Twilio (or fallback to Google's STUN)."""
    if twilio_client:
        # Twilio’s Network Traversal endpoint
        token = twilio_client.tokens.create()
        servers = []
        for s in token.ice_servers:
            servers.append(RTCIceServer(
                urls=s["urls"],
                username=s.get("username"),
                credential=s.get("credential"),
            ))
        return servers

    # fallback
    return [RTCIceServer(urls=["stun:stun.l.google.com:19302"])]


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/ice_servers")
async def ice_servers():
    """Return JSON array of ICE‐server configs for the client."""
    servers = await get_ice_servers()
    out = []
    for s in servers:
        item = {"urls": s.urls}
        if getattr(s, "username", None):
            item["username"] = s.username
        if getattr(s, "credential", None):
            item["credential"] = s.credential
        out.append(item)
    return out


@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role = await authenticate(ws.query_params.get("token", ""))
    state = rooms.setdefault(room_id, {
        "admins": set(), "waiting": set(),
        "peers": {}, "audio_tracks": []
    })

    # — admit admin immediately —
    if role == "admin":
        state["admins"].add(ws)
        await admit(ws, room_id)
        # let new admin know about any waiters
        for waiter in state["waiting"]:
            await ws.send_json({"type": "new_waiting", "peer_id": id(waiter)})

    # — otherwise put user in waiting list —
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type": "waiting"})
        for admin_ws in state["admins"]:
            await admin_ws.send_json({"type": "new_waiting", "peer_id": id(ws)})
        # block until admitted
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            if typ == "offer":
                pc = state["peers"][ws]
                await pc.setRemoteDescription(RTCSessionDescription(msg["sdp"], "offer"))
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})

            elif typ == "ice":
                # msg["candidate"] is the JS‐side toJSON() dict
                pc = state["peers"][ws]
                await pc.addIceCandidate(msg["candidate"])

            elif typ == "chat":
                for p in state["peers"]:
                    await p.send_json(msg)

            elif typ == "admit" and ws in state["admins"]:
                target = msg["peer_id"]
                pending = next((w for w in state["waiting"] if id(w) == target), None)
                if pending:
                    await admit(pending, room_id)

            elif typ == "material_event" and ws in state["admins"]:
                for p in state["peers"]:
                    await p.send_json(msg)

    except WebSocketDisconnect:
        pass

    finally:
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws, None)
        if pc:
            await pc.close()


async def admit(ws: WebSocket, room_id: str):
    """Move a waiting ws into the peers set and set up an SFU PC."""
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    ice_servers = await get_ice_servers()
    config = RTCConfiguration(iceServers=ice_servers)
    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            relay_track = relay.subscribe(track)
            state["audio_tracks"].append(relay_track)
            # fan-out to everyone else
            for other in state["peers"].values():
                if other is not pc:
                    other.addTrack(relay_track)

    # re-attach existing audio
    for t in state["audio_tracks"]:
        pc.addTrack(t)

    # tell client to start negotiation
    await ws.send_json({"type": "ready_for_offer"})
