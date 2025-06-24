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

# ─── In-memory room state ───────────────────────────────────────────────────────
# rooms: room_id → { admins:set, waiting:set, peers:dict }
rooms = {}
relay = MediaRelay()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


# ─── Normalize Twilio ICE dict ────────────────────────────────────────────────
def normalize_ice_server(s: dict) -> dict:
    d = dict(s)
    if "url" in d:
        d["urls"] = [d.pop("url")]
    return d


# ─── /ice endpoint returns TURN credentials only ───────────────────────────────
@app.get("/ice")
async def ice():
    token = twilio_client.tokens.create()
    servers = [normalize_ice_server(s) for s in token.ice_servers]
    # Twilio returns both STUN & TURN; we keep them all and let the browser
    # decide (we force relay on the client)
    return JSONResponse(servers)


# ─── Serve the HTML/JS ─────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse("static/index.html")


# ─── Simple JWT auth (admin vs user) ───────────────────────────────────────────
async def authenticate(token: str) -> str:
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except:
        return "user"


# ─── Admit helper: wire up a new RTCPeerConnection ─────────────────────────────
async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    # tell client it’s been admitted
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    # fetch fresh TURN+STUN servers
    token = twilio_client.tokens.create()
    ice_servers = [
        RTCIceServer(**normalize_ice_server(s))
        for s in token.ice_servers
    ]
    config = RTCConfiguration(iceServers=ice_servers)

    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    # ─── trickle ICE: send each server candidate to the browser ────────────────
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

    # ─── when a remote audio track arrives, immediately forward live audio ───────
    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            relay_track = relay.subscribe(track)
            for peer_ws, other_pc in state["peers"].items():
                if peer_ws != ws:
                    other_pc.addTrack(relay_track)

    # ─── tell client to start negotiation ──────────────────────────────────────
    await ws.send_json({"type": "ready_for_offer"})

    # ─── receive offer, set it, create & send answer (with gathered ICE) ───────
    offer_msg = await ws.receive_json()
    offer = RTCSessionDescription(sdp=offer_msg["sdp"], type=offer_msg["type"])
    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    # wait for ICE gathering to finish so SDP contains candidates
    while pc.iceGatheringState != "complete":
        await asyncio.sleep(0.1)

    await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})

    # ─── then handle incoming WS messages ──────────────────────────────────────
    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            if typ == "ice":
                # parse candidate dict into RTCIceCandidate
                c = msg["candidate"]
                cand = RTCIceCandidate(
                    candidate=c["candidate"],
                    sdpMid=c.get("sdpMid"),
                    sdpMLineIndex=c.get("sdpMLineIndex"),
                )
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
        # clean up when someone disconnects
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        old_pc = state["peers"].pop(ws, None)
        if old_pc:
            for sender in old_pc.getSenders():
                if sender.track:
                    sender.track.stop()
            await old_pc.close()


# ─── WebSocket entrypoint ──────────────────────────────────────────────────────
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
        # notify any admin
        for admin_ws in state["admins"]:
            await admin_ws.send_json({"type": "new_waiting", "peer_id": id(ws)})
        # block until _admit moves us into peers
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)
