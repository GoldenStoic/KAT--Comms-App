import os, sys, asyncio, jwt
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
from twilio.rest import Client as TwilioClient

# debug
print(">>> SERVER Python:", sys.executable)
print(">>> jwt path:", getattr(jwt, "__file__", None))

# env vars
JWT_SECRET           = os.getenv("JWT_SECRET", "change-this-secret")
TWILIO_ACCOUNT_SID   = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN    = os.environ["TWILIO_AUTH_TOKEN"]

# Twilio client
twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# In‐memory rooms
rooms = {}  # room_id -> { admins, waiting, peers, audio_tracks, pending_ice }
relay = MediaRelay()

@app.get("/")
async def index():
    return FileResponse("static/index.html")

@app.get("/token")
async def ice_token():
    # return fresh Twilio ICE servers (STUN+TURN)
    token = twilio.tokens.create()
    return {"iceServers": token.ice_servers}

async def authenticate(token: str):
    try:
        data = jwt.decode(token or "", JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except:
        return "user"

async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type":"admitted","peer_id":id(ws)})

    # PeerConnection (server uses only STUN; TURN is only for clients)
    ice_servers = [RTCIceServer(urls="stun:stun.l.google.com:19302")]
    pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            relay_track = relay.subscribe(track)
            state["audio_tracks"].append(relay_track)
            for other in state["peers"].values():
                if other is not pc:
                    other.addTrack(relay_track)

    for t in state["audio_tracks"]:
        pc.addTrack(t)

    await ws.send_json({"type":"ready_for_offer"})

@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role = await authenticate(ws.query_params.get("token",""))
    state = rooms.setdefault(room_id, {
        "admins":set(),"waiting":set(),
        "peers":{}, "audio_tracks":[], "pending_ice":{}
    })

    if role=="admin":
        state["admins"].add(ws)
        await _admit(ws, room_id)
        for pend in state["waiting"]:
            await ws.send_json({"type":"new_waiting","peer_id":id(pend)})
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type":"waiting"})
        for a in state["admins"]:
            await a.send_json({"type":"new_waiting","peer_id":id(ws)})
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    try:
        while True:
            msg = await ws.receive_json()
            t = msg.get("type")

            if t=="offer":
                pc = state["peers"][ws]
                offer = RTCSessionDescription(sdp=msg["sdp"],type="offer")
                await pc.setRemoteDescription(offer)
                # drain queued ICE
                for ice in state["pending_ice"].pop(ws,[]):
                    await pc.addIceCandidate(ice)
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await ws.send_json({"type":"answer","sdp":pc.localDescription.sdp})

            elif t=="ice":
                c = msg["candidate"]
                ice = candidate_from_sdp(c["candidate"])
                ice.sdpMid        = c.get("sdpMid")
                ice.sdpMLineIndex = c.get("sdpMLineIndex")
                pc = state["peers"].get(ws)
                if not pc or not pc.remoteDescription:
                    state["pending_ice"].setdefault(ws,[]).append(ice)
                else:
                    await pc.addIceCandidate(ice)

            elif t=="chat":
                for peer in state["peers"]:
                    await peer.send_json({
                        "type":"chat","from":msg["from"],"text":msg["text"]
                    })

            elif t=="admit" and ws in state["admins"]:
                target = next((w for w in state["waiting"] if id(w)==msg["peer_id"]),None)
                if target: await _admit(target,room_id)

            elif t=="material_event" and ws in state["admins"]:
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
        pc = state["peers"].pop(ws,None)
        if pc: await pc.close()
        print("closed",id(ws))
