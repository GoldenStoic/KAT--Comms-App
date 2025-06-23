import os
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
    RTCIceCandidate,
)
from aiortc.contrib.media import MediaRelay
import requests

# ——— CONFIG ———
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN   = os.environ["TWILIO_AUTH_TOKEN"]
JWT_SECRET          = os.environ.get("JWT_SECRET", "change-this-to-a-strong-secret")

rooms = {}      # room_id → {admins, waiting, peers, audio_tracks}
relay = MediaRelay()

app = FastAPI()


# ——— Twilio → aiortc ICE servers ———
def _fetch_twilio():
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Tokens.json"
    r = requests.post(url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
    r.raise_for_status()
    return r.json().get("ice_servers", [])

async def get_ice_servers():
    raw = await asyncio.get_event_loop().run_in_executor(None, _fetch_twilio)
    servers = []
    for srv in raw:
        # Twilio sometimes returns "url" or "urls"
        urls = srv.get("urls") or ([srv["url"]] if srv.get("url") else [])
        servers.append(RTCIceServer(
            urls=urls,
            username=srv.get("username"),
            credential=srv.get("credential"),
        ))
    return servers

@app.get("/ice_servers")
async def ice_servers_route():
    servers = await get_ice_servers()
    return [s.__dict__ for s in servers]


# ——— Serve client ———
@app.get("/")
async def index():
    return FileResponse("static/index.html")

app.mount("/static", StaticFiles(directory="static"), name="static")


# ——— JWT helper ———
async def authenticate(token: str):
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except:
        return "user"


# ——— Admit helper ———
async def admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type": "admitted", "peer_id": id(ws)})

    ice_servers = await get_ice_servers()
    config      = RTCConfiguration(iceServers=ice_servers)
    pc          = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            sub = relay.subscribe(track)
            state["audio_tracks"].append(sub)
            for other in state["peers"].values():
                if other is not pc:
                    other.addTrack(sub)

    # Forward any existing audio to the new peer
    for t in state["audio_tracks"]:
        pc.addTrack(t)

    # Tell client to kick off offer
    await ws.send_json({"type": "ready_for_offer"})


# ——— WebSocket endpoint ———
@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role  = await authenticate(ws.query_params.get("token", ""))
    state = rooms.setdefault(room_id, {
        "admins": set(), "waiting": set(),
        "peers": {},   "audio_tracks": []
    })

    # Admins join immediately
    if role == "admin":
        state["admins"].add(ws)
        await admit(ws, room_id)
        # let the admin see who’s already waiting
        for w in state["waiting"]:
            await ws.send_json({"type": "new_waiting", "peer_id": id(w)})

    # Users wait until an admin admits
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type": "waiting"})
        for a in state["admins"]:
            await a.send_json({"type": "new_waiting", "peer_id": id(ws)})
        # busy-wait until admitted
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    try:
        while True:
            msg = await ws.receive_json()
            t   = msg.get("type")

            # 1) SDP offer from client
            if t == "offer":
                pc = state["peers"][ws]
                offer = RTCSessionDescription(sdp=msg["sdp"], type="offer")
                await pc.setRemoteDescription(offer)
                ans = await pc.createAnswer()
                await pc.setLocalDescription(ans)
                await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})

            # 2) ICE candidate from client
            elif t == "ice":
                pc = state["peers"][ws]
                c  = msg["candidate"]
                # aiortc wants an RTCIceCandidate object, not a raw dict
                ice = RTCIceCandidate(
                    c["sdpMid"],
                    c["sdpMLineIndex"],
                    c["candidate"]
                )
                await pc.addIceCandidate(ice)

            # 3) Text chat
            elif t == "chat":
                for p in state["peers"]:
                    await p.send_json({
                        "type": "chat",
                        "from": msg["from"],
                        "text": msg["text"]
                    })

            # 4) Admin admits a user
            elif t == "admit" and ws in state["admins"]:
                peer_id = msg["peer_id"]
                target = next((u for u in state["waiting"] if id(u) == peer_id), None)
                if target:
                    await admit(target, room_id)

            # 5) Admin broadcasts material events
            elif t == "material_event" and ws in state["admins"]:
                for p in state["peers"]:
                    await p.send_json({
                        "type": "material_event",
                        "event": msg["event"],
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
