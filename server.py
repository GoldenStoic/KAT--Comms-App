import os
import jwt
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCIceServer,
    RTCConfiguration,
)
from aiortc.contrib.media import MediaRelay
from twilio.rest import Client

# ─── configuration ────────────────────────────────────────────────
JWT_SECRET         = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")

# ─── application setup ─────────────────────────────────────────────
app   = FastAPI()
relay = MediaRelay()
rooms = {}   # room_id → { admins: set, waiting: set, peers: { ws: pc }, audio_tracks: [MediaStreamTrack] }

# ─── helper: fetch ICE servers ───────────────────────────────────────
def get_ice_servers():
    servers = [
        # always include Google’s public STUN
        RTCIceServer(urls=["stun:stun.l.google.com:19302"])
    ]
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        token  = client.tokens.create()
        for s in token.ice_servers:
            servers.append(RTCIceServer(
                urls=s["urls"],
                username=s.get("username"),
                credential=s.get("credential")
            ))
    return servers

# ─── helper: verify JWT ─────────────────────────────────────────────
async def authenticate(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload.get("role", "user")
    except:
        return "user"

# ─── helper: admit a peer (SFU logic) ──────────────────────────────
async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)

    # tell client “you’re admitted, now please do an offer”
    await ws.send_json({"type":"admitted", "peer_id": id(ws)})

    # build our aiortc peer connection with the *real* ICE‐server objects
    config = RTCConfiguration(iceServers=get_ice_servers())
    pc     = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    # whenever *we* gather a local ICE candidate, push it down the WS
    @pc.on("icecandidate")
    async def on_ice(e):
        if e.candidate:
            await ws.send_json({
                "type": "ice",
                "candidate": e.candidate.toJSON()
            })

    # whenever we *receive* an audio track, relay it
    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            relay_track = relay.subscribe(track)
            state["audio_tracks"].append(relay_track)
            # fan-out to everyone else
            for other in state["peers"].values():
                if other is not pc:
                    other.addTrack(relay_track)

    # if there’s already audio in flight, pump that track into the newcomer
    for t in state["audio_tracks"]:
        pc.addTrack(t)

    # now ask client to createOffer()
    await ws.send_json({"type":"ready_for_offer"})


# ─── serve our HTML ────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse("static/index.html")

# ─── WebSocket “signaling + SFU” endpoint ───────────────────────────
@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()

    # 1) figure out if you’re an admin or a user
    token = ws.query_params.get("token","")
    role  = await authenticate(token)

    # 2) “lazy-init” this room’s state
    state = rooms.setdefault(room_id, {
        "admins": set(),
        "waiting": set(),
        "peers": {},
        "audio_tracks": []
    })

    # 3) if you’re the admin, admit yourself & any waiting peers
    if role=="admin":
        state["admins"].add(ws)
        await _admit(ws, room_id)
        for pend in list(state["waiting"]):
            await ws.send_json({"type":"new_waiting","peer_id":id(pend)})

    # 4) otherwise you’re a user → waiting room
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type":"waiting"})
        for a in state["admins"]:
            await a.send_json({"type":"new_waiting","peer_id":id(ws)})
        # block until an admin calls _admit(ws)
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    # ─── main loop: handle messages ────────────────────────────────
    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            if typ=="offer":
                pc = state["peers"][ws]
                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=msg["sdp"], type="offer")
                )
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await ws.send_json({"type":"answer","sdp":pc.localDescription.sdp})

            elif typ=="answer":
                # (not used in this flow)

                pass

            elif typ=="ice":
                # wrap the plain dict in aiortc-expected structure
                pc = state["peers"][ws]
                await pc.addIceCandidate(msg["candidate"])

            elif typ=="chat":
                # fan-out text chat
                for peer_ws in state["peers"]:
                    await peer_ws.send_json(msg)

            elif typ=="admit" and ws in state["admins"]:
                target_id = msg["peer_id"]
                pend = next((w for w in state["waiting"] if id(w)==target_id), None)
                if pend:
                    await _admit(pend, room_id)

            elif typ=="material_event" and ws in state["admins"]:
                for peer_ws in state["peers"]:
                    await peer_ws.send_json(msg)

    except WebSocketDisconnect:
        pass
    finally:
        # cleanup
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws, None)
        if pc:
            await pc.close()

# ─── serve static files ─────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")
