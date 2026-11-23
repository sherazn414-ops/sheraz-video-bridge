"""Sheraz Video Bridge — MCP server for ChatGPT.

Division of labour (Sheraz's rule):
    GPT writes the script  ->  calls submit_script  ->  job lands in Firestore
    Muse picks the job up and makes the video, delivered in the main chat.

DURABILITY (bridge break na ho):
  * Stable public URL — deployed on Render free tier (sheraz-video-bridge),
    NOT a throwaway tunnel URL that changes on restart.
  * Durable queue — jobs live in Firestore (project streamflix-7d2ee,
    collection video_jobs), NOT on the server's disk, so a restart or
    redeploy loses nothing.
  * Keep-alive — a cron pings /health every 10 minutes so Render's free
    tier does not sleep.
  * /health endpoint for uptime checks.

The server exposes ONLY three tools, deliberately append-only + read-only:
    submit_script  - save a script as a queued video job
    list_jobs      - list recent jobs and their status
    job_status     - status (and video link, once done) of one job

No delete, no shell, no file access outside the jobs/ folder, no secrets.
It runs on Streamable HTTP at /mcp so ChatGPT web plugins can reach it.

Claude connects to the SAME server (same URL): its custom-connector
dialog needs minimal OAuth 2.0 (dynamic registration + authorization
code), so this server also exposes the /.well-known/oauth-* discovery
endpoints and /oauth/register, /oauth/authorize, /oauth/approve,
/oauth/token — the same minimal test-OAuth pattern as the Oct 25 bridge,
which Claude accepted. ChatGPT uses "No authentication" and never
touches those endpoints. A token unlocks only queueing a video job.

Config (env vars):
    FIREBASE_API_KEY      Firebase web API key (already public in his apps)
    FIREBASE_PROJECT_ID   Firebase project id (streamflix-7d2ee)
    PORT                  HTTP port (Render sets this; default 8000)
Without the Firebase vars the server falls back to local JSON files in
jobs/ (handy for offline testing — NOT durable, do not use in production).
"""

import html
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

BASE = Path(__file__).resolve().parent
JOBS = BASE / "jobs"
JOBS.mkdir(exist_ok=True)

FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "")
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
PORT = int(os.environ.get("PORT", "8000"))

mcp = FastMCP("Sheraz Video Bridge")

MAX_TITLE = 200
MAX_SCRIPT = 20000
MAX_NOTES = 2000


