# server.py
import os
import sys
import asyncio
import jwt
import json

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
TW_ACCOUNT_SID     = os.getenv("TWILIO_ACCOUNT_SID")
TW_API_KEY_SID     = os.getenv("TWILIO_API_KEY_SID")
TW_API_KEY_SECRET  = os.getenv("TWILIO_API_KEY_SECRET")
if not (TW_ACCOUNT_SID and TW_API_KEY_SID and TW_API_KEY_SECRET):
    raise RuntimeError("Twilio credentials not set in environment")

twilio_client = Client(TW_API_KEY_SID, TW_API_KEY_SECRET, TW_ACCOUNT_SID)

# ─── JWT CONFIG ───────────────────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")

# ─── In-memory room state ─────────────────────────────────────────────────────
rooms = {}  # room_id → { admins, waiting, peers }
relay = MediaRelay()

app = FastAPI()

# ─── Utility to normalize Twilio ice_server dict ──────────────────────────────
def normalize_ice_server(s: dict) -> dict:
    d = dict(s)
    if "url" in d:
        if "urls" not in d:
            d["urls"] = [d.pop("url")]
        else:
            d.pop("url")
    return d

# ─── ICE endpoint for clients to fetch Twilio STUN/TURN ────────────────────────
@app.get("/ice")
async def ice():
    token = twilio_client.tokens.create()
    servers = [normalize_ice_server(s) for s in token.ice_servers]
    return JSONResponse(servers)

# ─── Serve the client page ────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse("static/index.html")

app.mount("/static", StaticFiles(directory="static"), name="static")

# ─── JWT authentication helper ────────────────────────────────────────────────
async def authenticate(token: str):
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        print("🛡️ authenticate:", data)
        return data.get("role", "user")
    except Exception as e:
        print("⚠️ authenticate error:", repr(e))
        return "user"

# ─── Admit helper: set up PeerConnection without buffering old audio ──────────
async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    # fetch fresh Twilio ICE servers
    token = twilio_client.tokens.create()
    rtc_ice_servers = []
    for s in token.ice_servers:
        srv = normalize_ice_server(s)
        rtc_ice_servers.append(RTCIceServer(**srv))
    config = RTCConfiguration(iceServers=rtc_ice_servers)

    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            relay_track = relay.subscribe(track)
            # forward only new audio to existing peers
            for other_pc in state["peers"].values():
                if other_pc is not pc:
                    other_pc.addTrack(relay_track)

    # signal client ready for its offer
    await ws.send_json({"type": "ready_for_offer"})

# ─── WebSocket signaling endpoint ─────────────────────────────────────────────
@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role = await authenticate(ws.query_params.get("token", ""))
    print(f"[ws] conn room={room_id} role={role}")

    state = rooms.setdefault(
        room_id,
        {"admins": set(), "waiting": set(), "peers": {}}
    )

    # admin path
    if role == "admin":
        state["admins"].add(ws)
        print(f"[ws] admitting admin {id(ws)}")
        await _admit(ws, room_id)
        for pending in state["waiting"]:
            await ws.send_json({"type":"new_waiting","peer_id":id(pending)})
    else:
        # user path
        state["waiting"].add(ws)
        await ws.send_json({"type":"waiting"})
        for admin_ws in state["admins"]:
            await admin_ws.send_json({"type":"new_waiting","peer_id":id(ws)})
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)
        print(f"[ws] user admitted {id(ws)}")

    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            if typ == "offer":
                pc = state["peers"][ws]
                offer = RTCSessionDescription(sdp=msg["sdp"], type="offer")
                await pc.setRemoteDescription(offer)
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await ws.send_json({"type":"answer","sdp":pc.localDescription.sdp})

            elif typ == "ice":
                pc = state["peers"][ws]
                c = msg["candidate"]
                parts = c["candidate"].split()
                foundation = parts[0].split(":",1)[1]
                component  = int(parts[1])
                protocol   = parts[2].lower()
                priority   = int(parts[3])
                ip         = parts[4]
                port       = int(parts[5])
                cand_type  = parts[7]

                ice = RTCIceCandidate(
                    foundation=foundation,
                    component=component,
                    protocol=protocol,
                    priority=priority,
                    ip=ip,
                    port=port,
                    type=cand_type,
                    sdpMid=c.get("sdpMid"),
                    sdpMLineIndex=c.get("sdpMLineIndex")
                )
                try:
                    await pc.addIceCandidate(ice)
                except Exception:
                    pass

            elif typ == "chat":
                for peer in state["peers"]:
                    await peer.send_json({
                        "type":"chat", "from":msg["from"], "text":msg["text"]
                    })

            elif typ == "admit" and ws in state["admins"]:
                target = msg["peer_id"]
                pending = next((w for w in state["waiting"] if id(w)==target), None)
                if pending:
                    await _admit(pending, room_id)

            elif typ == "material_event" and ws in state["admins"]:
                for peer in state["peers"]:
                    await peer.send_json({
                        "type":"material_event",
                        "event":msg["event"],
                        "payload":msg.get("payload",{})
                    })

    except WebSocketDisconnect:
        pass

    finally:
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws, None)
        if pc:
            # stop all local senders to free buffers
            for sender in pc.getSenders():
                if sender.track:
                    sender.track.stop()
            await pc.close()
        print(f"[ws] closed {id(ws)}")
