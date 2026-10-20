#!/usr/bin/env python3
"""
comelit_unlock.py — unlock your Comelit door lock from the command line.

Reproduces exactly what the Comelit app does when you tap a lock / actuator:
  1. OAuth2 (authorization-code + PKCE) login to api.comelitgroup.com
  2. list your devices (endpoints) and find the ones with a PowerController
  3. PUT {"value": true} to the device's /power endpoint -> the lock releases

This is the cloud control path (works from anywhere, same as the app). It is NOT
a local-only call — the command goes to Comelit's cloud, which relays it to your
system — but it needs no WebRTC and only the Python standard library.

Nothing proprietary is cracked: it is your own account issuing a normal command
to your own device.

CREDENTIALS: put COMELIT_EMAIL / COMELIT_PASSWORD in `comelit.env` (this folder,
or the project root) or in the environment. They are only ever sent to Comelit's
own login endpoint, exactly as the app does.

Usage:
  python3 comelit_unlock.py --list                 # show your lock/actuator devices
  python3 comelit_unlock.py                         # unlock the default lock
  python3 comelit_unlock.py --name "Fechadura entrada"
  python3 comelit_unlock.py --endpoint "<endpointId>"
"""
import argparse
import base64
import hashlib
import json
import os
import secrets
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

# ---- constants from the decompiled app (BuildConfig / OAuthClientIDKt) ----
BASE_URL = "https://api.comelitgroup.com"
CLIENT_ID = "kgDV0WRlQcSF4jPsz887lOTPyVVtP7Oh"
REDIRECT_URI = "https://app.comelitgroup.com/oauth_redirect/comelit"
SCOPE = "all"
USER_AGENT = "okhttp/4.12.0"

# Default device to actuate. "Fechadura entrada" = entrance door lock on this
# system; override with --name / --endpoint. Use --list to see yours.
DEFAULT_LOCK_NAME = "Fechadura entrada"

# Categories/capabilities that identify an actuatable lock/relay.
POWER_CAP = "PowerController"


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def log(*a):
    print(*a, flush=True)


# --- minimal HTTP (stdlib) that does NOT auto-follow redirects ---------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(
    context=ssl.create_default_context()))


def http(method, url, headers=None, data=None, timeout=30):
    """Return (status, headers, body_text). 3xx is returned, not followed."""
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        resp = _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:   # 4xx/5xx
        return e.code, dict(e.headers or {}), e.read().decode("utf-8", "replace")
    with resp:
        return resp.status, dict(resp.headers or {}), resp.read().decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Auth: OAuth2 authorization-code + PKCE (identical flow to comelit_stream.py)
# ---------------------------------------------------------------------------
def oauth_login(email, password):
    code_verifier = b64url(secrets.token_bytes(32))
    code_challenge = b64url(hashlib.sha256(code_verifier.encode()).digest())
    state = b64url(secrets.token_bytes(16))

    authorize_body = json.dumps({
        "username": email, "password": password, "responseType": "code",
        "clientId": CLIENT_ID, "redirectUri": REDIRECT_URI, "scope": SCOPE,
        "state": state, "codeChallenge": code_challenge,
        "codeChallengeMethod": "S256",
    }).encode()
    log("[auth] POST /o-auth-2/auth (authorize) …")
    status, hdrs, body = http("POST", f"{BASE_URL}/o-auth-2/auth",
                              headers={"Content-Type": "application/json",
                                       "User-Agent": USER_AGENT},
                              data=authorize_body)
    location = None
    if status in (301, 302, 303, 307, 308):
        location = hdrs.get("Location") or hdrs.get("location")
    else:
        try:
            location = json.loads(body).get("location")
        except Exception:
            pass
    if not location:
        raise SystemExit(f"[auth] authorize failed (HTTP {status}): {body[:300]}")
    code = (urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
            .get("code") or [None])[0]
    if not code:
        raise SystemExit(f"[auth] no 'code' in redirect: {location[:200]}")
    log("[auth] got authorization code")

    token_form = urllib.parse.urlencode({
        "grant_type": "authorization_code", "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI, "scope": SCOPE, "code": code,
        "code_verifier": code_verifier,
    }).encode()
    log("[auth] POST /o-auth-2/token …")
    status, _, body = http("POST", f"{BASE_URL}/o-auth-2/token",
                           headers={"Content-Type": "application/x-www-form-urlencoded",
                                    "User-Agent": USER_AGENT},
                           data=token_form)
    if status != 200:
        raise SystemExit(f"[auth] token exchange failed (HTTP {status}): {body[:300]}")
    access_token = json.loads(body)["access_token"]
    log("[auth] access_token acquired")
    return access_token