# ---------------------------------------------------------------- storage
class LocalBackend:
    """JSON files in jobs/ — local testing only, not durable."""

    def create(self, job):
        path = JOBS / f"{job['id']}.json"
        path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        return job

    def list(self, limit):
        out = []
        for p in sorted(JOBS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue
        return out

    def get(self, job_id):
        p = JOBS / f"{job_id}.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))


class FirestoreBackend:
    """Firestore collection video_jobs via the REST API (API key only).

    Firestore rules allow create + read on video_jobs only, with field
    validation (see firestore.rules in the StreamFlix repo). No service
    account key needed; the web API key is already public in his apps.
    """

    def __init__(self, api_key, project_id):
        self.api_key = api_key
        self.base = (
            f"https://firestore.googleapis.com/v1/projects/{project_id}"
            "/databases/(default)/documents"
        )

    def _to_fields(self, job):
        fields = {}
        for k, v in job.items():
            if isinstance(v, str):
                fields[k] = {"stringValue": v}
        return {"fields": fields}

    def _from_doc(self, doc):
        out = {}
        for k, v in doc.get("fields", {}).items():
            if "stringValue" in v:
                out[k] = v["stringValue"]
        return out

    def create(self, job):
        url = f"{self.base}/video_jobs?documentId={job['id']}&key={self.api_key}"
        r = httpx.post(url, json=self._to_fields(job), timeout=30)
        r.raise_for_status()
        return job

    def list(self, limit):
        url = f"{self.base}/video_jobs?pageSize={limit}&key={self.api_key}"
        r = httpx.get(url, timeout=30)
        r.raise_for_status()
        docs = r.json().get("documents", [])
        jobs = [self._from_doc(d) for d in docs]
        jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
        return jobs[:limit]

    def get(self, job_id):
        url = f"{self.base}/video_jobs/{job_id}?key={self.api_key}"
        r = httpx.get(url, timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return self._from_doc(r.json())


def _backend():
    if FIREBASE_API_KEY and FIREBASE_PROJECT_ID:
        return FirestoreBackend(FIREBASE_API_KEY, FIREBASE_PROJECT_ID)
    return LocalBackend()


BACKEND = _backend()


def _job_id_ok(job_id):
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+", job_id or ""))


# ------------------------------------------------------------------- tools
@mcp.tool
def submit_script(
    title: str,
    script: str,
    video_format: str = "9:16 vertical, 60 seconds, emotional micro-drama",
    notes: str = "",
) -> str:
    """Submit a video script from ChatGPT to Muse's video queue.

    Use this when you (ChatGPT) have written a script and want Muse to
    turn it into a video. The script is saved as a job; Muse builds the
    video and delivers it to Sheraz in the main chat. Keep the script to
    short spoken lines (one line = one short sentence), no timestamps.
    """
    title = (title or "").strip()[:MAX_TITLE]
    script = (script or "").strip()[:MAX_SCRIPT]
    notes = (notes or "").strip()[:MAX_NOTES]
    if not title or not script:
        return "ERROR: title and script are both required."
    job = {
        "id": uuid.uuid4().hex[:12],
        "title": title,
        "script": script,
        "format": (video_format or "").strip()[:200],
        "notes": notes,
        "status": "queued",
        "source": "chatgpt-mcp",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        BACKEND.create(job)
    except Exception as e:
        return f"ERROR: could not save job ({type(e).__name__}). Try again."
    return (
        f"OK: script queued as job {job['id']} ('{title}'). "
        "Muse will build the video and deliver it to Sheraz."
    )


@mcp.tool
def list_jobs(limit: int = 10) -> str:
    """List recent video jobs and their status (queued / building / done)."""
    limit = max(1, min(int(limit), 20))
    try:
        jobs = BACKEND.list(limit)
    except Exception as e:
        return f"ERROR: could not list jobs ({type(e).__name__})."
    lines = [
        f"{j.get('id')} | {j.get('status')} | {j.get('title')} | {j.get('created_at', '')}"
        for j in jobs
    ]
    return "\n".join(lines) if lines else "No jobs yet."


@mcp.tool
def job_status(job_id: str) -> str:
    """Get one job's status and, when done, the delivered video link."""
    if not _job_id_ok(job_id):
        return "ERROR: bad job id."
    try:
        j = BACKEND.get(job_id)
    except Exception as e:
        return f"ERROR: could not read job ({type(e).__name__})."
    if not j:
        return f"ERROR: no job {job_id}."
    out = [
        f"id: {j.get('id')}",
        f"title: {j.get('title')}",
        f"status: {j.get('status')}",
        f"format: {j.get('format')}",
        f"created: {j.get('created_at')}",
    ]
    if j.get("video_url"):
        out.append(f"video: {j.get('video_url')}")
    if j.get("note"):
        out.append(f"note: {j.get('note')}")
    return "\n".join(out)


# ------------------------------------------------------------------ health
@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    return JSONResponse(
        {"ok": True, "service": "sheraz-video-bridge", "backend": type(BACKEND).__name__}
    )


# ------------------------------------------------------------------- OAuth
# Minimal OAuth 2.0 for Claude's custom-connector dialog (same pattern as
# the Oct 25 bridge, which Claude accepted). Claude's dialog does dynamic
# client registration, then an authorization-code flow. Without these
# endpoints it fails with "Couldn't register with ... sign-in service".
# This is NOT production OAuth: the approve page has no login. The only
# thing a token unlocks is queueing a video job (append-only), and the
# Render URL itself is the protection. A stable production deployment
# must add a real login before approve.
OAUTH_CLIENTS = {}   # client_id -> {"client_secret": str, "redirect_uris": [str]}
OAUTH_CODES = {}     # code -> {"client_id": str, "redirect_uri": str, "expires": float}
OAUTH_TOKENS = {}    # access_token -> {"client_id": str, "expires": float}
OAUTH_REFRESH = {}   # refresh_token -> {"client_id": str}


def _oauth_base(request: Request) -> str:
    host = request.headers.get("host", f"127.0.0.1:{PORT}")
    proto = request.headers.get("x-forwarded-proto", "")
    if proto:
        scheme = proto.split(",")[0].strip()
    elif host.startswith("127.0.0.1") or host.startswith("localhost"):
        scheme = "http"
    else:
        scheme = "https"
    return f"{scheme}://{host}"


def _oauth_server_meta(request: Request):
    base = _oauth_base(request)
    return {
        "issuer": base,
        "authorization_endpoint": base + "/oauth/authorize",
        "token_endpoint": base + "/oauth/token",
        "registration_endpoint": base + "/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_basic", "client_secret_post"],
    }


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
@mcp.custom_route("/.well-known/oauth-authorization-server/mcp", methods=["GET"])
@mcp.custom_route("/.well-known/openid-configuration", methods=["GET"])
async def _oauth_discovery(request: Request):
    return JSONResponse(_oauth_server_meta(request))


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
@mcp.custom_route("/.well-known/oauth-protected-resource/mcp", methods=["GET"])
async def _oauth_protected_resource(request: Request):
    base = _oauth_base(request)
    return JSONResponse({
        "resource": base + "/mcp",
        "authorization_servers": [base],
        "scopes_supported": ["video"],
    })


@mcp.custom_route("/oauth/authorize", methods=["GET"])
async def _oauth_authorize(request: Request):
    q = request.query_params
    client_id = q.get("client_id", "")
    redirect_uri = q.get("redirect_uri", "")
    state = q.get("state", "")
    if not client_id or not redirect_uri:
        return HTMLResponse(
            "<html><head><title>Muse Video Bridge</title></head><body>"
            "<h2>Is page ko seedha mat kholo</h2>"
            "<p>Ye Approve page sirf Claude ke connector se khulne par "
            "kaam karta hai. Seedha kholne par is me client aur redirect ki "
            "maloomat nahi hoti, is liye Approve dabane se kuch nahi hoga.</p>"
            "<p>Sahi tareeqa: Claude me connector par Connect dabao, "
            "jo Approve page wo khole, us me Approve dabao. Wo tumhe khud wapas "
            "Claude par le jayega.</p></body></html>",
            status_code=400,
        )
    code_challenge = q.get("code_challenge", "")
    method = q.get("code_challenge_method", "plain")
    esc = html.escape
    body = (
        "<html><head><title>Muse Video Bridge</title></head><body>"
        "<h2>Muse Video Bridge</h2>"
        "<p>Claude is asking to connect to your Muse video queue "
        "(submit and check video jobs only). Click Approve to connect.</p>"
        "<form method='post' action='/oauth/approve'>"
        f"<input type='hidden' name='client_id' value='{esc(client_id)}'>"
        f"<input type='hidden' name='redirect_uri' value='{esc(redirect_uri)}'>"
        f"<input type='hidden' name='state' value='{esc(state)}'>"
        f"<input type='hidden' name='code_challenge' value='{esc(code_challenge)}'>"
        f"<input type='hidden' name='code_challenge_method' value='{esc(method)}'>"
        "<button type='submit'>Approve</button></form>"
        "<p><b>TEST OAUTH ONLY — no login on this page.</b> Do not run "
        "this server as a stable public service in this state.</p></body></html>"
    )
    return HTMLResponse(body)


@mcp.custom_route("/oauth/register", methods=["POST"])
@mcp.custom_route("/register", methods=["POST"])
async def _oauth_register(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    client_id = "muse-" + secrets.token_hex(8)
    client_secret = secrets.token_urlsafe(32)
    redirect_uris = body.get("redirect_uris") or []
    OAUTH_CLIENTS[client_id] = {
        "client_secret": client_secret,
        "redirect_uris": redirect_uris,
    }
    return JSONResponse({
        "client_id": client_id,
        "client_secret": client_secret,
        "client_secret_expires_at": 0,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, status_code=201)


@mcp.custom_route("/oauth/approve", methods=["POST"])
async def _oauth_approve(request: Request):
    form = await request.form()
    client_id = form.get("client_id", "")
    redirect_uri = form.get("redirect_uri", "")
    state = form.get("state", "")
    if not redirect_uri:
        return HTMLResponse(
            "<html><head><title>Muse Video Bridge</title></head><body>"
            "<h2>Approve nahi ho saka</h2>"
            "<p>Redirect ki maloomat missing hai. Ye page seedha khola gaya hai, "
            "Claude ke connector se nahi. Koi code issue nahi hua.</p>"
            "<p>Claude me wapas jao, connector par Connect dabao, "
            "aur jo Approve page wo khole us me Approve dabao.</p></body></html>",
            status_code=400,
        )
    code = secrets.token_urlsafe(32)
    OAUTH_CODES[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "expires": time.time() + 600,
    }
    params = {"code": code}
    if state:
        params["state"] = state
    location = redirect_uri + ("&" if "?" in redirect_uri else "?") + urlencode(params)
    return RedirectResponse(location, status_code=302)


@mcp.custom_route("/oauth/token", methods=["POST"])
async def _oauth_token(request: Request):
    form = await request.form()
    grant = form.get("grant_type", "")
    if grant == "authorization_code":
        code = form.get("code", "")
        rec = OAUTH_CODES.pop(code, None)
        if not rec or rec["expires"] < time.time():
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        OAUTH_TOKENS[access] = {"client_id": rec["client_id"], "expires": time.time() + 3600}
        OAUTH_REFRESH[refresh] = {"client_id": rec["client_id"]}
        return JSONResponse({
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": refresh,
            "scope": "video",
        })
    if grant == "refresh_token":
        refresh = form.get("refresh_token", "")
        rec = OAUTH_REFRESH.get(refresh)
        if not rec:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        access = secrets.token_urlsafe(32)
        OAUTH_TOKENS[access] = {"client_id": rec["client_id"], "expires": time.time() + 3600}
        return JSONResponse({
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "video",
        })
    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


if __name__ == "__main__":
    # Streamable HTTP on /mcp — the path ChatGPT web plugins expect.
    mcp.run(transport="streamable-http", host="0.0.0.0", port=PORT, path="/mcp")
