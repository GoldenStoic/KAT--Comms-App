# server.py

import os
import asyncio
import jwt

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCIceCandidate,
    RTCIceServer,
)
from aiortc.contrib.media import MediaRelay
from twilio.rest import Client as TwilioClient

# ─── JWT config ─────────────────────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")

# ─── In-memory room state & media relay ──────────────────────────────────────────
rooms = {}  # room_id → { admins:set, waiting:set, peers:dict }
relay = MediaRelay()

# ─── App setup ───────────────────────────────────────────────────────────────────
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


# ─── Helpers ────────────────────────────────────────────────────────────────────

async def safe_send(ws: WebSocket, message: dict):
    """
    Wrap ws.send_json so that if the socket is already closed we swallow the error.
    """
    try:
        await ws.send_json(message)
    except (WebSocketDisconnect, RuntimeError):
        # socket already closed—ignore
        pass


def normalize_ice_server(spec) -> dict:
    """
    Take Twilio's ICE‐server spec (a dict or an object with a to_dict() / _properties)
    and return ONLY the keys RTCIceServer() accepts: urls, username, credential.
    """
    # get a plain dict
    if hasattr(spec, "to_dict"):
        d = spec.to_dict()
    elif hasattr(spec, "_properties"):
        d = dict(spec._properties)
    else:
        d = dict(spec)

    # Twilio sometimes uses "url" instead of "urls"
    if "url" in d and "urls" not in d:
        d["urls"] = [d.pop("url")]

    # now filter to exactly the arguments RTCIceServer wants
    allowed = {}
    if "urls" in d:
        allowed["urls"] = d["urls"]
    if "username" in d:
        allowed["username"] = d["username"]
    if "credential" in d:
        allowed["credential"] = d["credential"]
    return allowed


def parse_ice_candidate(line: str) -> RTCIceCandidate:
    """
    Convert a single ICE candidate line into an RTCIceCandidate.
    """
    parts = line.strip().split()
    component = int(parts[1].split("=")[1])
    foundation = parts[2].split("=")[1]
    priority = int(parts[3].split("=")[1])
    ip = parts[4].split("=")[1]
    port = int(parts[5].split("=")[1])
    typ = parts[6].split("=")[1]
    protocol = parts[7].split("=")[1]

    relatedAddress = None
    relatedPort = None
    tcpType = None
    i = 8
    while i < len(parts):
        if parts[i] == "raddr":
            relatedAddress = parts[i + 1]
            i += 2
        elif parts[i] == "rport":
            relatedPort = int(parts[i + 1])
            i += 2
        elif parts[i] == "tcptype":
            tcpType = parts[i + 1]
            i += 2
        else:
            i += 1

    obj = {
        "component": component,
        "foundation": foundation,
        "priority": priority,
        "ip": ip,
        "port": port,
        "protocol": protocol,
        "type": typ,
        "relatedAddress": relatedAddress,
        "relatedPort": relatedPort,
        "sdpMid": None,
        "sdpMLineIndex": None,
        "tcpType": tcpType,
    }
    return RTCIceCandidate(**obj)


async def authenticate(token: str) -> str:
    """
    Simple JWT‐based role extractor.
    """
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except:
        return "user"


# ─── Twilio REST client ─────────────────────────────────────────────────────────
twilio_client = TwilioClient(
    os.getenv("TWILIO_API_KEY_SID"),
    os.getenv("TWILIO_API_KEY_SECRET"),
    os.getenv("TWILIO_ACCOUNT_SID"),
)


# ─── ICE endpoint ────────────────────────────────────────────────────────────────
@app.get("/ice")
async def ice():
    """
    Return STUN/TURN servers to the client.
    """
    token = twilio_client.tokens.create()
    ice_servers = [
        RTCIceServer(**normalize_ice_server(s))
        for s in token.ice_servers
    ]
    return JSONResponse(
        {"iceServers": [srv.__dict__ for srv in ice_servers]}
    )


# ─── Serve client ───────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse("static/index.html")


# ─── Admit helper ────────────────────────────────────────────────────────────────
async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)

    # notify admitted
    await safe_send(ws, {"type": "admitted", "peer_id": id(ws)})

    # fetch fresh STUN/TURN for this peer
    token = twilio_client.tokens.create()
    ice_servers = [
        RTCIceServer(**normalize_ice_server(s))
        for s in token.ice_servers
    ]

    pc = RTCPeerConnection({"iceServers": ice_servers})
    state["peers"][ws] = pc

    @pc.on("icecandidate")
    async def on_icecandidate(event):
        if event.candidate:
            await safe_send(ws, {
                "type": "candidate",
                "candidate": event.candidate.to_sdp(),
                "sdpMid": event.candidate.sdpMid,
                "sdpMLineIndex": event.candidate.sdpMLineIndex,
            })

    @pc.on("track")
    def on_track(track):
        relay.subscribe(track)
        for peer_ws, peer_pc in list(state["peers"].items()):
            if peer_ws is not ws:
                peer_pc.addTrack(relay.subscribe(track))

    try:
        await safe_send(ws, {"type": "ready_for_offer"})
        offer = await ws.receive_json()
        desc = RTCSessionDescription(offer["sdp"], offer["type"])
        await pc.setRemoteDescription(desc)

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await safe_send(ws, {
            "type": "answer",
            "sdp": pc.localDescription.sdp
        })

        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "candidate":
                cand = parse_ice_candidate(msg["candidate"])
                cand.sdpMid = msg.get("sdpMid")
                cand.sdpMLineIndex = msg.get("sdpMLineIndex")
                await pc.addIceCandidate(cand)

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


# ─── WebSocket entry point ──────────────────────────────────────────────────────
@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role = await authenticate(ws.query_params.get("token", ""))
    state = rooms.setdefault(room_id, {
        "admins": set(),
        "waiting": set(),
        "peers": {},
    })

    if role == "admin":
        state["admins"].add(ws)
        for pending in state["waiting"]:
            await safe_send(ws, {
                "type": "new_waiting",
                "peer_id": id(pending),
            })
    else:
        state["waiting"].add(ws)
        await safe_send(ws, {"type": "waiting"})
        for admin_ws in state["admins"]:
            await safe_send(admin_ws, {
                "type": "new_waiting",
                "peer_id": id(ws),
            })
        # wait until an admin admits us
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    try:
        await _admit(ws, room_id)
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
