import os
import sys
import asyncio
import jwt

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from aiortc import (
    RTCPeerConnection,
    RTCConfiguration,
    RTCSessionDescription,
    RTCIceCandidate,
    RTCIceServer,
)
from aiortc.contrib.media import MediaRelay
from twilio.rest import Client as TwilioClient

print(">>> server.py running under:", sys.executable)

# ─── Configuration ─────────────────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")
TW_ACCOUNT_SID    = os.environ["TWILIO_ACCOUNT_SID"]
TW_API_KEY_SID    = os.environ["TWILIO_API_KEY_SID"]
TW_API_KEY_SECRET = os.environ["TWILIO_API_KEY_SECRET"]

# ─── Twilio ICE setup ───────────────────────────────────────────────────────────
twilio = TwilioClient(TW_API_KEY_SID, TW_API_KEY_SECRET, TW_ACCOUNT_SID)
token  = twilio.tokens.create()

GLOBAL_ICE = []
for s in token.ice_servers:
    d = dict(s)
    if "url" in d:
        # normalize to 'urls'
        if "urls" not in d:
            d["urls"] = [d.pop("url")]
        else:
            d.pop("url")
    GLOBAL_ICE.append(d)

# ─── FastAPI & media relay ───────────────────────────────────────────────────────
app   = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
relay = MediaRelay()
rooms = {}  # room_id → { admins:set, waiting:set, peers:dict, audio_tracks:list }


# ─── Helpers ─────────────────────────────────────────────────────────────────────
async def safe_send(ws: WebSocket, msg: dict):
    """Swallow any send errors if socket is closed."""
    try:
        await ws.send_json(msg)
    except:
        pass


async def authenticate(token: str) -> str:
    """
    Return 'admin' or 'user' if literally passed, else decode JWT.
    """
    if token in ("admin", "user"):
        return token
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except:
        return "user"


def parse_ice_candidate(line: str) -> RTCIceCandidate:
    """
    Convert a single ICE candidate SDP line into an RTCIceCandidate.
    """
    parts = line.strip().split()
    component = int(parts[1].split("=")[1])
    foundation = parts[2].split("=")[1]
    priority = int(parts[3].split("=")[1])
    ip = parts[4].split("=")[1]
    port = int(parts[5].split("=")[1])
    typ = parts[6].split("=")[1]
    protocol = parts[7].split("=")[1]
    relatedAddress = None
    relatedPort = None
    tcpType = None
    i = 8
    while i < len(parts):
        if parts[i] == "raddr":
            relatedAddress = parts[i + 1]
            i += 2
        elif parts[i] == "rport":
            relatedPort = int(parts[i + 1])
            i += 2
        elif parts[i] == "tcptype":
            tcpType = parts[i + 1]
            i += 2
        else:
            i += 1

    c = {
        "component": component,
        "foundation": foundation,
        "priority": priority,
        "ip": ip,
        "port": port,
        "protocol": protocol,
        "type": typ,
        "relatedAddress": relatedAddress,
        "relatedPort": relatedPort,
        "sdpMid": None,
        "sdpMLineIndex": None,
        "tcpType": tcpType,
    }
    return RTCIceCandidate(**c)


async def _admit(ws: WebSocket, room_id: str):
    """
    Start WebRTC handshake for a peer:
    - send 'admitted' & 'ready_for_offer'
    - build PeerConnection (TURN-only)
    - relay all existing live audio tracks to them
    - then loop receiving offer/ice/chat
    """
    state = rooms[room_id]
    state["waiting"].discard(ws)

    # 1) notify admitted
    await safe_send(ws, {"type": "admitted", "peer_id": id(ws)})
    await safe_send(ws, {"type": "ready_for_offer"})

    # 2) build PeerConnection
    rtc_ice = [RTCIceServer(**d) for d in GLOBAL_ICE]
    config = RTCConfiguration(iceServers=rtc_ice)
    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    # 3) relay existing live audio
    for t in state["audio_tracks"]:
        pc.addTrack(t)

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            relay_track = relay.subscribe(track)
            state["audio_tracks"].append(relay_track)
            for other_ws, other_pc in state["peers"].items():
                if other_ws is not ws:
                    other_pc.addTrack(relay_track)

    try:
        # 4) signaling loop for this peer
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            if typ == "offer":
                # handle remote offer → low-latency answer
                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=msg["sdp"], type="offer")
                )
                answer = await pc.createAnswer()
                lines, patched = answer.sdp.splitlines(), []
                for L in lines:
                    patched.append(L)
                    if L.startswith("m=audio"):
                        patched += ["a=sendrecv", "a=ptime:20", "a=maxptime:20"]
                patched_sdp = "\r\n".join(patched) + "\r\n"
                await pc.setLocalDescription(
                    RTCSessionDescription(sdp=patched_sdp, type="answer")
                )
                await safe_send(ws, {"type": "answer", "sdp": pc.localDescription.sdp})

            elif typ == "ice":
                cand = parse_ice_candidate(msg["candidate"])
                try:
                    await pc.addIceCandidate(cand)
                except:
                    pass

            elif typ == "chat":
                # broadcast chat to all peers
                for peer_ws in state["peers"]:
                    await safe_send(peer_ws, msg)

    except WebSocketDisconnect:
        pass
    finally:
        # cleanup peer
        state["peers"].pop(ws, None)
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        if pc:
            for sender in pc.getSenders():
                if sender.track:
                    sender.track.stop()
            await pc.close()


# ─── HTTP endpoints ─────────────────────────────────────────────────────────────
@app.get("/ice")
async def ice():
    # return raw array; client expects an array
    return JSONResponse(GLOBAL_ICE)


@app.get("/")
async def index():
    return FileResponse("static/index.html")


# ─── WebSocket endpoint ─────────────────────────────────────────────────────────
@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role = await authenticate(ws.query_params.get("token", ""))

    state = rooms.setdefault(
        room_id,
        {"admins": set(), "waiting": set(), "peers": {}, "audio_tracks": []},
    )

    if role == "admin":
        # Admin flow: no _admit; handle its own receive loop
        state["admins"].add(ws)
        # notify of current waiting peers
        for pending in state["waiting"]:
            await safe_send(ws, {"type": "new_waiting", "peer_id": id(pending)})

        try:
            while True:
                msg = await ws.receive_json()
                t = msg.get("type")

                if t == "admit":
                    peer_id = msg.get("peer_id")
                    pending = next(
                        (p for p in state["waiting"] if id(p) == peer_id), None
                    )
                    if pending:
                        # admit in-place (no nested receive)
                        await _admit(pending, room_id)

                elif t == "material_event":
                    for peer_ws in state["peers"]:
                        await safe_send(peer_ws, msg)

                elif t == "chat":
                    for peer_ws in state["peers"]:
                        await safe_send(peer_ws, msg)

        except WebSocketDisconnect:
            pass
        finally:
            state["admins"].discard(ws)

    else:
        # Peer flow: queue and notify admins, then wait for admit
        state["waiting"].add(ws)
        await safe_send(ws, {"type": "waiting"})
        for adm in state["admins"]:
            await safe_send(adm, {"type": "new_waiting", "peer_id": id(ws)})

        # block until an admin admits
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

        # once admitted, enter the peer handshake/loop
        await _admit(ws, room_id)
