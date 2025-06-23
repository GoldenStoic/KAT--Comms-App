import os
import sys
import jwt
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCIceServer,
    RTCConfiguration,
)
from aiortc.contrib.media import MediaRelay
from twilio.rest import Client

print(">>> SERVER running under:", sys.executable)
print(">>> jwt module path:", getattr(jwt, "__file__", None))

# ── Environment ─────────────────────────────────────────────
JWT_SECRET        = os.environ["JWT_SECRET"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN  = os.environ["TWILIO_AUTH_TOKEN"]

# ── Globals ─────────────────────────────────────────────────
rooms = {}  # room_id → { admins:set, waiting:set, peers:dict, audio_tracks:list }
relay = MediaRelay()
app = FastAPI()

# ── Helper: Fetch ICE servers from Twilio ───────────────────
def fetch_twilio_ice_servers():
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    token = client.tokens.create()  # Twilio returns { ice_servers: [ { urls, username, credential } ] }
    return token.ice_servers

# ── HTTP endpoint for the client to fetch ICE config ───────
@app.get("/ice_servers")
async def ice_servers():
    return JSONResponse({ "iceServers": fetch_twilio_ice_servers() })

# ── Serve index.html ───────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse("static/index.html")

# ── WebSocket & SFU signaling ──────────────────────────────
async def authenticate(token: str):
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except:
        return "user"

async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type":"admitted","peer_id":id(ws)})

    # build aiortc config from the SAME Twilio ICE list
    ice_servers = [RTCIceServer(**{
        "urls": srv.get("urls"),
        "username": srv.get("username"),
        "credential": srv.get("credential")
    }) for srv in fetch_twilio_ice_servers()]

    pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            relay_track = relay.subscribe(track)
            state["audio_tracks"].append(relay_track)
            for other in state["peers"].values():
                if other is not pc:
                    other.addTrack(relay_track)

    # forward existing audio to newcomer
    for t in state["audio_tracks"]:
        pc.addTrack(t)

    await ws.send_json({"type":"ready_for_offer"})

@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    token = ws.query_params.get("token","")
    role  = await authenticate(token)
    state = rooms.setdefault(room_id,{
        "admins":set(), "waiting":set(),
        "peers":{},   "audio_tracks":[]
    })

    if role == "admin":
        state["admins"].add(ws)
        await _admit(ws, room_id)
        # notify about existing waiters
        for pend in state["waiting"]:
            await ws.send_json({"type":"new_waiting","peer_id":id(pend)})
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type":"waiting"})
        for a in state["admins"]:
            await a.send_json({"type":"new_waiting","peer_id":id(ws)})
        # block until admitted
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")
            if typ == "offer":
                pc = state["peers"][ws]
                offer = RTCSessionDescription(sdp=msg["sdp"],type="offer")
                await pc.setRemoteDescription(offer)
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await ws.send_json({"type":"answer","sdp":pc.localDescription.sdp})

            elif typ == "ice":
                pc = state["peers"][ws]
                # NOTE: browser will send the candidate JSON directly
                await pc.addIceCandidate(msg["candidate"])

            elif typ == "chat":
                for p in state["peers"]:
                    await p.send_json({
                        "type":"chat","from":msg["from"],"text":msg["text"]
                    })

            elif typ == "admit" and ws in state["admins"]:
                target_id = msg["peer_id"]
                pend = next((w for w in state["waiting"] if id(w)==target_id), None)
                if pend:
                    await _admit(pend, room_id)

            elif typ == "material_event" and ws in state["admins"]:
                for p in state["peers"]:
                    await p.send_json({
                        "type":"material_event",
                        "event":msg["event"],
                        "payload":msg.get("payload",{})
                    })

    except WebSocketDisconnect:
        pass
    finally:
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws,None)
        if pc:
            await pc.close()

# ── Mount static files ─────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")