def auth_header(access_token):
    return {"Authorization": f"bearer {access_token}", "User-Agent": USER_AGENT}


def discover(headers):
    status, _, body = http("GET", f"{BASE_URL}/servicerest/devicecom/endpoints/discovery",
                           headers=headers)
    if status != 200:
        raise SystemExit(f"[discovery] HTTP {status}: {body[:300]}")
    return json.loads(body)


def set_power(headers, endpoint_id, value):
    ep = urllib.parse.quote(endpoint_id, safe="")
    url = f"{BASE_URL}/servicerest/devicecom/endpoint/{ep}/power"
    data = json.dumps({"value": value}).encode()
    status, _, body = http("PUT", url,
                           headers={**headers, "Content-Type": "application/json"},
                           data=data)
    return status, body


# ---------------------------------------------------------------------------
def load_env_file():
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


def is_actuatable(dev):
    return POWER_CAP in dev.get("capabilities", [])


def main():
    load_env_file()
    ap = argparse.ArgumentParser(description="Unlock a Comelit door lock / actuator.")
    ap.add_argument("--list", action="store_true", help="list your lock/actuator devices and exit")
    ap.add_argument("--name", help=f"device friendlyName to actuate (default: {DEFAULT_LOCK_NAME!r})")
    ap.add_argument("--endpoint", help="endpointId to actuate (alternative to --name)")
    ap.add_argument("--value", default="true", choices=["true", "false"],
                    help="power value to set (default true = release the lock)")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    email = os.environ.get("COMELIT_EMAIL")
    password = os.environ.get("COMELIT_PASSWORD")
    if not email or not password:
        raise SystemExit("Set COMELIT_EMAIL and COMELIT_PASSWORD (env or comelit.env).")

    headers = auth_header(oauth_login(email, password))
    devices = discover(headers)

    actuatable = [d for d in devices if is_actuatable(d)]
    log("\n==== LOCKS / ACTUATORS ====")
    for d in actuatable:
        log(f"  {d.get('friendlyName','?'):<24} {','.join(d.get('displayCategories',[])):<16} "
            f"{d.get('endpointId','')}")
    log("===========================\n")
    if args.list:
        return

    # resolve target
    endpoint_id, name = args.endpoint, args.endpoint
    if not endpoint_id:
        want = args.name or DEFAULT_LOCK_NAME
        matches = [d for d in actuatable if d.get("friendlyName", "").lower() == want.lower()]
        if not matches:
            raise SystemExit(f"No actuatable device named {want!r}. Use --list to see names.")
        endpoint_id, name = matches[0]["endpointId"], matches[0]["friendlyName"]

    value = (args.value == "true")
    action = "RELEASE (unlock)" if value else "set power OFF"
    if not args.yes:
        reply = input(f"About to {action} '{name}'. Continue? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            raise SystemExit("Aborted.")

    log(f"[unlock] PUT /power value={value} -> {name}")
    status, body = set_power(headers, endpoint_id, value)
    # The API returns 202 (Accepted) for the power command — it is relayed to the
    # system asynchronously. Treat any 2xx as success.
    if 200 <= status < 300:
        log(f"[ok] door command accepted (HTTP {status}). '{name}' should release now.")
    else:
        raise SystemExit(f"[error] power command failed (HTTP {status}): {body[:300]}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except urllib.error.URLError as e:
        raise SystemExit(f"[error] network: {e}")
