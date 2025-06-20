import os
import sys
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

# Debug info
print(">>> SERVER running under:", sys.executable)
print(">>> jwt module path:", getattr(jwt, "__file__", None))

# --- CONFIG ---
# Pull TURN credentials from env; set these on Render under Environment
TURN_URL        = os.getenv("TURN_URL", "turn:your.turn.addr:443?transport=tcp")
TURN_USERNAME   = os.getenv("TURN_USERNAME", "turnuser")
TURN_CREDENTIAL = os.getenv("TURN_CREDENTIAL", "turnpass")

ICE_SERVERS = [
    { "urls": "stun:stun.l.google.com:19302" },
    {
        "urls": TURN_URL,
        "username": TURN_USERNAME,
        "credential": TURN_CREDENTIAL
    }
]
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")

# In-memory room state
rooms = {}  # room_id → { admins, waiting, peers, audio_tracks }
relay = MediaRelay()

app = FastAPI()


async def authenticate(token: str):
    """Decode JWT and return 'admin' or 'user'."""
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        print("🛡️ authenticate:", data)
        return data.get("role", "user")
    except Exception as e:
        print("⚠️ authenticate error:", repr(e))
        return "user"


async def _admit(ws: WebSocket, room_id: str):
    """Mark ws as admitted, create PeerConnection, wire up tracks, and ask for an offer."""
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    # Build RTCConfiguration with our TURN+STUN servers
    rtc_ice_servers = [RTCIceServer(**entry) for entry in ICE_SERVERS]
    config = RTCConfiguration(iceServers=rtc_ice_servers)

    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            # Relay into our SFU mesh
            relay_track = relay.subscribe(track)
            state["audio_tracks"].append(relay_track)
            for other in state["peers"].values():
                if other is not pc:
                    other.addTrack(relay_track)

    # For any already-mounted audio, forward it to the newcomer
    for t in state["audio_tracks"]:
        pc.addTrack(t)

    # Tell the client to send us an SDP offer
    await ws.send_json({"type": "ready_for_offer"})


# 1) Serve index.html
@app.get("/")
async def index():
    return FileResponse("static/index.html")


# 2) WebSocket SFU endpoint
@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()

    token = ws.query_params.get("token", "")
    role  = await authenticate(token)
    print(f"[ws] conn room={room_id} role={role}")

    state = rooms.setdefault(room_id, {
        "admins": set(), "waiting": set(),
        "peers": {}, "audio_tracks": []
    })

    if role == "admin":
        state["admins"].add(ws)
        print(f"[ws] admitting admin {id(ws)}")
        await _admit(ws, room_id)
        # Notify admin of existing waiters
        for pending in state["waiting"]:
            await ws.send_json({"type": "new_waiting", "peer_id": id(pending)})
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type": "waiting"})
        # Notify all admins of this new waiter
        for admin_ws in state["admins"]:
            await admin_ws.send_json({"type": "new_waiting", "peer_id": id(ws)})
        print(f"[ws] user waiting {id(ws)}")
        # Block until admitted
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
                await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})

            elif typ == "ice":
                pc = state["peers"][ws]
                await pc.addIceCandidate(msg["candidate"])

            elif typ == "chat":
                for peer_ws in state["peers"]:
                    await peer_ws.send_json({
                        "type": "chat",
                        "from": msg["from"],
                        "text": msg["text"]
                    })

            elif typ == "admit" and ws in state["admins"]:
                target_id = msg["peer_id"]
                pending = next((w for w in state["waiting"] if id(w) == target_id), None)
                if pending:
                    await _admit(pending, room_id)

            elif typ == "material_event" and ws in state["admins"]:
                for peer_ws in state["peers"]:
                    await peer_ws.send_json({
                        "type": "material_event",
                        "event": msg["event"],
                        "payload": msg.get("payload", {})
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
        print(f"[ws] closed {id(ws)}")


# 3) Static files
app.mount("/static", StaticFiles(directory="static"), name="static")
