# server.py (final version with frame-dropping real-time forwarding)

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
    MediaStreamTrack,
)
from twilio.rest import Client

print(">>> SERVER running under:", sys.executable)
print(">>> jwt module path:", getattr(jwt, "__file__", None))

TW_ACCOUNT_SID    = os.environ["TWILIO_ACCOUNT_SID"]
TW_API_KEY_SID    = os.environ["TWILIO_API_KEY_SID"]
TW_API_KEY_SECRET = os.environ["TWILIO_API_KEY_SECRET"]
twilio_client     = Client(TW_API_KEY_SID, TW_API_KEY_SECRET, TW_ACCOUNT_SID)
initial_token     = twilio_client.tokens.create()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

rooms = {}  # room_id -> {admins, waiting, peers, live_tracks}
JWT_SECRET = os.getenv("JWT_SECRET", "change-this")

def normalize_ice_server(s):
    d = dict(s)
    if "url" in d:
        d["urls"] = [d.pop("url")]
    return d

GLOBAL_ICE = [normalize_ice_server(s) for s in initial_token.ice_servers]

@app.get("/ice")
async def ice():
    return JSONResponse(GLOBAL_ICE)

@app.get("/")
async def index():
    return FileResponse("static/index.html")

async def authenticate(token):
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except:
        return "user"

class ForwardedAudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, source_track):
        super().__init__()
        self.source = source_track
        self._latest_frame = None
        self._queue_task = asyncio.create_task(self._pull_loop())

    async def _pull_loop(self):
        while True:
            try:
                frame = await self.source.recv()
                self._latest_frame = frame
            except Exception:
                break

    async def recv(self):
        while self._latest_frame is None:
            await asyncio.sleep(0.005)
        return self._latest_frame

async def _admit(ws, room_id):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[RTCIceServer(**s) for s in GLOBAL_ICE]))
    state["peers"][ws] = pc

    for sender_id, track in state["live_tracks"].items():
        if id(ws) != sender_id:
            pc.addTrack(ForwardedAudioTrack(track))

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            state["live_tracks"][id(ws)] = track
            for other_ws, other_pc in state["peers"].items():
                if other_pc is not pc:
                    other_pc.addTrack(ForwardedAudioTrack(track))

    await ws.send_json({"type": "ready_for_offer"})

@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role = await authenticate(ws.query_params.get("token", ""))
    state = rooms.setdefault(room_id, {"admins": set(), "waiting": set(), "peers": {}, "live_tracks": {}})

    if role == "admin":
        state["admins"].add(ws)
        await _admit(ws, room_id)
        for pending in state["waiting"]:
            await ws.send_json({"type": "new_waiting", "peer_id": id(pending)})
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type": "waiting"})
        for adm in state["admins"]:
            await adm.send_json({"type": "new_waiting", "peer_id": id(ws)})
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            if typ == "offer":
                pc = state["peers"][ws]
                await pc.setRemoteDescription(RTCSessionDescription(sdp=msg["sdp"], type="offer"))
                answer = await pc.createAnswer()

                lines = answer.sdp.splitlines()
                new_lines = []
                for line in lines:
                    new_lines.append(line)
                    if line.startswith("m=audio"):
                        new_lines.append("a=sendrecv")
                        new_lines.append("a=rtpmap:111 opus/48000/2")
                        new_lines.append("a=fmtp:111 minptime=10;useinbandfec=1;stereo=0;maxaveragebitrate=20000;usedtx=1")
                patched_sdp = "\r\n".join(new_lines) + "\r\n"

                await pc.setLocalDescription(RTCSessionDescription(sdp=patched_sdp, type="answer"))
                await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})

            elif typ == "ice":
                pc = state["peers"][ws]
                c = msg["candidate"]
                parts = c["candidate"].split()
                cand = RTCIceCandidate(
                    foundation=parts[0].split(":", 1)[1],
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
                for peer in state["peers"]:
                    await peer.send_json({"type": "chat", "from": msg["from"], "text": msg["text"]})

            elif typ == "admit" and ws in state["admins"]:
                pending = next((w for w in state["waiting"] if id(w) == msg["peer_id"]), None)
                if pending:
                    await _admit(pending, room_id)

            elif typ == "material_event" and ws in state["admins"]:
                for peer in state["peers"]:
                    await peer.send_json({"type": "material_event", "event": msg["event"], "payload": msg.get("payload", {})})

    except WebSocketDisconnect:
        pass
    finally:
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws, None)
        state["live_tracks"].pop(id(ws), None)
        if pc:
            for sender in pc.getSenders():
                if sender.track:
                    sender.track.stop()
            await pc.close()
