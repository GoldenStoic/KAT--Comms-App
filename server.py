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
    RTCConfiguration,
)
from aiortc.contrib.media import MediaRelay
from twilio.rest import Client as TwilioClient

print(">>> server.py running under:", sys.executable)

# ─── Configuration ─────────────────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")
TW_ACCOUNT_SID    = os.environ["TWILIO_ACCOUNT_SID"]
TW_API_KEY_SID    = os.environ["TWILIO_API_KEY_SID"]
TW_API_KEY_SECRET = os.environ["TWILIO_API_KEY_SECRET"]

# ─── Twilio ICE setup ───────────────────────────────────────────────────────────
twilio = TwilioClient(TW_API_KEY_SID, TW_API_KEY_SECRET, TW_ACCOUNT_SID)

# ─── In-memory room state & media relay ─────────────────────────────────────────
app   = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
relay = MediaRelay()
rooms = {}  # room_id → { admins:set, waiting:set, peers:dict, audio_tracks:list }


# ─── Helpers ─────────────────────────────────────────────────────────────────────
async def safe_send(ws: WebSocket, msg: dict):
    try:
        await ws.send_json(msg)
    except:
        pass  # socket closed or already closed

async def authenticate(token: str) -> str:
    if token in ("admin", "user"):
        return token
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except:
        return "user"

def normalize_ice_server(spec) -> dict:
    """
    Convert Twilio ICE‐server spec to only the keys RTCIceServer accepts:
    `urls`, `username`, `credential`.
    """
    # Turn it into a plain dict
    if hasattr(spec, "to_dict"):
        d = spec.to_dict()
    elif hasattr(spec, "_properties"):
        d = dict(spec._properties)
    else:
        d = dict(spec)

    # Twilio sometimes uses "url" instead of "urls"
    if "url" in d and "urls" not in d:
        d["urls"] = [d.pop("url")]

    # Now filter to exactly the arguments RTCIceServer wants:
    allowed = {}
    if "urls" in d:
        allowed["urls"] = d["urls"]
    if "username" in d:
        allowed["username"] = d["username"]
    if "credential" in d:
        allowed["credential"] = d["credential"]
    return allowed

def parse_ice_candidate(line: str) -> RTCIceCandidate:
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
            relatedAddress = parts[i + 1]; i += 2
        elif parts[i] == "rport":
            relatedPort = int(parts[i + 1]); i += 2
        elif parts[i] == "tcptype":
            tcpType = parts[i + 1]; i += 2
        else:
            i += 1

    return RTCIceCandidate(
        component=component,
        foundation=foundation,
        priority=priority,
        ip=ip,
        port=port,
        protocol=protocol,
        type=typ,
        relatedAddress=relatedAddress,
        relatedPort=relatedPort,
        sdpMid=None,
        sdpMLineIndex=None,
        tcpType=tcpType,
    )


# ─── ICE endpoint ───────────────────────────────────────────────────────────────
@app.get("/ice")
async def ice():
    token = twilio.tokens.create()
    ice_servers = [RTCIceServer(**normalize_ice_server(s)) for s in token.ice_servers]
    return JSONResponse({"iceServers": [srv.__dict__ for srv in ice_servers]})


# ─── Serve client ───────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse("static/index.html")


# ─── Admit helper ───────────────────────────────────────────────────────────────
async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await safe_send(ws, {"type": "admitted", "peer_id": id(ws)})

    # build PeerConnection
    token = twilio.tokens.create()
    ice_servers = [RTCIceServer(**normalize_ice_server(s)) for s in token.ice_servers]
    pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
    state["peers"][ws] = pc

    @pc.on("icecandidate")
    async def on_icecandidate(event):
        if event.candidate:
            await safe_send(ws, {
                "type": "ice",
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

    await safe_send(ws, {"type": "ready_for_offer"})

    # handle the rest: offer → patched answer, then ice/chat loop
    offer = await ws.receive_json()
    desc = RTCSessionDescription(offer["sdp"], offer["type"])
    await pc.setRemoteDescription(desc)

    answer = await pc.createAnswer()
    # force 20ms ptime for low latency
    lines, out = answer.sdp.splitlines(), []
    for L in lines:
        out.append(L)
        if L.startswith("m=audio"):
            out += ["a=sendrecv", "a=ptime:20", "a=maxptime:20"]
    patched = "\r\n".join(out) + "\r\n"
    await pc.setLocalDescription(RTCSessionDescription(patched, "answer"))
    await safe_send(ws, {"type": "answer", "sdp": pc.localDescription.sdp})

    try:
        while True:
            msg = await ws.receive_json()
            t = msg.get("type")

            if t == "ice":
                cand = parse_ice_candidate(msg["candidate"])
                try:
                    await pc.addIceCandidate(cand)
                except:
                    pass

            elif t == "chat":
                for peer in state["peers"]:
                    await safe_send(peer, msg)

            elif t == "admit" and ws in state["admins"]:
                pending = next((p for p in state["waiting"] if id(p) == msg["peer_id"]), None)
                if pending:
                    await _admit(pending, room_id)

            elif t == "material_event" and ws in state["admins"]:
                for peer in state["peers"]:
                    await safe_send(peer, msg)

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
        await _admit(ws, room_id)
        for pending in state["waiting"]:
            await safe_send(ws, {"type": "new_waiting", "peer_id": id(pending)})

    else:
        state["waiting"].add(ws)
        await safe_send(ws, {"type": "waiting"})
        for adm in state["admins"]:
            await safe_send(adm, {"type": "new_waiting", "peer_id": id(ws)})

        while ws not in state["peers"]:
            await asyncio.sleep(0.1)
        await _admit(ws, room_id)
