#!/usr/bin/env python3
"""
comelit_stream.py — standalone local WebRTC client for a Comelit ViP system.

It reproduces exactly what the Comelit app does to view the door camera:
  1. OAuth2 (authorization-code + PKCE) login to api.comelitgroup.com
  2. list your devices (endpoints)
  3. create a WebRTC offer and PUT it to the device's rtc/offer endpoint
  4. receive the DTLS-SRTP H.264/Opus media — which, on the same LAN, flows
     peer-to-peer straight from the monitor (e.g. 192.168.1.220) — and record
     it to a file (and/or you can restream it).

Nothing proprietary is cracked: we are a legitimate WebRTC peer completing the
standard handshake with your own device, using your own account.

Constants below were extracted from the decompiled app v7.5.0-7.

CREDENTIALS: supply your own Comelit login via environment variables or a
`comelit.env` file next to this script (see comelit.env.example). This script
never stores or transmits them anywhere except Comelit's own login endpoint,
exactly as the app does.

Usage:
  # put COMELIT_EMAIL / COMELIT_PASSWORD in comelit.env, then:
  ./rtcvenv/bin/python comelit_stream.py --list           # just log in + list devices
  ./rtcvenv/bin/python comelit_stream.py --endpoint <ID> --duration 30 --out feed.mp4
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import tempfile
import uuid
from urllib.parse import urlparse, parse_qs, quote

import aiohttp

# ---- constants from the decompiled app (BuildConfig / OAuthClientIDKt / FirebaseProject) ----
BASE_URL = "https://api.comelitgroup.com"
CLIENT_ID = "kgDV0WRlQcSF4jPsz887lOTPyVVtP7Oh"
REDIRECT_URI = "https://app.comelitgroup.com/oauth_redirect/comelit"
SCOPE = "all"
FIREBASE_API_KEY = "AIzaSyDPANoOl3vYKoGPk3CqjDjIbJHrhJUzz7M"
FIREBASE_PROJECT = "friend-home-55eb5"

ICE_SERVERS = [
    "stun:stun1.l.google.com:19302",
    "stun:stun2.l.google.com:19302",
    "stun:stun.cloud.comelitgroup.com:3478",
]

USER_AGENT = "okhttp/4.12.0"   # generic; the API doesn't appear to gate on this


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------------------
# Auth: OAuth2 authorization-code + PKCE
# ---------------------------------------------------------------------------
async def oauth_login(session, email, password):
    code_verifier = b64url(secrets.token_bytes(32))
    code_challenge = b64url(hashlib.sha256(code_verifier.encode()).digest())
    state = b64url(secrets.token_bytes(16))

    # 1) authorize -> returns {location: "<redirect>?code=...&state=..."}
    authorize_body = {
        "username": email,
        "password": password,
        "responseType": "code",
        "clientId": CLIENT_ID,
        "redirectUri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "codeChallenge": code_challenge,
        "codeChallengeMethod": "S256",
    }
    log("[auth] POST /o-auth-2/auth (authorize) …")
    async with session.post(f"{BASE_URL}/o-auth-2/auth", json=authorize_body,
                            allow_redirects=False) as r:
        text = await r.text()
        location = None
        if r.status in (301, 302, 303, 307, 308):
            location = r.headers.get("Location")
        else:
            try:
                location = json.loads(text).get("location")
            except Exception:
                pass
        if not location:
            raise SystemExit(f"[auth] authorize failed (HTTP {r.status}): {text[:400]}")
    qs = parse_qs(urlparse(location).query)
    code = (qs.get("code") or [None])[0]
    if not code:
        raise SystemExit(f"[auth] no 'code' in redirect: {location[:200]}")
    log("[auth] got authorization code")

    # 2) token exchange (form-urlencoded)
    token_form = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code": code,
        "code_verifier": code_verifier,
    }
    log("[auth] POST /o-auth-2/token …")
    async with session.post(f"{BASE_URL}/o-auth-2/token", data=token_form) as r:
        text = await r.text()
        if r.status != 200:
            raise SystemExit(f"[auth] token exchange failed (HTTP {r.status}): {text[:400]}")
        tok = json.loads(text)
    access_token = tok["access_token"]
    log(f"[auth] access_token acquired (type={tok.get('token_type')}, "
        f"expires_in={tok.get('expires_in')})")
    return access_token


async def auth_headers_for(session, access_token):
    """Return the Authorization header that the device-com API accepts."""
    for scheme in ("bearer", "ccstoken"):
        headers = {"Authorization": f"{scheme} {access_token}", "User-Agent": USER_AGENT}
        async with session.get(f"{BASE_URL}/servicerest/devicecom/endpoints/discovery",
                               headers=headers) as r:
            if r.status == 200:
                log(f"[auth] device-com accepts '{scheme}' tokens")
                return headers, await r.text()
            log(f"[auth] '{scheme}' -> HTTP {r.status}")
    raise SystemExit("[auth] neither 'bearer' nor 'ccstoken' accepted by device-com. "
                     "Check account access and the HTTP codes above.")


# ---------------------------------------------------------------------------
# WebRTC
# ---------------------------------------------------------------------------
def make_rpc(method, params, _id):
    return json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": _id})


def compatible_offer(sdp):
    """Advertise SHA-256 only, matching the embedded monitor's SDP parser.

    aiortc 1.15 emits SHA-256, SHA-384 and SHA-512 for the same certificate.
    This monitor completes DTLS but closes before SCTP with that offer.
    A single SHA-256 fingerprint works; certificate verification stays enabled.
    """
    return "\r\n".join(
        line for line in sdp.splitlines()
        if not line.startswith("a=fingerprint:") or line.startswith("a=fingerprint:sha-256 ")
    ) + "\r\n"


def verify_recording(path):
    """Require an actual decodable video frame, not just a container header."""
    import av
    with av.open(path) as container:
        if not container.streams.video:
            raise RuntimeError("Recording contains no video stream")
        frame = next(container.decode(video=0), None)
        if frame is None:
            raise RuntimeError("Recording contains no decodable video frames")
        return frame.width, frame.height


def _dump_sdp(tag, sdp):
    """Print the structurally interesting lines of an SDP."""
    log(f"---- {tag} SDP (summary) ----")
    for line in sdp.splitlines():
        if line[:2] in ("m=",) or line.startswith((
                "a=setup", "a=sctp", "a=mid", "a=rtpmap", "a=sendonly", "a=recvonly",
                "a=sendrecv", "a=inactive", "a=fmtp", "a=candidate", "a=fingerprint")):
            log("   " + line)
    log("---------------------------")


async def run_webrtc(headers, endpoint_id, out_file, duration, call_id, session,
                     send_start=True, resolution="HD", dtls_setup="actpass", sctp_role="auto",
                     srtp_profile="auto", startup_timeout=30, debug=False):
    from aiortc import (RTCPeerConnection, RTCConfiguration, RTCIceServer,
                        RTCSessionDescription, MediaStreamTrack)
    from aiortc.contrib.media import MediaRecorder

    config = RTCConfiguration(iceServers=[RTCIceServer(urls=[u]) for u in ICE_SERVERS])
    pc = RTCPeerConnection(configuration=config)
    # Write to a temporary sibling so a failed attempt preserves an existing recording.
    suffix = os.path.splitext(out_file)[1] or ".mp4"
    fd, pending_file = tempfile.mkstemp(prefix=".comelit-", suffix=suffix,
                                      dir=os.path.dirname(os.path.abspath(out_file)))
    os.close(fd)
    recorder = MediaRecorder(pending_file)
    first_video_at = None
    frames = {"video": 0, "audio": 0}
    stopped = asyncio.Event()

    class CountedTrack(MediaStreamTrack):
        def __init__(self, source):
            super().__init__()
            self.source = source
            self.kind = source.kind

        async def recv(self):
            nonlocal first_video_at
            frame = await self.source.recv()
            frames[self.kind] += 1
            if self.kind == "video" and first_video_at is None:
                first_video_at = asyncio.get_running_loop().time()
                log(f"[rtc] first video frame: {frame.width}x{frame.height}; recording for {duration}s")
            return frame

    @pc.on("track")
    def on_track(track):
        log(f"[rtc] track negotiated: {track.kind} (waiting for frames)")
        recorder.addTrack(CountedTrack(track))

    @pc.on("connectionstatechange")
    async def on_state():
        log(f"[rtc] connection state: {pc.connectionState}")
        if pc.connectionState in ("failed", "closed"):
            stopped.set()
        elif pc.connectionState == "connected":
            # aiortc currently exposes the selected ICE pair through aioice.
            connection = pc.sctp.transport.transport._connection
            for pair in getattr(connection, "_nominated", {}).values():
                local, remote = pair.local_candidate, pair.remote_candidate
                log(f"[rtc] media path: {local.host}:{local.port} -> "
                    f"{remote.host}:{remote.port} ({remote.type})")

    @pc.on("iceconnectionstatechange")
    async def on_ice():
        log(f"[rtc] ICE state: {pc.iceConnectionState}")

    # Cloud-offer calls in the app create these two Connexus control channels.
    def log_msg(label, message):
        if isinstance(message, (bytes, bytearray)):
            log(f"[dc] <- {label}: <{len(message)} bytes> {bytes(message)[:80]!r}")
        else:
            log(f"[dc] <- {label}: {message[:300]}")

    dcs = {}
    for label in ("libcomelit-channel", "iu-channel"):
        ch = pc.createDataChannel(label, ordered=True)
        dcs[label] = ch

        @ch.on("open")
        def _on_open(label=label, ch=ch):
            log(f"[dc] '{label}' OPEN")
            if label == "libcomelit-channel" and send_start:
                msg = make_rpc("set-video-resolution", {"resolution": resolution}, 1)
                log(f"[dc] -> {label}: {msg}")
                ch.send(msg)

        @ch.on("message")
        def _on_msg(message, label=label):
            log_msg(label, message)

    # Diagnostic override scoped to this connection, never the library globally.
    # The working default initiates SCTP normally; DTLS server does not imply
    # that we must wait for the peer to initiate SCTP.
    if sctp_role in ("server", "client"):
        from aiortc.rtcsctptransport import RTCSctpTransport

        class ForcedSctpTransport(RTCSctpTransport):
            @property
            def is_server(self):
                return sctp_role == "server"

        pc.sctp.__class__ = ForcedSctpTransport
        log(f"[rtc] forcing aiortc SCTP role = {sctp_role}")

    @pc.on("datachannel")
    def on_remote_dc(channel):
        log(f"[dc] monitor opened channel: '{channel.label}'")

        @channel.on("message")
        def _rm(message, label=channel.label):
            log_msg(label, message)

    # We want to RECEIVE audio+video.
    pc.addTransceiver("video", direction="recvonly")
    pc.addTransceiver("audio", direction="recvonly")

    if srtp_profile == "classic":
        from aiortc.rtcdtlstransport import SRTP_AES128_CM_SHA1_80
        transports = {t.receiver.transport for t in pc.getTransceivers()}
        transports.add(pc.sctp.transport)
        for transport in transports:
            transport._srtp_profiles = [SRTP_AES128_CM_SHA1_80]
        log("[rtc] using SRTP_AES128_CM_SHA1_80")

    try:
        log("[rtc] creating offer (with data channels) and gathering ICE candidates …")
        await pc.setLocalDescription(await pc.createOffer())
        # aiortc completes ICE gathering before setLocalDescription returns.
        offer_sdp = pc.localDescription.sdp
        offer_sdp = compatible_offer(offer_sdp)
        # Diagnostic override; this monitor normally answers active regardless.
        if dtls_setup and dtls_setup != "actpass":
            offer_sdp = offer_sdp.replace("a=setup:actpass", f"a=setup:{dtls_setup}")
            log(f"[rtc] forcing DTLS setup:{dtls_setup} in the offer we send")
        n_host = offer_sdp.count("typ host")
        n_srflx = offer_sdp.count("typ srflx")
        log(f"[rtc] offer ready ({n_host} host, {n_srflx} srflx candidates)")

        session_id = str(uuid.uuid4())
        body = {"sessionId": session_id, "offer": offer_sdp, "callId": call_id}
        # endpointId contains '#' etc. -> must be percent-encoded in the path
        url = f"{BASE_URL}/servicerest/devicecom/endpoint/{quote(endpoint_id, safe='')}/rtc/offer"
        log(f"[rtc] PUT offer -> endpoint {endpoint_id} (sessionId={session_id[:8]}…) …")
        async with session.put(url, headers={**headers, "Content-Type": "application/json"},
                               data=json.dumps(body)) as r:
            text = await r.text()
            if r.status not in (200, 201):
                raise SystemExit(f"[rtc] offer rejected (HTTP {r.status}): {text[:500]}")
            try:
                answer_sdp = json.loads(text).get("answer")
            except Exception:
                answer_sdp = None
            if not answer_sdp:
                raise SystemExit(f"[rtc] no 'answer' in response: {text[:500]}")
        log("[rtc] received SDP answer, setting remote description …")
        if debug:
            _dump_sdp("OFFER", offer_sdp)
            _dump_sdp("ANSWER", answer_sdp)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))

        await recorder.start()
        log(f"[rtc] waiting up to {startup_timeout}s for video … (Ctrl+C to stop)")
        loop = asyncio.get_running_loop()
        started = loop.time()
        next_status = started + 5
        try:
            while first_video_at is None or loop.time() - first_video_at < duration:
                if stopped.is_set():
                    raise RuntimeError("Peer connection closed before recording completed")
                if first_video_at is None and loop.time() - started >= startup_timeout:
                    raise RuntimeError("No video frames arrived before the startup timeout")
                now = loop.time()
                if now >= next_status:
                    states = ", ".join(f"{label}={ch.readyState}" for label, ch in dcs.items())
                    log(f"[rtc] video={frames['video']} frames, audio={frames['audio']} frames | "
                        f"dtls={pc.sctp.transport.state} | sctp={pc.sctp.state} | {states}")
                    next_status = now + 5
                deadline = (first_video_at + duration if first_video_at is not None
                            else started + startup_timeout)
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=min(0.25, max(0, deadline - now)))
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            # Preserve a valid shortened recording when interrupted after video starts.
            if first_video_at is None:
                raise
        await recorder.stop()
        width, height = verify_recording(pending_file)
        os.replace(pending_file, out_file)
        final = os.path.getsize(out_file)
        log(f"[done] saved {out_file} ({final/1024:.0f} KB, "
            f"{frames['video']} video frames, {width}x{height})")
    finally:
        try:
            await recorder.stop()
        finally:
            await pc.close()
            if os.path.exists(pending_file):
                os.unlink(pending_file)


# ---------------------------------------------------------------------------
def load_env_file():
    # Look next to the script and in the parent dirs, so a single shared
    # comelit.env at the project root serves both video_stream/ and door_unlock/.
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, "comelit.env"),
                 os.path.join(here, os.pardir, "comelit.env"),
                 os.path.join(here, os.pardir, os.pardir, "comelit.env")):
        if os.path.exists(path):
            for line in open(path):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
            return


async def main():
    load_env_file()
    if "--debug" in sys.argv:
        import logging
        logging.basicConfig(level=logging.INFO)
        for name in ("aiortc", "aioice", "aiortc.rtcsctptransport", "aiortc.rtcdtlstransport"):
            logging.getLogger(name).setLevel(logging.DEBUG)
    ap = argparse.ArgumentParser(description="Local WebRTC client for a Comelit ViP door camera.")
    ap.add_argument("--list", action="store_true", help="just log in and list devices, then exit")
    ap.add_argument("--name", help="select the device to stream by friendlyName, e.g. --name Entrada")
    ap.add_argument("--endpoint", help="endpointId to stream from (alternative to --name)")
    ap.add_argument("--duration", type=int, default=30, help="seconds to record (default 30)")
    ap.add_argument("--out", default="comelit_feed.mp4",
                    help="output media file (relative paths are resolved next to this script)")
    ap.add_argument("--call-id", default=None, help="optional callId / camera selector")
    ap.add_argument("--no-start", action="store_true",
                    help="do NOT send the set-video-resolution start command (test if video auto-starts)")
    ap.add_argument("--resolution", default="HD", choices=["HD", "SD"], help="requested resolution")
    ap.add_argument("--debug", action="store_true", help="verbose aiortc/SCTP/DTLS logging")
    ap.add_argument("--dtls-setup", default="actpass", choices=["active", "passive", "actpass"],
                    help="DTLS role to offer (default actpass: monitor picks, it always picks active)")
    ap.add_argument("--sctp-role", default="auto", choices=["server", "client", "auto"],
                    help="diagnostic SCTP role override (default auto: aiortc initiates normally)")
    ap.add_argument("--srtp-profile", default="auto", choices=["auto", "classic"],
                    help="SRTP protection profile (classic restricts negotiation to AES128_CM_SHA1_80)")
    ap.add_argument("--startup-timeout", type=float, default=30,
                    help="seconds to wait for the first video frame (default 30)")
    args = ap.parse_args()
    if args.duration <= 0 or args.startup_timeout <= 0:
        ap.error("duration and startup-timeout must be positive")

    email = os.environ.get("COMELIT_EMAIL")
    password = os.environ.get("COMELIT_PASSWORD")
    if not email or not password:
        raise SystemExit("Set COMELIT_EMAIL and COMELIT_PASSWORD (env or comelit.env). "
                         "See comelit.env.example.")

    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT},
                                     timeout=aiohttp.ClientTimeout(total=30)) as session:
        access_token = await oauth_login(session, email, password)
        headers, discovery_json = await auth_headers_for(session, access_token)

        try:
            devices = json.loads(discovery_json)
        except Exception:
            devices = []
        log("\n==== DEVICES ====")
        for d in devices:
            cap = d.get("capabilities", [])
            streamable = "RTCSessionController" in cap
            mark = "  [streamable]" if streamable else ""
            log(f"  {d.get('friendlyName','?'):<24} {','.join(d.get('displayCategories',[])):<14}{mark}")
        log("=================\n")

        if args.list:
            return

        endpoint_id = args.endpoint
        if args.name:
            matches = [d for d in devices if d.get("friendlyName", "").lower() == args.name.lower()]
            if not matches:
                raise SystemExit(f"No device named '{args.name}'. Use --list to see names.")
            endpoint_id = matches[0]["endpointId"]
            if "RTCSessionController" not in matches[0].get("capabilities", []):
                log(f"[warn] '{args.name}' has no RTCSessionController capability; offer may fail.")
        if not endpoint_id:
            raise SystemExit("Pass --name <friendlyName> (e.g. --name Entrada) or --endpoint <ID>.")

        out_file = args.out
        if not os.path.isabs(out_file):
            out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), out_file)
        log(f"[rtc] target endpoint: {endpoint_id}")
        log(f"[rtc] output file: {out_file}")
        await run_webrtc(headers, endpoint_id, out_file, args.duration, args.call_id, session,
                         send_start=not args.no_start, resolution=args.resolution,
                         dtls_setup=args.dtls_setup, sctp_role=args.sctp_role,
                         srtp_profile=args.srtp_profile, startup_timeout=args.startup_timeout,
                         debug=args.debug)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise SystemExit(f"[error] {exc}") from None
