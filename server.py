import os, sys, asyncio, jwt
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

print(">>> server.py running under:", sys.executable)
print(">>> jwt module path:", getattr(jwt, "__file__", None))

# ─── Twilio Network Traversal Setup ───────────────────────────────────────────
TW_ACCOUNT_SID    = os.environ["TWILIO_ACCOUNT_SID"]
TW_API_KEY_SID    = os.environ["TWILIO_API_KEY_SID"]
TW_API_KEY_SECRET = os.environ["TWILIO_API_KEY_SECRET"]
twilio_client     = Client(TW_API_KEY_SID, TW_API_KEY_SECRET, TW_ACCOUNT_SID)

# fetch ICE servers once
initial_token = twilio_client.tokens.create()
GLOBAL_ICE = []
for s in initial_token.ice_servers:
    d = dict(s)
    if "url" in d:
        if "urls" not in d:
            d["urls"] = [d.pop("url")]
        else:
            d.pop("url")
    GLOBAL_ICE.append(d)

# ─── FastAPI & aiortc state ───────────────────────────────────────────────────
app        = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
relay      = MediaRelay()
rooms      = {}   # room_id → { admins:set, waiting:set, peers:dict, audio_tracks:list }
JWT_SECRET = os.getenv("JWT_SECRET", "change-this")

@app.get("/ice")
async def ice():
    # serve the raw list (client treats it as an Array)
    return JSONResponse(GLOBAL_ICE)

@app.get("/")
async def index():
    return FileResponse("static/index.html")

async def authenticate(token: str):
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except:
        # allow literal "admin"/"user" for testing
        return token if token in ("admin", "user") else "user"

async def _admit(ws: WebSocket, room_id: str):
    """
    Called when admitting a peer. Builds their RTCPeerConnection,
    subscribes future audio, and sends the initial signals.
    """
    state = rooms[room_id]
    state["waiting"].discard(ws)

    # notify client
    await ws.send_json({"type":"admitted","peer_id":id(ws)})

    # build PeerConnection with TURN-only ICE
    rtc_ice = [ RTCIceServer(**s) for s in GLOBAL_ICE ]
    config  = RTCConfiguration(iceServers=rtc_ice)
    pc      = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    # ——— ADD EXISTING LIVE TRACKS TO NEW PEER ———
    for t in state["audio_tracks"]:
        pc.addTrack(t)
    # ———————————————————————————————

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            # relay only _new_ live audio
            relay_track = relay.subscribe(track)
            state["audio_tracks"].append(relay_track)
            for other_pc in state["peers"].values():
                if other_pc is not pc:
                    other_pc.addTrack(relay_track)

    # tell client to send offer
    await ws.send_json({"type":"ready_for_offer"})

    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            if typ == "offer":
                # handle offer → low-latency patched answer
                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=msg["sdp"], type="offer")
                )
                answer = await pc.createAnswer()
                lines, out = answer.sdp.splitlines(), []
                for L in lines:
                    out.append(L)
                    if L.startswith("m=audio"):
                        out += ["a=sendrecv","a=ptime:20","a=maxptime:20"]
                patched = "\r\n".join(out) + "\r\n"
                await pc.setLocalDescription(
                    RTCSessionDescription(sdp=patched, type="answer")
                )
                await ws.send_json({"type":"answer","sdp":pc.localDescription.sdp})

            elif typ == "ice":
                # ICE candidate from peer
                c = msg["candidate"]
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
                try:
                    await pc.addIceCandidate(cand)
                except:
                    pass

            elif typ == "chat":
                # broadcast chat to all peers
                for peer in state["peers"]:
                    await peer.send_json(msg)

            elif typ == "admit" and ws in state["admins"]:
                # admin admitting another peer
                pending = next(
                    (p for p in state["waiting"] if id(p)==msg["peer_id"]),
                    None
                )
                if pending:
                    await _admit(pending, room_id)

            elif typ == "material_event" and ws in state["admins"]:
                # broadcast material events
                for peer in state["peers"]:
                    await peer.send_json(msg)

    except WebSocketDisconnect:
        pass
    finally:
        # cleanup
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws, None)
        if pc:
            for sender in pc.getSenders():
                if sender.track:
                    sender.track.stop()
            await pc.close()

@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role  = await authenticate(ws.query_params.get("token",""))
    state = rooms.setdefault(
        room_id,
        {"admins":set(), "waiting":set(), "peers":{}, "audio_tracks":[]}
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

        # block until admin admits → then full handshake
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)
        await _admit(ws, room_id)
