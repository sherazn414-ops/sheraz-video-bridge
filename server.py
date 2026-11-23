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

Config (env vars):
    FIREBASE_API_KEY      Firebase web API key (already public in his apps)
    FIREBASE_PROJECT_ID   Firebase project id (streamflix-7d2ee)
    PORT                  HTTP port (Render sets this; default 8000)
Without the Firebase vars the server falls back to local JSON files in
jobs/ (handy for offline testing — NOT durable, do not use in production).
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

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


if __name__ == "__main__":
    # Streamable HTTP on /mcp — the path ChatGPT web plugins expect.
    mcp.run(transport="streamable-http", host="0.0.0.0", port=PORT, path="/mcp")
