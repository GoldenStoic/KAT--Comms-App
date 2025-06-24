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
    RTCIceCandidate,
    RTCIceServer,
)
from aiortc.contrib.media import MediaRelay
from starlette.websockets import WebSocketState
from twilio.rest import Client as TwilioClient

# ─── JWT config ─────────────────────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")

# ─── In-memory room state & media relay ─────────────────────────────────────────
rooms = {}  # room_id → { admins:set, waiting:set, peers:dict }
relay = MediaRelay()

# ─── App setup ───────────────────────────────────────────────────────────────────
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# ─── Helpers ────────────────────────────────────────────────────────────────────

async def safe_send(ws: WebSocket, message: dict):
    """
    Wrap ws.send_json so that if the socket is already closed we swallow the error
    (either WebSocketDisconnect or RuntimeError from Uvicorn).
    """
    try:
        await ws.send_json(message)
    except (WebSocketDisconnect, RuntimeError):
        # peer hung up or we already closed—ignore
        pass

def normalize_ice_server(s: dict) -> dict:
    """
    Twilio returns ICE servers with either 'url' or 'urls'.
    Normalize to always have 'urls'.
    """
    d = dict(s)
    if "url" in d and "urls" not in d:
        d["urls"] = [d.pop("url")]
    return d

def parse_ice_candidate(line: str) -> RTCIceCandidate:
    """
    Convert a single ICE candidate line from the client into an RTCIceCandidate.
    """
    parts = line.strip().split()
    # (implementation as in original—untouched)
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

    c = {
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
    return RTCIceCandidate(**c)

async def authenticate(token: str) -> str:
    """
    Very simple JWT-based role extraction.
    """
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except:
        return "user"

# ─── ICE endpoint ────────────────────────────────────────────────────────────────
twilio_client = TwilioClient(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN"),
)

@app.get("/ice")
async def ice():
    """
    Fetch fresh Twilio ICE credentials (STUN + TURN),
    so that clients can negotiate P2P when they join.
    """
    token = twilio_client.tokens.create()
    servers = [RTCIceServer(**normalize_ice_server(s)) for s in token.ice_servers]
    return JSONResponse(
        {"iceServers": [srv.__dict__ for srv in servers]}
    )

# ─── Serve client ───────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse("static/index.html")

# ─── Admit helper ────────────────────────────────────────────────────────────────
async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)

    # signal client that it's been admitted
    await safe_send(ws, {"type": "admitted", "peer_id": id(ws)})

    # fetch fresh STUN/TURN for this peer
    token = twilio_client.tokens.create()
    ice_servers = [RTCIceServer(**normalize_ice_server(s)) for s in token.ice_servers]

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
        # broadcast incoming tracks to all other peers
        for peer_ws, peer_pc in list(state["peers"].items()):
            if peer_ws is not ws:
                peer_pc.addTrack(relay.subscribe(track))

    try:
        # tell client we're ready to receive its offer
        await safe_send(ws, {"type": "ready_for_offer"})

        # wait for the offer
        offer = await ws.receive_json()
        desc = RTCSessionDescription(offer["sdp"], offer["type"])
        await pc.setRemoteDescription(desc)

        # send back our answer
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await safe_send(ws, {
            "type": "answer",
            "sdp": pc.localDescription.sdp
        })

        # now relay ICE candidates both ways
        while True:
            msg = await ws.receive_json()
            t = msg.get("type", "")
            if t == "candidate":
                cand = parse_ice_candidate(msg["candidate"])
                cand.sdpMid = msg.get("sdpMid")
                cand.sdpMLineIndex = msg.get("sdpMLineIndex")
                await pc.addIceCandidate(cand)
            else:
                # if you want to handle other signaling types, do it here
                pass

    except WebSocketDisconnect:
        pass
    finally:
        # cleanup this peer
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
        # register admin
        state["admins"].add(ws)
        # immediately notify admin of any currently waiting peers
        for pending in state["waiting"]:
            await safe_send(ws, {
                "type": "new_waiting",
                "peer_id": id(pending),
            })

    else:
        # a regular peer—announce waiting status, notify admins
        state["waiting"].add(ws)
        await safe_send(ws, {"type": "waiting"})
        for admin_ws in state["admins"]:
            await safe_send(admin_ws, {
                "type": "new_waiting",
                "peer_id": id(ws),
            })
        # block until an admin actually admits us
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    # once here, *either*:
    # - an admin has just admitted this peer, or
    # - this is the admin itself
    # proceed with the full WebRTC handshake
    try:
        await _admit(ws, room_id)
    except WebSocketDisconnect:
        pass
    finally:
        # final cleanup on socket close
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        old_pc = state["peers"].pop(ws, None)
        if old_pc:
            for sender in old_pc.getSenders():
                if sender.track:
                    sender.track.stop()
            await old_pc.close()
