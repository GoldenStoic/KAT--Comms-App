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

# ─── Fetch exactly one TURN+STUN token at startup ─────────────────────────────
initial_token = twilio_client.tokens.create()

def normalize_ice_server(s: dict) -> dict:
    d = dict(s)
    if "url" in d:
        # promote single 'url' to list 'urls'
        if "urls" not in d:
            d["urls"] = [d.pop("url")]
        else:
            d.pop("url")
    return d

# build a single global ICE‐server list once
GLOBAL_ICE = [ normalize_ice_server(s) for s in initial_token.ice_servers ]

# ─── FastAPI app setup ────────────────────────────────────────────────────────
app = FastAPI()
relay = MediaRelay()
rooms = {}  # room_id → { admins:set, waiting:set, peers:dict, audio_tracks:list }
JWT_SECRET = os.getenv("JWT_SECRET", "CHANGE_THIS")

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/ice")
async def ice():
    # serve same ICE config to every browser client
    return JSONResponse(GLOBAL_ICE)

@app.get("/")
async def index():
    return FileResponse("static/index.html")

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

    # use the single GLOBAL_ICE for aiortc
    rtc_ice_servers = [ RTCIceServer(**s) for s in GLOBAL_ICE ]
    config = RTCConfiguration(iceServers=rtc_ice_servers)

    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            relay_track = relay.subscribe(track)
            state["audio_tracks"].append(relay_track)
            for other_pc in state["peers"].values():
                if other_pc is not pc:
                    other_pc.addTrack(relay_track)

    # forward any already-subscribed live tracks
    for t in state["audio_tracks"]:
        pc.addTrack(t)

    await ws.send_json({"type": "ready_for_offer"})

@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role = await authenticate(ws.query_params.get("token", ""))
    state = rooms.setdefault(
        room_id,
        {"admins": set(), "waiting": set(), "peers": {}, "audio_tracks": []}
    )

    if role == "admin":
        state["admins"].add(ws)
        await _admit(ws, room_id)
        for pending in state["waiting"]:
            await ws.send_json({"type":"new_waiting","peer_id":id(pending)})
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type":"waiting"})
        for adm in state["admins"]:
            await adm.send_json({"type":"new_waiting","peer_id":id(ws)})
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            if typ == "offer":
                pc = state["peers"][ws]
                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=msg["sdp"], type="offer")
                )
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await ws.send_json({"type":"answer","sdp":pc.localDescription.sdp})

            elif typ == "ice":
                pc = state["peers"][ws]
                c  = msg["candidate"]
                parts = c["candidate"].split()
                cand = RTCIceCandidate(
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
                try: await pc.addIceCandidate(cand)
                except: pass

            elif typ == "chat":
                for peer in state["peers"]:
                    await peer.send_json({
                        "type":"chat","from":msg["from"],"text":msg["text"]
                    })

            elif typ == "admit" and ws in state["admins"]:
                pending = next((w for w in state["waiting"] if id(w)==msg["peer_id"]), None)
                if pending: await _admit(pending, room_id)

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
            for sender in pc.getSenders():
                if sender.track: sender.track.stop()
            await pc.close()
