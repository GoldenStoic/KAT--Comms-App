import os
import jwt
import asyncio
import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCIceServer,
    RTCConfiguration,
)
from aiortc.contrib.media import MediaRelay

# ——— CONFIG ———
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN   = os.environ["TWILIO_AUTH_TOKEN"]
JWT_SECRET          = os.environ.get("JWT_SECRET", "change-this-to-a-strong-secret")

rooms = {}          # room_id → {admins, waiting, peers, audio_tracks}
relay = MediaRelay()

app = FastAPI()


# ——— Twilio helper ———
def fetch_raw_ice():
    """Sync helper to call Twilio.  We normalize below."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Tokens.json"
    r   = requests.post(url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
    return r.json().get("ice_servers", [])


async def fetch_ice_servers():
    """Return a list of RTCIceServer instances with proper `urls` key."""
    raw = await asyncio.get_event_loop().run_in_executor(None, fetch_raw_ice)
    out = []
    for s in raw:
        # Twilio sometimes returns "url" (string) or "urls" (list)
        urls = s.get("urls") or ([s["url"]] if "url" in s else [])
        out.append(
            RTCIceServer(
                urls=urls,
                username=s.get("username"),
                credential=s.get("credential")
            )
        )
    return out


@app.get("/ice_servers")
async def ice_servers_route():
    servers = await fetch_ice_servers()
    # return in JSON form so client can pass straight into RTCPeerConnection
    return JSONResponse(content=[srv.__dict__ for srv in servers])


# ——— Serve index ———
@app.get("/")
async def index():
    return FileResponse("static/index.html")


# ——— JWT auth ———
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
    await ws.send_json({"type":"admitted","peer_id":id(ws)})

    ice_servers = await fetch_ice_servers()
    config      = RTCConfiguration(iceServers=ice_servers)

    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            sub = relay.subscribe(track)
            state["audio_tracks"].append(sub)
            for other in state["peers"].values():
                if other is not pc:
                    other.addTrack(sub)

    # forward any existing
    for t in state["audio_tracks"]:
        pc.addTrack(t)

    await ws.send_json({"type":"ready_for_offer"})


# ——— WebSocket endpoint ———
@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role  = await authenticate(ws.query_params.get("token",""))
    state = rooms.setdefault(room_id, {
        "admins": set(), "waiting": set(),
        "peers": {},   "audio_tracks": []
    })

    if role == "admin":
        state["admins"].add(ws)
        await admit(ws, room_id)
        for w in state["waiting"]:
            await ws.send_json({"type":"new_waiting","peer_id":id(w)})
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
            t   = msg.get("type")

            if t == "offer":
                pc = state["peers"][ws]
                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=msg["sdp"], type="offer")
                )
                ans = await pc.createAnswer()
                await pc.setLocalDescription(ans)
                await ws.send_json({"type":"answer","sdp":pc.localDescription.sdp})

            elif t == "ice":
                pc = state["peers"][ws]
                await pc.addIceCandidate(msg["candidate"])

            elif t == "chat":
                for p in state["peers"]:
                    await p.send_json({
                        "type":"chat",
                        "from":msg["from"],
                        "text":msg["text"]
                    })

            elif t == "admit" and ws in state["admins"]:
                pid = msg["peer_id"]
                target = next((u for u in state["waiting"] if id(u)==pid), None)
                if target:
                    await admit(target, room_id)

            elif t == "material_event" and ws in state["admins"]:
                for p in state["peers"]:
                    await p.send_json({
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
            await pc.close()


# ——— Static files ———
app.mount("/static", StaticFiles(directory="static"), name="static")
