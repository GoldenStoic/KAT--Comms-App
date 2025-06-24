import os
import sys
import asyncio
import jwt

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from aiortc import (
    RTCPeerConnection,
    RTCConfiguration,
    RTCSessionDescription,
    RTCIceCandidate,
    RTCIceServer,
)
from aiortc.contrib.media import MediaRelay
from twilio.rest import Client as TwilioClient

# ─── Setup ───────────────────────────────────────────────────────────────────────
print(">>> server.py running under:", sys.executable)
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")

# Twilio credentials
TW_ACCOUNT_SID    = os.environ["TWILIO_ACCOUNT_SID"]
TW_API_KEY_SID    = os.environ["TWILIO_API_KEY_SID"]
TW_API_KEY_SECRET = os.environ["TWILIO_API_KEY_SECRET"]
twilio = TwilioClient(TW_API_KEY_SID, TW_API_KEY_SECRET, TW_ACCOUNT_SID)

# Pre‐fetch ICE servers
ice_token = twilio.tokens.create()
GLOBAL_ICE = []
for s in ice_token.ice_servers:
    d = dict(s)
    if "url" in d:
        # normalize to 'urls'
        if "urls" not in d:
            d["urls"] = [d.pop("url")]
        else:
            d.pop("url")
    GLOBAL_ICE.append(d)

app   = FastAPI()
relay = MediaRelay()
rooms = {}  # room_id → { admins:set, waiting:set, peers:dict }

# ─── Helpers ────────────────────────────────────────────────────────────────────
async def safe_send(ws: WebSocket, msg: dict):
    try:
        await ws.send_json(msg)
    except:
        # ignore if socket closed
        pass

async def authenticate(token: str) -> str:
    # literal 'admin' or 'user' for testing, else JWT decode
    if token in ("admin", "user"):
        return token
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except:
        return "user"

def parse_ice_candidate(c: dict) -> RTCIceCandidate:
    parts = c["candidate"].split()
    return RTCIceCandidate(
        foundation=parts[0].split(":",1)[1],
        component=int(parts[1]),
        protocol=parts[2].lower(),
        priority=int(parts[3]),
        ip=parts[4],
        port=int(parts[5]),
        type=parts[7],
        sdpMid=c.get("sdpMid"),
        sdpMLineIndex=c.get("sdpMLineIndex")
    )

async def init_peer(ws: WebSocket, room_id: str):
    """
    Called when a peer is admitted. Creates RTCPeerConnection,
    sends 'admitted' and 'ready_for_offer'.
    """
    state = rooms[room_id]
    state["waiting"].discard(ws)

    # build PeerConnection
    ice_servers = [RTCIceServer(**d) for d in GLOBAL_ICE]
    config = RTCConfiguration(iceServers=ice_servers)
    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            # relay live audio to all other peers
            relay_track = relay.subscribe(track)
            for other_pc in state["peers"].values():
                if other_pc is not pc:
                    other_pc.addTrack(relay_track)

    # notify client
    await safe_send(ws, {"type":"admitted",    "peer_id": id(ws)})
    await safe_send(ws, {"type":"ready_for_offer"})

# ─── HTTP endpoints ─────────────────────────────────────────────────────────────
@app.get("/ice")
async def ice():
    # return the raw list (our client treats it as an array)
    return JSONResponse(GLOBAL_ICE)

@app.get("/")
async def index():
    return FileResponse("static/index.html")

# ─── WebSocket endpoint ─────────────────────────────────────────────────────────
@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role = await authenticate(ws.query_params.get("token",""))
    state = rooms.setdefault(room_id, {
        "admins": set(),
        "waiting": set(),
        "peers": {}
    })

    # register
    if role == "admin":
        state["admins"].add(ws)
        # let admin hear about waiting peers
        for p in state["waiting"]:
            await safe_send(ws, {"type":"new_waiting", "peer_id": id(p)})
    else:
        state["waiting"].add(ws)
        await safe_send(ws, {"type":"waiting"})
        # notify admins
        for admin_ws in state["admins"]:
            await safe_send(admin_ws, {"type":"new_waiting", "peer_id": id(ws)})

    try:
        # single receive_json loop—no nested calls!
        while True:
            msg = await ws.receive_json()
            t   = msg.get("type")

            if t == "admit" and ws in state["admins"]:
                # admin admits a waiting peer
                peer_id = msg.get("peer_id")
                pending = next((p for p in state["waiting"] if id(p)==peer_id), None)
                if pending:
                    await init_peer(pending, room_id)

            elif t == "offer" and ws in state["peers"]:
                # peer has sent an offer
                pc = state["peers"][ws]
                await pc.setRemoteDescription(RTCSessionDescription(
                    sdp=msg["sdp"], type="offer"
                ))
                answer = await pc.createAnswer()
                # patch SDP for low-latency audio
                lines, out = answer.sdp.splitlines(), []
                for L in lines:
                    out.append(L)
                    if L.startswith("m=audio"):
                        out += ["a=sendrecv","a=ptime:20","a=maxptime:20"]
                patched = "\r\n".join(out)+"\r\n"
                await pc.setLocalDescription(RTCSessionDescription(
                    sdp=patched, type="answer"
                ))
                await safe_send(ws, {"type":"answer","sdp":pc.localDescription.sdp})

            elif t == "ice" and ws in state["peers"]:
                # ICE candidate from peer
                pc = state["peers"][ws]
                cand = parse_ice_candidate(msg["candidate"])
                await pc.addIceCandidate(cand)

            elif t == "chat":
                # broadcast chat to all peers
                for peer_ws in state["peers"]:
                    await safe_send(peer_ws, msg)

            elif t == "material_event" and ws in state["admins"]:
                # broadcast material events
                for peer_ws in state["peers"]:
                    await safe_send(peer_ws, msg)

    except WebSocketDisconnect:
        pass
    finally:
        # cleanup
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws, None)
        if pc:
            for s in pc.getSenders():
                if s.track: s.track.stop()
            await pc.close()
