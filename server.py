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
TW_ACCOUNT_SID    = os.environ["TWILIO_ACCOUNT_SID"]
TW_API_KEY_SID    = os.environ["TWILIO_API_KEY_SID"]
TW_API_KEY_SECRET = os.environ["TWILIO_API_KEY_SECRET"]
twilio_client     = Client(TW_API_KEY_SID, TW_API_KEY_SECRET, TW_ACCOUNT_SID)

# ─── JWT CONFIG ───────────────────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")

# ─── In-memory room state ─────────────────────────────────────────────────────
rooms = {}  # room_id → { admins:set, waiting:set, peers:dict }
relay = MediaRelay()

app = FastAPI()


def normalize_ice_server(s: dict) -> dict:
    d = dict(s)
    if "url" in d:
        # Twilio sometimes returns `url` instead of `urls`
        if "urls" not in d:
            d["urls"] = [d.pop("url")]
        else:
            d.pop("url")
    return d


@app.get("/ice")
async def ice():
    token   = twilio_client.tokens.create()
    servers = [normalize_ice_server(s) for s in token.ice_servers]
    return JSONResponse(servers)


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")


async def authenticate(token: str):
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except:
        return "user"


async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    # Fetch fresh TURN creds
    token = twilio_client.tokens.create()
    ice_servers = [RTCIceServer(**normalize_ice_server(s)) for s in token.ice_servers]

    # **Force TURN-only** so we never try STUN or host candidates.
    config = RTCConfiguration(
        iceServers=ice_servers,
        iceTransportPolicy="relay"
    )

    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    # Relay incoming audio tracks out immediately (no buffering)
    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            relay_track = relay.subscribe(track)
            for other in state["peers"].values():
                if other is not pc:
                    other.addTrack(relay_track)

    # **Trickle ICE**: send every TURN candidate back to the browser
    @pc.on("icecandidate")
    async def on_icecandidate(candidate):
        if candidate:
            await ws.send_json({
                "type": "ice",
                "candidate": {
                    "candidate": candidate.to_sdp(),
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex
                }
            })

    # Let the client know it can start its offer
    await ws.send_json({"type": "ready_for_offer"})


@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role = await authenticate(ws.query_params.get("token", ""))
    state = rooms.setdefault(
        room_id,
        {"admins": set(), "waiting": set(), "peers": {}}
    )

    if role == "admin":
        state["admins"].add(ws)
        await _admit(ws, room_id)
        for pending in state["waiting"]:
            await ws.send_json({"type":"new_waiting","peer_id":id(pending)})
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type":"waiting"})
        for admin_ws in state["admins"]:
            await admin_ws.send_json({"type":"new_waiting","peer_id":id(ws)})
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            if typ == "offer":
                pc = state["peers"][ws]
                # Set the incoming offer…
                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=msg["sdp"], type="offer")
                )
                # …then create+set our answer (ICE gathering will trickle)
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await ws.send_json({
                    "type":"answer",
                    "sdp": pc.localDescription.sdp
                })

            elif typ == "ice":
                pc = state["peers"][ws]
                c = msg["candidate"]
                candidate = RTCIceCandidate(
                    candidate=c["candidate"],
                    sdpMid=c.get("sdpMid"),
                    sdpMLineIndex=c.get("sdpMLineIndex")
                )
                await pc.addIceCandidate(candidate)

            elif typ == "chat":
                for peer in state["peers"]:
                    await peer.send_json({
                        "type":"chat","from":msg["from"],"text":msg["text"]
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
                        "event": msg["event"],
                        "payload": msg.get("payload", {})
                    })

    except WebSocketDisconnect:
        pass
    finally:
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws, None)
        if pc:
            await pc.close()
