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

# ——— Config from env ———
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN   = os.environ["TWILIO_AUTH_TOKEN"]
JWT_SECRET          = os.environ.get("JWT_SECRET", "change-this-to-a-strong-secret")

# In-memory rooms
rooms = {}
relay = MediaRelay()

app = FastAPI()

# Decode JWT to role
async def authenticate(token: str):
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role", "user")
    except:
        return "user"

# Fetch TURN/STUN servers on-demand
@app.get("/ice_servers")
async def ice_servers():
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Tokens.json"
    resp = requests.post(url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
    data = resp.json()
    return JSONResponse(content=data.get("ice_servers", []))

# Serve index.html
@app.get("/")
async def index():
    return FileResponse("static/index.html")

# WebSocket signalling + SFU
@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role = await authenticate(ws.query_params.get("token",""))
    state = rooms.setdefault(room_id, {
        "admins": set(), "waiting": set(),
        "peers": {},  "audio_tracks": []
    })

    # Admin auto-admit logic
    if role=="admin":
        state["admins"].add(ws)
        await admit(ws, room_id)
        for w in state["waiting"]:
            await ws.send_json({"type":"new_waiting","peer_id":id(w)})
    else:
        # user waits
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
                await pc.setRemoteDescription(RTCSessionDescription(sdp=msg["sdp"], type="offer"))
                ans = await pc.createAnswer()
                await pc.setLocalDescription(ans)
                await ws.send_json({"type":"answer","sdp":pc.localDescription.sdp})

            elif t=="ice":
                pc = state["peers"][ws]
                await pc.addIceCandidate(msg["candidate"])

            elif t=="chat":
                for p in state["peers"]:
                    await p.send_json({"type":"chat","from":msg["from"],"text":msg["text"]})

            elif t=="admit" and ws in state["admins"]:
                pid = msg["peer_id"]
                pending = next((w for w in state["waiting"] if id(w)==pid),None)
                if pending: await admit(pending, room_id)

            elif t=="material_event" and ws in state["admins"]:
                for p in state["peers"]:
                    await p.send_json({
                      "type":"material_event",
                      "event": msg["event"],
                      "payload": msg.get("payload",{})
                    })
    except WebSocketDisconnect:
        pass
    finally:
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws, None)
        if pc: await pc.close()

# Admit helper
async def admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type":"admitted","peer_id":id(ws)})

    # get ICE servers
    ice_list = (await ice_servers()).body.decode()
    ice_servers = JSONResponse.parse_raw(ice_list)

    config = RTCConfiguration([RTCIceServer(**srv) for srv in ice_servers])
    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind=="audio":
            sub = relay.subscribe(track)
            state["audio_tracks"].append(sub)
            for o in state["peers"].values():
                if o is not pc:
                    o.addTrack(sub)

    for t in state["audio_tracks"]:
        pc.addTrack(t)

    await ws.send_json({"type":"ready_for_offer"})

# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")
