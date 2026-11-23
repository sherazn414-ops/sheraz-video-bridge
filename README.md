# Sheraz Video Bridge — MCP server for ChatGPT

**Kaam:** GPT script likhta hai, Muse video banata hai. Ye server dono ke
beech ka bridge hai.

## Workflow

1. ChatGPT (web ya desktop) mein **Sheraz Video Bridge** plugin on karo.
2. GPT se script likhwao, phir wo `submit_script` tool se script bhej dega.
3. Script yahan `jobs/` mein job ban ke save ho jati hai.
4. Muse (main chat) job dekh ke video banata hai aur Sheraz ko deliver karta hai.
5. GPT `job_status` se check kar sakta hai ke video ban gayi ya nahi.

## Tools (sirf 3 — append-only + read-only, koi delete/shell nahi)

- `submit_script(title, script, video_format, notes)` — script queue karo
- `list_jobs(limit)` — recent jobs + status
- `job_status(job_id)` — aik job ka status + video link (jab done ho)

## Run locally

```bash
cd ~/workspace/mcp-server
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
# .env from .env.example (FIREBASE_API_KEY + FIREBASE_PROJECT_ID)
./venv/bin/python server.py   # http://localhost:8000/mcp
```

NOTE (VM quirk): this machine's `NO_PROXY` contains bracketed IPv6
(`[::1]`) which makes httpx crash with `Invalid port: ':1]'`. Always
start the server with a clean proxy list:
`NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1`. Render
does not have this problem.

## Durability — bridge break na ho

- **Stable URL:** deployed on **Render free tier** (`render.yaml`),
  `https://sheraz-video-bridge.onrender.com/mcp` — NOT a throwaway
  tunnel URL that changes on restart.
- **Durable queue:** jobs live in **Firestore** (`video_jobs`
  collection, project `streamflix-7d2ee`), not on the server disk —
  a restart or redeploy loses nothing.
- **Keep-alive:** a cron pings `/health` every 10 minutes so Render's
  free tier does not sleep (cold start would make the first ChatGPT
  call time out).
- **Health:** `GET /health` returns `{"ok": true, ...}`.
- **Firestore rules:** `video_jobs` allows create + read with the web
  API key only, with strict field validation; only the owner may
  update status/video_url or delete. (Rules live in the StreamFlix
  repo `firestore.rules`, deployed with `ci-tools/deploy_rules.py`.)

## Public URL (ChatGPT web plugins ke liye)

## ChatGPT mein connect karna (web)

1. chatgpt.com/plugins → **+** → **Add custom MCP server**
2. Name: `Sheraz Video Bridge`
3. Description: `GPT writes the script, Muse makes the video.`
4. Connection → Server URL: `<tunnel URL>/mcp`
5. Authentication: **No authentication**
6. Risk warning par **I understand and want to continue** → **Create as a plugin**

## Security notes

- URL kisi se share na karo — jis ke paas URL hai wo script submit kar sakta hai.
- Tools sirf job likh/parh sakte hain; server se kuch delete ya run nahi hota.
- Har job pehle Muse ke haath se guzarti hai — video Sheraz ke "banao"
  kehne par hi banta hai, khud-ba-khud nahi.
