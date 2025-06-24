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

# ─── Twilio creds ─────────────────────────────────────────────────────────────
TW_ACCOUNT_SID    = os.environ["TWILIO_ACCOUNT_SID"]
TW_API_KEY_SID    = os.environ["TWILIO_API_KEY_SID"]
TW_API_KEY_SECRET = os.environ["TWILIO_API_KEY_SECRET"]
twilio_client = Client(TW_API_KEY_SID, TW_API_KEY_SECRET, TW_ACCOUNT_SID)

# ─── JWT config ────────────────────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")

# ─── In-memory room state ───────────────────────────────────────────────────────
rooms = {}  # room_id → { admins:set, waiting:set, peers:dict }
relay = MediaRelay()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


# ─── Normalize Twilio ICE dict ────────────────────────────────────────────────
def normalize_ice_server(s: dict) -> dict:
    d = dict(s)
    if "url" in d:
        d["urls"] = [d.pop("url")]
    return d


# ─── /ice endpoint ─────────────────────────────────────────────────────────────
@app.get("/ice")
async def ice():
    token = twilio_client.tokens.create()
    servers = [normalize_ice_server(s) for s in token.ice_servers]
    return JSONResponse(servers)


# ─── Serve the HTML/JS ─────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse("static/index.html")


# ─── Simple JWT auth ───────────────────────────────────────────────────────────
async def authenticate(token: str) -> str:
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except:
        return "user"


# ─── Admit helper ──────────────────────────────────────────────────────────────
async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    # fetch fresh STUN+TURN
    token = twilio_client.tokens.create()
    ice_servers = [
        RTCIceServer(**normalize_ice_server(s))
        for s in token.ice_servers
    ]
    config = RTCConfiguration(iceServers=ice_servers)

    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    # trickle ICE back to client
    @pc.on("icecandidate")
    async def on_icecandidate(candidate):
        if candidate:
            await ws.send_json({
                "type": "ice",
                "candidate": candidate.toJSON()
            })

    # live‐relay any incoming audio track to all other peers
    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            relay_track = relay.subscribe(track)
            for peer_ws, other_pc in state["peers"].items():
                if peer_ws is not ws:
                    other_pc.addTrack(relay_track)

    # kickoff offer/answer
    await ws.send_json({"type": "ready_for_offer"})

    offer_msg = await ws.receive_json()
    offer = RTCSessionDescription(sdp=offer_msg["sdp"], type=offer_msg["type"])
    await pc.setRemoteDescription(offer)

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    # wait for candidates to be gathered
    while pc.iceGatheringState != "complete":
        await asyncio.sleep(0.1)
    await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})

    # now handle incoming messages
    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            if typ == "ice":
                # let aiortc parse the dict for us
                await pc.addIceCandidate(msg["candidate"])

            elif typ == "chat":
                for peer_ws in state["peers"]:
                    await peer_ws.send_json({
                        "type": "chat",
                        "from": msg["from"],
                        "text": msg["text"],
                    })

            elif typ == "admit" and ws in state["admins"]:
                target = msg["peer_id"]
                pending = next(
                    (w for w in state["waiting"] if id(w) == target),
                    None
                )
                if pending:
                    await _admit(pending, room_id)

            elif typ == "material_event" and ws in state["admins"]:
                for peer_ws in state["peers"]:
                    await peer_ws.send_json({
                        "type": "material_event",
                        "event": msg["event"],
                        "payload": msg.get("payload", {}),
                    })

    except WebSocketDisconnect:
        pass
    finally:
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        old_pc = state["peers"].pop(ws, None)
        if old_pc:
            for sender in old_pc.getSenders():
                if sender.track:
                    sender.track.stop()
            await old_pc.close()


# ─── WebSocket endpoint ───────────────────────────────────────────────────────
@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role = await authenticate(ws.query_params.get("token", ""))
    state = rooms.setdefault(room_id, {
        "admins": set(),
        "waiting": set(),
        "peers": {}
    })

    if role == "admin":
        state["admins"].add(ws)
        await _admit(ws, room_id)
        for pending in state["waiting"]:
            await ws.send_json({"type": "new_waiting", "peer_id": id(pending)})
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type": "waiting"})
        for admin_ws in state["admins"]:
            await admin_ws.send_json({
                "type": "new_waiting",
                "peer_id": id(ws)
            })
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)
