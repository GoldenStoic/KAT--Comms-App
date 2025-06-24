# server.py

import os
import sys
import asyncio
import jwt

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCIceCandidate,
    RTCIceServer,
    RTCConfiguration,
)
from aiortc.contrib.media import MediaRelay
from twilio.rest import Client

print(">>> SERVER running under:", sys.executable)
print(">>> jwt module path:", getattr(jwt, "__file__", None))

# ─── Twilio creds ─────────────────────────────────────────────────────────────
TW_ACCOUNT_SID    = os.environ["TWILIO_ACCOUNT_SID"]
TW_API_KEY_SID    = os.environ["TWILIO_API_KEY_SID"]
TW_API_KEY_SECRET = os.environ["TWILIO_API_KEY_SECRET"]
twilio_client = Client(TW_API_KEY_SID, TW_API_KEY_SECRET, TW_ACCOUNT_SID)

# ─── JWT config ────────────────────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")

# ─── In-memory room state & media relay ────────────────────────────────────────
rooms = {}  # room_id → { admins:set, waiting:set, peers:dict, audio_tracks:list }
relay = MediaRelay()

# ─── App setup ───────────────────────────────────────────────────────────────────
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


# ─── Helpers ────────────────────────────────────────────────────────────────────
async def safe_send(ws: WebSocket, message: dict):
    try:
        await ws.send_json(message)
    except (WebSocketDisconnect, RuntimeError):
        pass

def normalize_ice_server(s: dict) -> dict:
    d = dict(s)
    if "url" in d and "urls" not in d:
        d["urls"] = [d.pop("url")]
    return d

def parse_candidate(c: dict) -> RTCIceCandidate:
    parts = c["candidate"].split()
    return RTCIceCandidate(
        foundation=parts[0].split(":",1)[1],
        component=int(parts[1]),
        protocol=parts[2].lower(),
        priority=int(parts[3]),
        ip=parts[4],
        port=int(parts[5]),
        type=parts[7],
        sdpMid=c.get("sdpMid"),
        sdpMLineIndex=c.get("sdpMLineIndex"),
    )

async def authenticate(token: str) -> str:
    if token in ("admin","user"):
        return token
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role","user")
    except:
        return "user"


# ─── ICE endpoint ────────────────────────────────────────────────────────────────
@app.get("/ice")
async def ice():
    token = twilio_client.tokens.create()
    servers = [RTCIceServer(**normalize_ice_server(s)) for s in token.ice_servers]
    return JSONResponse({"iceServers":[srv.__dict__ for srv in servers]})


# ─── Serve client ───────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse("static/index.html")


# ─── Admit helper ────────────────────────────────────────────────────────────────
async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await safe_send(ws, {"type":"admitted","peer_id":id(ws)})

    token = twilio_client.tokens.create()
    ice_servers = [RTCIceServer(**normalize_ice_server(s)) for s in token.ice_servers]
    pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
    state["peers"][ws] = pc

    @pc.on("icecandidate")
    async def on_icecandidate(event):
        if event.candidate:
            await safe_send(ws, {
                "type":"ice",
                "candidate": event.candidate.to_sdp(),
                "sdpMid": event.candidate.sdpMid,
                "sdpMLineIndex": event.candidate.sdpMLineIndex
            })

    @pc.on("track")
    def on_track(track):
        relay.subscribe(track)
        for peer_ws, peer_pc in list(state["peers"].items()):
            if peer_ws is not ws:
                peer_pc.addTrack(relay.subscribe(track))

    await safe_send(ws, {"type":"ready_for_offer"})

    # ← only change: patch SDP to force 20 ms ptime for low latency
    offer = await ws.receive_json()
    desc = RTCSessionDescription(sdp=offer["sdp"], type=offer["type"])
    await pc.setRemoteDescription(desc)

    answer = await pc.createAnswer()
    lines, out = answer.sdp.splitlines(), []
    for L in lines:
        out.append(L)
        if L.startswith("m=audio"):
            out += ["a=sendrecv","a=ptime:20","a=maxptime:20"]
    patched_sdp = "\r\n".join(out) + "\r\n"
    patched = RTCSessionDescription(sdp=patched_sdp, type="answer")

    await pc.setLocalDescription(patched)

    # wait ICE gather and send
    while pc.iceGatheringState != "complete":
        await asyncio.sleep(0.1)
    await safe_send(ws, {"type":"answer","sdp":pc.localDescription.sdp})

    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            if typ == "ice":
                cand = parse_candidate(msg["candidate"])
                try: await pc.addIceCandidate(cand)
                except: pass

            elif typ == "chat":
                for peer_ws in list(state["peers"]):
                    await safe_send(peer_ws, msg)

            elif typ == "admit" and ws in state["admins"]:
                pending = next((p for p in state["waiting"] if id(p)==msg["peer_id"]), None)
                if pending:
                    await _admit(pending, room_id)

            elif typ == "material_event" and ws in state["admins"]:
                for peer_ws in list(state["peers"]):
                    await safe_send(peer_ws, msg)

    except WebSocketDisconnect:
        pass
    finally:
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        pc = state["peers"].pop(ws, None)
        if pc:
            for sender in pc.getSenders():
                if sender.track: sender.track.stop()
            await pc.close()


# ─── WebSocket entry point ──────────────────────────────────────────────────────
@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role = await authenticate(ws.query_params.get("token",""))
    state = rooms.setdefault(room_id, {"admins":set(),"waiting":set(),"peers":{}})

    if role=="admin":
        state["admins"].add(ws)
        await _admit(ws, room_id)
        for pending in state["waiting"]:
            await safe_send(ws, {"type":"new_waiting","peer_id":id(pending)})

    else:
        state["waiting"].add(ws)
        await safe_send(ws, {"type":"waiting"})
        for adm in state["admins"]:
            await safe_send(adm, {"type":"new_waiting","peer_id":id(ws)})

        while ws not in state["peers"]:
            await asyncio.sleep(0.1)
        # then the `_admit` above already ran for this ws, and the loop in `_admit` handles rest
