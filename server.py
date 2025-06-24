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

print(">>> SERVER running under:", sys.executable)
print(">>> jwt module path:", getattr(jwt, "__file__", None))

# ─── Twilio Network Traversal Setup ───────────────────────────────────────────
TW_ACCOUNT_SID    = os.environ["TWILIO_ACCOUNT_SID"]
TW_API_KEY_SID    = os.environ["TWILIO_API_KEY_SID"]
TW_API_KEY_SECRET = os.environ["TWILIO_API_KEY_SECRET"]
twilio_client     = Client(TW_API_KEY_SID, TW_API_KEY_SECRET, TW_ACCOUNT_SID)

# grab one token at startup
initial_token = twilio_client.tokens.create()

def normalize_ice_server(s: dict) -> dict:
    d = dict(s)
    if "url" in d:
        if "urls" not in d:
            d["urls"] = [d.pop("url")]
        else:
            d.pop("url")
    return d

GLOBAL_ICE = [ normalize_ice_server(s) for s in initial_token.ice_servers ]

# ─── FastAPI & aiortc state ───────────────────────────────────────────────────
app        = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
relay      = MediaRelay()
rooms      = {}   # room_id → { admins:set, waiting:set, peers:dict }
JWT_SECRET = os.getenv("JWT_SECRET", "change-this")

@app.get("/ice")
async def ice():
    # clients fetch this once
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
    await ws.send_json({"type":"admitted","peer_id":id(ws)})

    # build the aiortc PeerConnection with TURN-only ICE
    rtc_ice = [ RTCIceServer(**s) for s in GLOBAL_ICE ]
    config  = RTCConfiguration(iceServers=rtc_ice)
    pc      = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            # only new live tracks get relayed from here on
            relay_track = relay.subscribe(track)
            for other_pc in state["peers"].values():
                if other_pc is not pc:
                    other_pc.addTrack(relay_track)

    await ws.send_json({"type":"ready_for_offer"})

    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            if typ == "offer":
                # set remote, create patched answer, send it
                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=msg["sdp"], type="offer")
                )
                answer = await pc.createAnswer()
                # patch SDP unchanged from your existing server.py
                lines = answer.sdp.splitlines()
                new_lines = []
                for line in lines:
                    new_lines.append(line)
                    if line.startswith("m=audio"):
                        new_lines.append("a=sendrecv")
                        new_lines.append("a=ptime:20")
                        new_lines.append("a=maxptime:20")
                patched_sdp = "\r\n".join(new_lines) + "\r\n"

                await pc.setLocalDescription(
                    RTCSessionDescription(sdp=patched_sdp, type="answer")
                )
                await ws.send_json({"type":"answer","sdp":pc.localDescription.sdp})

            elif typ == "ice":
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
                for peer in state["peers"]:
                    await peer.send_json({
                        "type":"chat","from":msg["from"],"text":msg["text"]
                    })

            elif typ == "admit" and ws in state["admins"]:
                pending = next((w for w in state["waiting"] if id(w)==msg["peer_id"]), None)
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
        {"admins":set(), "waiting":set(), "peers":{}}
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

        # wait until admitted: then do full handshake
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)
        await _admit(ws, room_id)
