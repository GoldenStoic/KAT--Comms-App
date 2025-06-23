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

# --- CONFIG ---
ICE_SERVERS = [
    {"urls": os.getenv("STUN_URL", "stun:stun.l.google.com:19302")},
    {"urls": os.getenv("TURN_URL")},
    {"urls": os.getenv("TURN_URL")},  # you can add multiple TURN entries
]
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")

# In-memory room state
rooms = {}  # room_id → {admins, waiting, peers, audio_tracks}
relay = MediaRelay()

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
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    # configure ICE
    rtc_ice_servers = [RTCIceServer(**s) for s in ICE_SERVERS if s.get("urls")]
    config = RTCConfiguration(iceServers=rtc_ice_servers)

    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            relay_track = relay.subscribe(track)
            state["audio_tracks"].append(relay_track)
            for other in state["peers"].values():
                if other is not pc:
                    other.addTrack(relay_track)

    # forward existing audio
    for t in state["audio_tracks"]:
        pc.addTrack(t)

    await ws.send_json({"type": "ready_for_offer"})


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    token = ws.query_params.get("token", "")
    role = await authenticate(token)
    print(f"[ws] conn room={room_id} role={role}")

    state = rooms.setdefault(
        room_id,
        {"admins": set(), "waiting": set(), "peers": {}, "audio_tracks": []},
    )

    # Admins get instant admit; users wait
    if role == "admin":
        state["admins"].add(ws)
        print(f"[ws] admitting admin {id(ws)}")
        await _admit(ws, room_id)
        # notify admin of any waiting users
        for pending in state["waiting"]:
            await ws.send_json({"type": "new_waiting", "peer_id": id(pending)})
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type": "waiting"})
        for admin_ws in state["admins"]:
            await admin_ws.send_json({"type": "new_waiting", "peer_id": id(ws)})
        print(f"[ws] user waiting {id(ws)}")
        # block until admitted
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
                # **Fixed parsing of ICE candidates**
                pc = state["peers"][ws]
                c = msg["candidate"]
                # candidate string is like "candidate:<Foundation> <component> udp <priority> <address> <port> typ <type> ..."
                parts = c["candidate"].split()
                foundation = parts[1]
                component = int(parts[2])
                protocol = parts[3].lower()
                priority = int(parts[4])
                address = parts[5]
                port = int(parts[6])
                # parts[7] == "typ", parts[8] == "<type>"
                cand_type = parts[8]

                ice = RTCIceCandidate(
                    foundation,
                    component,
                    protocol,
                    priority,
                    address,
                    port,
                    cand_type,
                    sdpMid=c.get("sdpMid"),
                    sdpMLineIndex=c.get("sdpMLineIndex"),
                )
                # swallow errors if the transport has closed
                try:
                    await pc.addIceCandidate(ice)
                except Exception:
                    pass

            elif typ == "chat":
                # broadcast chat to all peers
                for peer in state["peers"]:
                    await peer.send_json(
                        {"type": "chat", "from": msg["from"], "text": msg["text"]}
                    )

            elif typ == "admit" and ws in state["admins"]:
                # admin admits a pending user
                target = msg["peer_id"]
                pending = next((w for w in state["waiting"] if id(w) == target), None)
                if pending:
                    await _admit(pending, room_id)

            elif typ == "material_event" and ws in state["admins"]:
                # broadcast material events
                for peer in state["peers"]:
                    await peer.send_json(
                        {
                            "type": "material_event",
                            "event": msg["event"],
                            "payload": msg.get("payload", {}),
                        }
                    )

    except WebSocketDisconnect:
        pass

    finally:
        # cleanup on disconnect
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws, None)
        if pc:
            await pc.close()
        print(f"[ws] closed {id(ws)}")


# serve your static client
app.mount("/static", StaticFiles(directory="static"), name="static")
