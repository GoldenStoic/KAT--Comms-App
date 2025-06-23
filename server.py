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
    RTCIceCandidate,
    RTCIceServer,
    RTCConfiguration,
)
from aiortc.contrib.media import MediaRelay

print(">>> SERVER running under:", sys.executable)
print(">>> jwt module path:", getattr(jwt, "__file__", None))

# ─── CONFIG ────────────────────────────────────────────────────────────────────

ICE_SERVERS = [
    # Google’s public STUN
    {"urls": ["stun:stun.l.google.com:19302"]},

    # Example Twilio TURN (uncomment & set via ENV)
    # {
    #     "urls": ["turn:global.turn.twilio.com:3478?transport=udp"],
    #     "username": os.getenv("TWILIO_TURN_USER"),
    #     "credential": os.getenv("TWILIO_TURN_PASS"),
    # },
]

JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")

# In-memory room state
rooms = {}            # room_id → { "admins", "waiting", "peers", "audio_tracks" }
relay = MediaRelay()  # for multicasting incoming audio to all peers

# ─── APP SETUP ─────────────────────────────────────────────────────────────────

app = FastAPI()

async def authenticate(token: str):
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        print("🛡️ authenticate:", data)
        return data.get("role", "user")
    except Exception as e:
        print("⚠️ authenticate error:", repr(e))
        return "user"


async def _admit(ws: WebSocket, room_id: str):
    """
    Move `ws` from waiting → peers, send “admitted”,
    wire up RTCPeerConnection, track events, and ask client for an offer.
    """
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    # Build aiortc ICE config
    ice_servers = [
        RTCIceServer(
            urls = srv["urls"],
            username = srv.get("username"),
            credential = srv.get("credential"),
        )
        for srv in ICE_SERVERS
    ]
    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            # relay into SFU
            relay_track = relay.subscribe(track)
            state["audio_tracks"].append(relay_track)
            # forward to all other peers
            for other in state["peers"].values():
                if other is not pc:
                    other.addTrack(relay_track)

    # any existing audio gets forwarded to the newcomer
    for t in state["audio_tracks"]:
        pc.addTrack(t)

    # signal client to createOffer()
    await ws.send_json({"type": "ready_for_offer"})


# ─── ROUTES ────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()

    token = ws.query_params.get("token", "")
    role  = await authenticate(token)
    print(f"[ws] conn room={room_id} role={role!r}")

    state = rooms.setdefault(room_id, {
        "admins": set(),
        "waiting": set(),
        "peers": {},
        "audio_tracks": []
    })

    # ─ Admin auto-admit & see queue
    if role == "admin":
        state["admins"].add(ws)
        print(f"[ws] admitting admin {id(ws)}")
        await _admit(ws, room_id)
        # send any waiting users
        for pending in list(state["waiting"]):
            await ws.send_json({"type": "new_waiting", "peer_id": id(pending)})

    # ─ Users go into waiting room
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type": "waiting"})
        for admin_ws in state["admins"]:
            await admin_ws.send_json({
                "type": "new_waiting",
                "peer_id": id(ws)
            })
        print(f"[ws] user waiting {id(ws)}")

        # block until an admin calls _admit(...)
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)
        print(f"[ws] user admitted {id(ws)}")

    try:
        # ── Signaling loop ────────────────────────────────────────────────
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            if typ == "offer":
                pc = state["peers"][ws]
                offer = RTCSessionDescription(sdp=msg["sdp"], type="offer")
                await pc.setRemoteDescription(offer)
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await ws.send_json({
                    "type": "answer",
                    "sdp": pc.localDescription.sdp
                })

            elif typ == "ice":
                pc = state["peers"][ws]
                c  = msg["candidate"]

                # ── parse the SDP candidate line into the 7 required fields ──
                parts     = c["candidate"].split()
                foundation = parts[0].split(":",1)[1]
                component  = int(parts[1])
                protocol   = parts[2].lower()
                priority   = int(parts[3])
                address    = parts[4]
                port       = int(parts[5])
                cand_type  = parts[7]  # parts[6] == "typ"

                # optional fields
                raddr = None
                if "raddr" in parts:
                    idx   = parts.index("raddr")
                    raddr = parts[idx+1]
                rport = None
                if "rport" in parts:
                    idx   = parts.index("rport")
                    rport = int(parts[idx+1])
                ufrag = None
                if "ufrag" in parts:
                    idx  = parts.index("ufrag")
                    ufrag = parts[idx+1]

                ice = RTCIceCandidate(
                    foundation,
                    component,
                    protocol,
                    priority,
                    address,
                    port,
                    cand_type,
                    # tcpType, relatedAddress, relatedPort, ...
                    relatedAddress = raddr,
                    relatedPort    = rport,
                    sdpMid         = c.get("sdpMid"),
                    sdpMLineIndex  = c.get("sdpMLineIndex"),
                    usernameFragment = ufrag
                )
                await pc.addIceCandidate(ice)

            elif typ == "chat":
                for peer_ws in state["peers"]:
                    await peer_ws.send_json({
                        "type": "chat",
                        "from": msg["from"],
                        "text": msg["text"]
                    })

            elif typ == "admit" and ws in state["admins"]:
                target_id = msg["peer_id"]
                pending = next((w for w in state["waiting"] if id(w)==target_id), None)
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


# serve your static/ under /static
app.mount("/static", StaticFiles(directory="static"), name="static")
