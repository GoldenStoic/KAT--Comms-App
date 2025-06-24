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

# ─── Config ─────────────────────────────────────────────────────────────────────
JWT_SECRET         = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")
TW_ACCOUNT_SID     = os.environ["TWILIO_ACCOUNT_SID"]
TW_API_KEY_SID     = os.environ["TWILIO_API_KEY_SID"]
TW_API_KEY_SECRET  = os.environ["TWILIO_API_KEY_SECRET"]

# ─── Twilio ICE setup ───────────────────────────────────────────────────────────
twilio = TwilioClient(TW_API_KEY_SID, TW_API_KEY_SECRET, TW_ACCOUNT_SID)

# ─── FastAPI & media relay ───────────────────────────────────────────────────────
app   = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
relay = MediaRelay()
rooms = {}  # room_id → { admins:set, waiting:set, peers:dict }


# ─── Helpers ─────────────────────────────────────────────────────────────────────
async def safe_send(ws: WebSocket, msg: dict):
    try:
        await ws.send_json(msg)
    except:
        pass

async def authenticate(token: str) -> str:
    if token in ("admin","user"):
        return token
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data.get("role","user")
    except:
        return "user"

def normalize_ice_server(spec) -> dict:
    d = spec.to_dict() if hasattr(spec,"to_dict") else dict(spec)
    if "url" in d and "urls" not in d:
        d["urls"] = [d.pop("url")]
    allowed = {}
    if "urls" in d:       allowed["urls"]       = d["urls"]
    if "username" in d:   allowed["username"]   = d["username"]
    if "credential" in d: allowed["credential"] = d["credential"]
    return allowed

def parse_ice_candidate(line: str) -> RTCIceCandidate:
    parts = line.strip().split()
    return RTCIceCandidate(
        component      = int(parts[1].split("=")[1]),
        foundation     = parts[2].split("=")[1],
        priority       = int(parts[3].split("=")[1]),
        ip             = parts[4].split("=")[1],
        port           = int(parts[5].split("=")[1]),
        protocol       = parts[7].split("=")[1],
        type           = parts[6].split("=")[1],
        relatedAddress = None,
        relatedPort    = None,
        sdpMid         = None,
        sdpMLineIndex  = None,
        tcpType        = None,
    )


# ─── ICE endpoint ────────────────────────────────────────────────────────────────
@app.get("/ice")
async def ice():
    token = twilio.tokens.create()
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

    # build PeerConnection with fresh ICE
    token = twilio.tokens.create()
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
                "sdpMLineIndex": event.candidate.sdpMLineIndex,
            })

    @pc.on("track")
    def on_track(track):
        relay.subscribe(track)
        for peer_ws, peer_pc in list(state["peers"].items()):
            if peer_ws is not ws:
                peer_pc.addTrack(relay.subscribe(track))

    # tell client to send us an offer
    await safe_send(ws, {"type":"ready_for_offer"})

    # ─── WAIT FOR THE OFFER MESSAGE ────────────────────────────────────────────
    offer = None
    while True:
        msg = await ws.receive_json()
        if msg.get("type") == "offer":
            offer = msg
            break
        # you can handle other types here if needed (e.g. chat from admin)
    # ─────────────────────────────────────────────────────────────────────────────

    # process the offer
    desc = RTCSessionDescription(sdp=offer["sdp"], type=offer["type"])
    await pc.setRemoteDescription(desc)

    # create low-latency answer
    answer = await pc.createAnswer()
    lines, patched = answer.sdp.splitlines(), []
    for L in lines:
        patched.append(L)
        if L.startswith("m=audio"):
            patched += ["a=sendrecv","a=ptime:20","a=maxptime:20"]
    patched_sdp = "\r\n".join(patched) + "\r\n"
    await pc.setLocalDescription(RTCSessionDescription(patched_sdp, "answer"))
    await safe_send(ws, {"type":"answer","sdp":pc.localDescription.sdp})

    # now loop on ICE / chat / admit / material
    try:
        while True:
            msg = await ws.receive_json()
            t = msg.get("type")

            if t == "ice":
                cand = parse_ice_candidate(msg["candidate"])
                try: await pc.addIceCandidate(cand)
                except: pass

            elif t == "chat":
                for peer in state["peers"]:
                    await safe_send(peer, msg)

            elif t == "admit" and ws in state["admins"]:
                pending = next((p for p in state["waiting"] if id(p)==msg["peer_id"]), None)
                if pending:
                    asyncio.create_task(_admit(pending, room_id))

            elif t == "material_event" and ws in state["admins"]:
                for peer in state["peers"]:
                    await safe_send(peer, msg)

    except WebSocketDisconnect:
        pass
    finally:
        state["admins"].discard(ws)
        state["waiting"].discard(ws)
        old_pc = state["peers"].pop(ws, None)
        if old_pc:
            for sender in old_pc.getSenders():
                if sender.track: sender.track.stop()
            await old_pc.close()


# ─── WebSocket entry point ──────────────────────────────────────────────────────
@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role = await authenticate(ws.query_params.get("token",""))
    state = rooms.setdefault(room_id, {
        "admins": set(), "waiting": set(), "peers": {}
    })

    if role == "admin":
        state["admins"].add(ws)
        # don’t call _admit for the admin—only notify them about waiting users
        for pending in state["waiting"]:
            await safe_send(ws, {"type":"new_waiting","peer_id":id(pending)})

        try:
            while True:
                msg = await ws.receive_json()
                if msg.get("type") == "admit":
                    pending = next((p for p in state["waiting"] if id(p)==msg["peer_id"]), None)
                    if pending:
                        asyncio.create_task(_admit(pending, room_id))
                elif msg.get("type") == "chat":
                    for peer in state["peers"]:
                        await safe_send(peer, msg)
                elif msg.get("type") == "material_event":
                    for peer in state["peers"]:
                        await safe_send(peer, msg)
        except WebSocketDisconnect:
            pass
        finally:
            state["admins"].discard(ws)

    else:
        # user joins
        state["waiting"].add(ws)
        await safe_send(ws, {"type":"waiting"})
        for adm in state["admins"]:
            await safe_send(adm, {"type":"new_waiting","peer_id":id(ws)})

        # now just sit here—once an admin admits, _admit() will take over this ws
        await asyncio.Event().wait()
