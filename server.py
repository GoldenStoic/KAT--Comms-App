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

# ─── In-memory room state & media relay ────────────────────────────────────────
rooms = {}  # room_id → { admins:set, waiting:set, peers:dict }
relay = MediaRelay()

app = FastAPI()
# serve our client HTML+JS from static/index.html
app.mount("/static", StaticFiles(directory="static"), name="static")


# ─── Helpers ───────────────────────────────────────────────────────────────────

def normalize_ice_server(s: dict) -> dict:
    """
    Twilio returns ICE servers with either 'url' or 'urls'.
    Normalize to always have 'urls'.
    """
    d = dict(s)
    if "url" in d:
        d["urls"] = [d.pop("url")]
    return d


def parse_candidate(c: dict) -> RTCIceCandidate:
    """
    Convert the dictionary we received over WebSocket
    into a proper aiortc.RTCIceCandidate instance.
    """
    s = c["candidate"]
    parts = s.split()
    foundation = parts[0].split(":", 1)[1]
    component = int(parts[1])
    protocol = parts[2].lower()
    priority = int(parts[3])
    ip = parts[4]
    port = int(parts[5])
    typ = parts[7]

    # optional fields
    relatedAddress = None
    relatedPort = None
    tcpType = None
    i = 8
    while i < len(parts):
        if parts[i] == "raddr":
            relatedAddress = parts[i+1]
            i += 2
        elif parts[i] == "rport":
            relatedPort = int(parts[i+1])
            i += 2
        elif parts[i] == "tcptype":
            tcpType = parts[i+1]
            i += 2
        else:
            i += 1

    return RTCIceCandidate(
        component=component,
        foundation=foundation,
        ip=ip,
        port=port,
        priority=priority,
        protocol=protocol,
        type=typ,
        relatedAddress=relatedAddress,
        relatedPort=relatedPort,
        sdpMid=c.get("sdpMid"),
        sdpMLineIndex=c.get("sdpMLineIndex"),
        tcpType=tcpType
    )


async def authenticate(token: str) -> str:
    """
    Very simple JWT-based role extraction.
    """
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except:
        return "user"


# ─── ICE endpoint ─────────────────────────────────────────────────────────────
@app.get("/ice")
async def ice():
    """
    Fetch fresh Twilio ICE credentials (STUN + TURN),
    return them verbatim (we force TURN on the client).
    """
    token = twilio_client.tokens.create()
    servers = [normalize_ice_server(s) for s in token.ice_servers]
    return JSONResponse(servers)


# ─── Serve client ─────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse("static/index.html")


# ─── Admit helper ──────────────────────────────────────────────────────────────
async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)

    # signal client that it's been admitted
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    # grab fresh STUN+TURN
    token = twilio_client.tokens.create()
    ice_servers = [
        RTCIceServer(**normalize_ice_server(s))
        for s in token.ice_servers
    ]
    config = RTCConfiguration(iceServers=ice_servers)

    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    # trickle ICE → forward each candidate to the browser
    @pc.on("icecandidate")
    async def on_icecandidate(candidate):
        if candidate:
            await ws.send_json({
                "type": "ice",
                "candidate": {
                    "candidate": candidate.candidate,
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex,
                }
            })

    # live-only audio forwarding (no backlog)
    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            relay_track = relay.subscribe(track, buffered=False)
            for peer_ws, other_pc in state["peers"].items():
                if peer_ws != ws:
                    other_pc.addTrack(relay_track)

    # tell client to kick off WebRTC
    await ws.send_json({"type": "ready_for_offer"})

    # get their offer, answer it
    offer_msg = await ws.receive_json()
    offer = RTCSessionDescription(sdp=offer_msg["sdp"], type=offer_msg["type"])
    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    # wait for ICE gathering to finish so our SDP has all candidates
    while pc.iceGatheringState != "complete":
        await asyncio.sleep(0.1)
    await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})

    try:
        # now handle any incoming WS messages (ice, chat, admits, material, etc.)
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            if typ == "ice":
                cand = parse_candidate(msg["candidate"])
                await pc.addIceCandidate(cand)

            elif typ == "chat":
                for peer_ws in state["peers"]:
                    await peer_ws.send_json({
                        "type": "chat",
                        "from": msg["from"],
                        "text": msg["text"],
                    })

            elif typ == "admit" and ws in state["admins"]:
                target = msg["peer_id"]
                pending = next((w for w in state["waiting"] if id(w) == target), None)
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
        # cleanup
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        old_pc = state["peers"].pop(ws, None)
        if old_pc:
            for sender in old_pc.getSenders():
                if sender.track:
                    sender.track.stop()
            await old_pc.close()


# ─── WebSocket entry point ────────────────────────────────────────────────────
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
        # tell admin about anyone already waiting
        for pending in state["waiting"]:
            await ws.send_json({"type": "new_waiting", "peer_id": id(pending)})

    else:
        state["waiting"].add(ws)
        await ws.send_json({"type": "waiting"})
        # notify admins
        for admin_ws in state["admins"]:
            await admin_ws.send_json({"type": "new_waiting", "peer_id": id(ws)})
        # block until we move from waiting to peers
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)
