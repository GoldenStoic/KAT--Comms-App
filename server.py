import os
import jwt
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration
from aiortc.contrib.media import MediaRelay
from twilio.rest import Client

# ————— Environment —————
JWT_SECRET         = os.environ["JWT_SECRET"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN  = os.environ["TWILIO_AUTH_TOKEN"]

# ————— Twilio client —————
twilio = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

async def get_ice_servers():
    """Mint a fresh STUN+TURN credential set from Twilio."""
    token = twilio.tokens.create()
    return token.ice_servers  # list of {"urls", "username", "credential"}

# ————— App & state —————
app   = FastAPI()
relay = MediaRelay()
rooms = {}  # room_id → { admins:set, waiting:set, peers:dict(ws→pc), audio_tracks:list }

# ————— Static files & index —————
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def index():
    return FileResponse("static/index.html")

@app.get("/ice_servers")
async def ice_servers():
    """Return Twilio ICE servers to the client."""
    return { "iceServers": await get_ice_servers() }

# ————— JWT auth —————
async def authenticate(token: str):
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except Exception:
        return "user"

# ————— Admit helper —————
async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    # Pull a fresh STUN+TURN list, build the PC
    ice_servers = await get_ice_servers()
    config      = RTCConfiguration(iceServers=ice_servers)
    pc          = RTCPeerConnection(configuration=config)

    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            relay_track = relay.subscribe(track)
            state["audio_tracks"].append(relay_track)
            # forward to every other peer
            for other in state["peers"].values():
                if other is not pc:
                    other.addTrack(relay_track)

    # give this newcomer all existing relayed tracks
    for t in state["audio_tracks"]:
        pc.addTrack(t)

    # signal client to createOffer()
    await ws.send_json({"type": "ready_for_offer"})

# ————— WebSocket endpoint —————
@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    token = ws.query_params.get("token","")
    role  = await authenticate(token)

    # bootstrap room
    state = rooms.setdefault(room_id, {
        "admins": set(), "waiting": set(),
        "peers": {},    "audio_tracks": []
    })

    # admin auto-admit
    if role == "admin":
        state["admins"].add(ws)
        await _admit(ws, room_id)
        # tell this admin about any currently waiting users
        for pending in state["waiting"]:
            await ws.send_json({"type":"new_waiting","peer_id":id(pending)})

    # normal user → waiting room
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type":"waiting"})
        for admin_ws in state["admins"]:
            await admin_ws.send_json({
                "type":"new_waiting","peer_id":id(ws)
            })
        # block until admin calls _admit()
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    # ——— main receive loop ———
    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            if typ == "offer":
                pc    = state["peers"][ws]
                offer = RTCSessionDescription(sdp=msg["sdp"], type="offer")
                await pc.setRemoteDescription(offer)
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await ws.send_json({
                    "type":"answer","sdp":pc.localDescription.sdp
                })

            elif typ == "ice":
                # just pass the dict straight through:
                pc = state["peers"][ws]
                await pc.addIceCandidate(msg["candidate"])

            elif typ == "chat":
                for peer in state["peers"]:
                    await peer.send_json({
                        "type":"chat",
                        "from":msg["from"],
                        "text":msg["text"]
                    })

            elif typ == "admit" and ws in state["admins"]:
                # manual admit button from admin
                target_id = msg["peer_id"]
                pending = next(
                  (w for w in state["waiting"] if id(w)==target_id),
                  None
                )
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
        # cleanup
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws, None)
        if pc:
            await pc.close()
