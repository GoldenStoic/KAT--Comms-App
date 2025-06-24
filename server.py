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
    RTCIceCandidate,
)
from aiortc.contrib.media import MediaRelay

from twilio.rest import Client

print(">>> SERVER running under:", sys.executable)
print(">>> jwt module path:", getattr(jwt, "__file__", None))

# ─── Twilio Network Traversal Setup ───────────────────────────────────────────
TW_ACCOUNT_SID     = os.environ["TWILIO_ACCOUNT_SID"]
TW_API_KEY_SID     = os.environ["TWILIO_API_KEY_SID"]
TW_API_KEY_SECRET  = os.environ["TWILIO_API_KEY_SECRET"]
twilio_client = Client(TW_API_KEY_SID, TW_API_KEY_SECRET, TW_ACCOUNT_SID)

# ─── JWT CONFIG ───────────────────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")

# ─── In-memory room state ─────────────────────────────────────────────────────
# tracks will hold source audio tracks
rooms = {}  # room_id → { admins:set, waiting:set, peers:dict, tracks:list }
relay = MediaRelay()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


# ─── Utility: normalize Twilio ICE dict ───────────────────────────────────────
def normalize_ice_server(s: dict) -> dict:
    d = dict(s)
    if "url" in d:
        if "urls" not in d:
            d["urls"] = [d.pop("url")]
        else:
            d.pop("url")
    return d

# ─── Grab ONLY TURN servers from Twilio (filter out any stun:) ───────────────
def get_turn_servers():
    token = twilio_client.tokens.create()
    servers = []
    for s in token.ice_servers:
        s_norm = normalize_ice_server(s)
        # keep only URLs that start with "turn:" or "turns:"
        if any(u.startswith(("turn:", "turns:")) for u in s_norm.get("urls", [])):
            servers.append(s_norm)
    return servers

# ─── ICE endpoint for browser clients ─────────────────────────────────────────
@app.get("/ice")
async def ice():
    servers = get_turn_servers()
    return JSONResponse(servers)

# ─── Serve static HTML/JS ─────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse("static/index.html")

# ─── JWT auth helper ─────────────────────────────────────────────────────────
async def authenticate(token: str):
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except:
        return "user"


# ─── Admit helper: create a new RTCPeerConnection ────────────────────────────
async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    # fetch TURN-only ICE servers
    turn_servers = get_turn_servers()
    ice_servers = [RTCIceServer(**s) for s in turn_servers]
    config = RTCConfiguration(iceServers=ice_servers)

    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            # store the source track
            state["tracks"].append(track)
            # broadcast live to everyone else
            for other_ws, other_pc in state["peers"].items():
                if other_pc is not pc:
                    other_pc.addTrack(relay.subscribe(track))

    # subscribe this new peer to all existing live tracks
    for source_track in state["tracks"]:
        pc.addTrack(relay.subscribe(source_track))

    await ws.send_json({"type": "ready_for_offer"})


# ─── WebSocket signaling ─────────────────────────────────────────────────────
@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role = await authenticate(ws.query_params.get("token", ""))
    state = rooms.setdefault(
        room_id,
        {"admins": set(), "waiting": set(), "peers": {}, "tracks": []}
    )

    if role == "admin":
        state["admins"].add(ws)
        await _admit(ws, room_id)
        for pending in state["waiting"]:
            await ws.send_json({"type": "new_waiting", "peer_id": id(pending)})
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type": "waiting"})
        for admin_ws in state["admins"]:
            await admin_ws.send_json({"type": "new_waiting", "peer_id": id(ws)})
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            if typ == "offer":
                pc = state["peers"][ws]
                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=msg["sdp"], type="offer")
                )
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})

            elif typ == "ice":
                pc = state["peers"][ws]
                c = msg["candidate"]
                cand = RTCIceCandidate(
                    sdpMid=c.get("sdpMid"),
                    sdpMLineIndex=c.get("sdpMLineIndex"),
                    candidate=c.get("candidate")
                )
                try:
                    await pc.addIceCandidate(cand)
                except:
                    pass

            elif typ == "admit" and ws in state["admins"]:
                target = msg["peer_id"]
                pending = next((w for w in state["waiting"] if id(w) == target), None)
                if pending:
                    await _admit(pending, room_id)

    except WebSocketDisconnect:
        pass
    finally:
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws, None)
        if pc:
            for sender in pc.getSenders():
                if sender.track:
                    sender.track.stop()
            await pc.close()
