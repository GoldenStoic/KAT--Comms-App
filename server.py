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
    RTCConfiguration
)
from aiortc.contrib.media import MediaRelay
from twilio.rest import Client as TwilioClient

print(">>> SERVER running under:", sys.executable)
print(">>> jwt module path:", getattr(jwt, "__file__", None))

# ─── CONFIG ────────────────────────────────────────────────────────────────────
JWT_SECRET          = os.getenv("JWT_SECRET", "change-this-in-prod")
TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_TTL_SECONDS  = int(os.getenv("TWILIO_TTL_SECONDS", "86400"))

# verify Twilio env vars
if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
    print("⚠️  Missing TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN in environment")

twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

ICE_SERVERS_FALLBACK = [
    # fallback STUN if Twilio fails
    RTCIceServer(urls="stun:stun.l.google.com:19302"),
]

# In-memory room state
rooms = {}  # room_id → {admins:set, waiting:set, peers:dict, audio_tracks:list}
relay = MediaRelay()

app = FastAPI()


# ───  Fetch a fresh STUN+TURN list from Twilio ────────────────────────────────
@app.get("/ice")
async def get_ice():
    """
    Returns JSON:
      { "iceServers": [ { urls, username?, credential? }, … ] }
    """
    try:
        token = twilio.tokens.create(ttl=TWILIO_TTL_SECONDS)
        servers = token.ice_servers  # list of dicts: {urls, username?, credential?}
    except Exception as e:
        print("⚠️  Twilio NTS fetch failed:", e)
        # fallback to pure STUN
        servers = [{"urls": srv.urls} for srv in ICE_SERVERS_FALLBACK]

    return JSONResponse({"iceServers": servers})


# ─── Helper: decode JWT ───────────────────────────────────────────────────────
async def authenticate(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload.get("role", "user")
    except Exception:
        return "user"


# ─── Helper: admit a peer into the SFU ────────────────────────────────────────
async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    # build aiortc RTCConfiguration from the last‐fetched servers
    raw = (await get_ice()).body  # JSONResponse.body is bytes
    ice_cfg = JSONResponse(raw).media  # hack to parse JSONResponse
    ice_servers = []
    for srv in ice_cfg["iceServers"]:
        ice_servers.append(RTCIceServer(
            urls       = srv["urls"],
            username   = srv.get("username"),
            credential = srv.get("credential")
        ))
    pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            r = relay.subscribe(track)
            state["audio_tracks"].append(r)
            for other in state["peers"].values():
                if other is not pc:
                    other.addTrack(r)

    # replay any existing audio into the new peer
    for t in state["audio_tracks"]:
        pc.addTrack(t)

    # kick off negotiation
    await ws.send_json({"type": "ready_for_offer"})


# ─── 1) Serve index.html ─────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse("static/index.html")


# ─── 2) WebSocket signalling & SFU ────────────────────────────────────────────
@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    token = ws.query_params.get("token", "")
    role  = await authenticate(token)
    state = rooms.setdefault(room_id, {
        "admins": set(), "waiting": set(),
        "peers": {},  "audio_tracks": []
    })

    if role == "admin":
        state["admins"].add(ws)
        await _admit(ws, room_id)
        # let the admin see everyone waiting
        for pending in state["waiting"]:
            await ws.send_json({"type":"new_waiting","peer_id":id(pending)})
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type":"waiting"})
        for a in state["admins"]:
            await a.send_json({"type":"new_waiting","peer_id":id(ws)})
        # block until the admin admits them
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    try:
        while True:
            msg = await ws.receive_json()
            t   = msg.get("type")
            if t == "offer":
                pc = state["peers"][ws]
                offer = RTCSessionDescription(sdp=msg["sdp"], type="offer")
                await pc.setRemoteDescription(offer)
                ans = await pc.createAnswer()
                await pc.setLocalDescription(ans)
                await ws.send_json({"type":"answer","sdp":pc.localDescription.sdp})

            elif t == "ice":
                # some clients send candidate as dict
                c = msg["candidate"]
                pc = state["peers"][ws]
                await pc.addIceCandidate(c)

            elif t == "chat":
                for p in state["peers"]:
                    await p.send_json({"type":"chat","from":msg["from"],"text":msg["text"]})

            elif t == "admit" and ws in state["admins"]:
                peer_id = msg["peer_id"]
                pend = next((w for w in state["waiting"] if id(w)==peer_id),None)
                if pend:
                    await _admit(pend, room_id)

            elif t == "material_event" and ws in state["admins"]:
                for p in state["peers"]:
                    await p.send_json({"type":"material_event",
                                        "event":msg["event"],
                                        "payload":msg.get("payload",{})})

    except WebSocketDisconnect:
        pass
    finally:
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws, None)
        if pc:
            await pc.close()


# ─── 3) Static files ─────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")
