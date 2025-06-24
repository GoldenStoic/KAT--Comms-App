import os, asyncio, jwt

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from twilio.rest import Client
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCIceServer,
    RTCConfiguration,
    RTCIceCandidate,
)
from aiortc.contrib.media import MediaRelay

# ─── Config ──────────────────────────────────────────────────────────────────
JWT_SECRET        = os.getenv("JWT_SECRET", "change-this-to-a-strong-secret")
TW_ACCOUNT_SID    = os.environ["TWILIO_ACCOUNT_SID"]
TW_API_KEY_SID    = os.environ["TWILIO_API_KEY_SID"]
TW_API_KEY_SECRET = os.environ["TWILIO_API_KEY_SECRET"]

twilio = Client(TW_API_KEY_SID, TW_API_KEY_SECRET, TW_ACCOUNT_SID)
relay  = MediaRelay()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# room state: room_id → { admins:set, waiting:set, peers:dict }
rooms = {}

def normalize_ice_server(s: dict) -> dict:
    """
    Promote 'url' → 'urls' and drop any STUN URLs later.
    """
    d = dict(s)
    if "url" in d:
        d.setdefault("urls", [d.pop("url")])
    return d

def only_turn_servers(ice_servers):
    """
    Given a list of Twilio ICE dicts, normalize them,
    then return only those whose URL starts with 'turn:'.
    """
    out = []
    for s in ice_servers:
        norm = normalize_ice_server(s)
        urls = norm.get("urls", [])
        # keep only TURN relays
        turn_urls = [u for u in urls if u.strip().lower().startswith("turn:")]
        if turn_urls:
            norm["urls"] = turn_urls
            out.append(norm)
    return out

async def authenticate(token: str):
    try:
        payload = jwt.decode(token or "", JWT_SECRET, algorithms=["HS256"])
        return payload.get("role", "user")
    except:
        return "user"

@app.get("/ice")
async def ice():
    # still return both STUN+TURN for browsers, they handle iceTransportPolicy
    token_obj = twilio.tokens.create()
    servers = [normalize_ice_server(s) for s in token_obj.ice_servers]
    return JSONResponse(servers)

@app.get("/")
async def index():
    return FileResponse("static/index.html")

async def _admit(ws: WebSocket, room_id: str):
    state = rooms[room_id]
    state["waiting"].discard(ws)
    await ws.send_json({"type":"admitted","peer_id":id(ws)})

    # fetch fresh Twilio ICE creds
    token_obj = twilio.tokens.create()
    # **filter** out STUN, leave only TURN
    turn_only = only_turn_servers(token_obj.ice_servers)
    rtc_ice = [RTCIceServer(**c) for c in turn_only]
    config  = RTCConfiguration(iceServers=rtc_ice)

    pc = RTCPeerConnection(configuration=config)
    state["peers"][ws] = pc

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            r = relay.subscribe(track)
            for other in state["peers"].values():
                if other is not pc:
                    other.addTrack(r)

    # tell client to start its offer
    await ws.send_json({"type":"ready_for_offer"})

@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    role  = await authenticate(ws.query_params.get("token",""))
    state = rooms.setdefault(room_id, {"admins":set(), "waiting":set(), "peers":{}})

    if role == "admin":
        state["admins"].add(ws)
        await _admit(ws, room_id)
        for pend in state["waiting"]:
            await ws.send_json({"type":"new_waiting","peer_id":id(pend)})
    else:
        state["waiting"].add(ws)
        await ws.send_json({"type":"waiting"})
        for adm in state["admins"]:
            await adm.send_json({"type":"new_waiting","peer_id":id(ws)})
        while ws not in state["peers"]:
            await asyncio.sleep(0.1)

    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")

            if typ == "offer":
                pc = state["peers"][ws]
                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=msg["sdp"], type="offer")
                )
                ans = await pc.createAnswer()
                await pc.setLocalDescription(ans)
                await ws.send_json({"type":"answer","sdp":pc.localDescription.sdp})

            elif typ == "ice":
                pc = state["peers"][ws]
                c  = msg["candidate"]
                p  = c["candidate"].split()
                ice = RTCIceCandidate(
                    foundation=p[0].split(":",1)[1],
                    component=int(p[1]),
                    protocol=p[2].lower(),
                    priority=int(p[3]),
                    ip=p[4],
                    port=int(p[5]),
                    type=p[7],
                    sdpMid=c.get("sdpMid"),
                    sdpMLineIndex=c.get("sdpMLineIndex")
                )
                try:
                    await pc.addIceCandidate(ice)
                except:
                    pass

            elif typ == "chat":
                for peer in state["peers"]:
                    await peer.send_json({"type":"chat","from":msg["from"],"text":msg["text"]})

            elif typ == "admit" and ws in state["admins"]:
                tid = msg["peer_id"]
                pend = next((w for w in state["waiting"] if id(w)==tid), None)
                if pend:
                    await _admit(pend, room_id)

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
            for s in pc.getSenders():
                if s.track: s.track.stop()
            await pc.close()
