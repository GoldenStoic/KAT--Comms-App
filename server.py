import sys
import asyncio
import jwt
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
from aiortc.sdp import candidate_from_sdp

# Debug: verify Python & PyJWT
print(">>> SERVER running under:", sys.executable)
print(">>> jwt module path:", getattr(jwt, "__file__", None))

# --- CONFIG ---
ICE_SERVERS = [
    {"urls": ["stun:stun.l.google.com:19302"]},
    {"urls": ["stun:global.stun.twilio.com:3478"]},
]
JWT_SECRET = "change-this-to-a-strong-secret"

# In-memory room state
rooms = {}  # room_id → {"admins", "waiting", "peers", "audio_tracks"}
relay = MediaRelay()

app = FastAPI()


async def authenticate(token: str):
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        print("🛡️ authenticate:", data)
        return data.get("role", "user")
    except Exception as e:
        print("⚠️ authenticate error:", e)
        return "user"


async def _admit(ws: WebSocket, room_id: str):
    """
    Remove `ws` from waiting, notify it, create a PeerConnection,
    hook up RTP relay, and ask it to send an offer.
    """
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    # Build aiortc configuration
    rtc_ice_servers = [RTCIceServer(**s) for s in ICE_SERVERS]
    config = RTCConfiguration(iceServers=rtc_ice_servers)
    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            # relay incoming audio into our mixer
            r = relay.subscribe(track)
            state["audio_tracks"].append(r)
            # broadcast to everyone else
            for other in state["peers"].values():
                if other is not pc:
                    other.addTrack(r)

    # ship any already-live tracks to the newcomer
    for t in state["audio_tracks"]:
        pc.addTrack(t)

    # now tell the client to kick off createOffer()
    await ws.send_json({"type": "ready_for_offer"})


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    token = ws.query_params.get("token", "")
    role = await authenticate(token)
    print(f"[ws] conn room={room_id} role={role!r}")

    state = rooms.setdefault(room_id, {
        "admins": set(), "waiting": set(),
        "peers": {}, "audio_tracks": []
    })

    # Admins get straight in, see who's waiting
    if role == "admin":
        state["admins"].add(ws)
        print(f"[ws] admitting admin {id(ws)}")
        await _admit(ws, room_id)
        # let them know who is waiting
        for w in state["waiting"]:
            await ws.send_json({"type": "new_waiting", "peer_id": id(w)})

    # Normal users go to the lobby
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type": "waiting"})
        for a in state["admins"]:
            await a.send_json({"type": "new_waiting", "peer_id": id(ws)})
        print(f"[ws] user waiting {id(ws)}")
        # block until an admin admits
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
                cand = msg["candidate"]
                # parse the SDP line into a full RTCIceCandidate
                ice = candidate_from_sdp(cand["candidate"])
                ice.sdpMid = cand.get("sdpMid")
                ice.sdpMLineIndex = cand.get("sdpMLineIndex")
                await pc.addIceCandidate(ice)

            elif typ == "chat":
                for peer in state["peers"]:
                    await peer.send_json({
                        "type": "chat",
                        "from": msg.get("from"),
                        "text": msg.get("text")
                    })

            elif typ == "admit" and ws in state["admins"]:
                target = msg.get("peer_id")
                pending = next((w for w in state["waiting"] if id(w) == target), None)
                if pending:
                    await _admit(pending, room_id)

            elif typ == "material_event" and ws in state["admins"]:
                for peer in state["peers"]:
                    await peer.send_json({
                        "type": "material_event",
                        "event": msg.get("event"),
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
        print(f"[ws] closed {id(ws)}")


# serve `static/` from `/static`
app.mount("/static", StaticFiles(directory="static"), name="static")
