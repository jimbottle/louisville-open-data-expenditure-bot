# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->


## ⚠️ Where the bot runs (READ FIRST)

> **The live, production bot is https://louisville.raylytics.io/ — and ONLY that URL.**
>
> **Everything else is dev/test, never call it "the live bot":**
> - `192.168.0.218:8000` — the server's LAN address (same container, for local testing on the network).
> - `localhost:8000` / `127.0.0.1:8000` — a local dev run, or the tunnel's origin target on the server.
> - Any `uvicorn`/Docker instance you start while iterating.
>
> When asked to "test the bot on the site" or to "publish/deploy," the target is
> **https://louisville.raylytics.io/**. Verify changes there, not at a LAN IP.

### Production path (how the URL maps to the app)

```
https://louisville.raylytics.io
   → Cloudflare (DNS proxy + TLS)
   → cloudflared tunnel on the server "Air-Server.local" (~/.cloudflared/config.yml: hostname louisville.raylytics.io → http://localhost:8000)
   → Docker container `louisville-bot` (published on :8000)
```

So the `louisville-bot` container on the server **is** the production origin. Rebuilding/recreating that container is what ships a change to https://louisville.raylytics.io/.

## Deployment

This is a **self-hosted FastAPI app**, NOT a static site. Pushing to GitHub does **not** deploy it (no CI/CD); the running code is baked into the Docker image, so a change is live only after the image is rebuilt and the container recreated.

Server access is via the **mac-shell MCP tool** (not SSH; see the deployment memory). The server (`Air-Server.local`) has `gh` authed and Docker. Deploy steps (one command at a time — the MCP runner is not a full shell: no `&&`, `;`, `|`, or redirection):

```bash
# 1. Pull latest code on the server
gh repo clone jimbottle/louisville-open-data-expenditure-bot /Users/macserver/projects/louisville-open-data   # or: git -C <dir> pull
# 2. Build the image (data is NOT in the image; it lives in the louisville-data volume)
docker build -t louisville-bot:new /Users/macserver/projects/louisville-open-data
# 3. Swap reversibly (keep the old container for rollback)
docker rename louisville-bot louisville-bot-prev
docker stop louisville-bot-prev
docker run -d --name louisville-bot --restart unless-stopped -p 8000:8000 \
  -v louisville-data:/data:ro -v louisville-state:/state -v louisville-logs:/logs \
  -e CEREBRAS_API_KEY=... -e CEREBRAS_PAID_API_KEY=... -e MODEL=qwen-3-235b-a22b-instruct-2507 \
  -e LLM_BASE_URL=https://api.cerebras.ai/v1 -e DATA_DIR=/data -e STATS_DIR=/state -e LOG_DIR=/logs \
  --health-cmd "curl -sf --max-time 5 http://localhost:8000/api/health || exit 1" \
  --health-interval 30s --health-start-period 120s --health-retries 3 \
  louisville-bot:new
docker tag louisville-bot:new louisville-bot:latest
# Rollback: docker stop louisville-bot; docker rm louisville-bot; docker rename louisville-bot-prev louisville-bot; docker start louisville-bot
```

### Deploy verification (MANDATORY — do not skip)

A deploy is not "done" until it is tested **end-to-end through the production URL**, not just by loading the page. Loading `/` proves static files; it does NOT prove the API works (the edge can block the API path while serving the page fine — exactly what the Cloudflare `/api` WAF block did).

1. Container reachable: `curl -sf http://localhost:8000/api/health` on the server returns `ok`.
2. **End-to-end through production:** `curl -i -X POST https://louisville.raylytics.io/<ask-path> -H 'Content-Type: application/json' --data '{"question":""}'` must return **HTTP 200** and `content-type: text/event-stream` (an empty question short-circuits to an SSE error with no LLM call, so it's a free, safe probe). A `403`/`5xx` here means the edge or app is blocking the API, fix before calling the deploy good.
3. One real question in the browser at https://louisville.raylytics.io/ returns an answer.

Persistent state lives in Docker named volumes, not the image: `louisville-data` (CSVs, read-only at `/data`), `louisville-state` (`.stats.json`, response cache), `louisville-logs` (rotating logs). Env (incl. the Cerebras keys) is set on the container, sourced from the gitignored `.env` locally; the keys are not in git.

The repo's `docker-compose.yml` and `deploy.sh` describe an **older** flow (bind-mounted `./data`, `DATA_DIR=/app/data`, SSH rsync) that does **not** match the current production container (named volumes at `/data`,`/state`,`/logs`; deployed via the MCP Docker flow above). Trust the running container's config over those files.

## Build & Test

```bash
# Run the test suite (known-answer + canonicalization invariants)
python -m pytest -q

# Run locally for dev (serves frontend + API same-origin on :8000)
uvicorn app:app --host 127.0.0.1 --port 8000
# then open http://127.0.0.1:8000  (DEV ONLY — see "Where the bot runs" above)
```

## Architecture Overview

- **`app.py`** — FastAPI backend. Serves the static frontend AND the `/api/ask` SSE endpoint (same origin). Translates NL → SQL via an OpenAI-compatible client (Cerebras), runs it on DuckDB, streams an interpretation. Per-IP rate limit (5/min), persistent stats + response cache, structured SSE events (`status`, `reasoning`, `sql`, `results`, `chart`, `interpretation`, `log`, `debug`, `usage`, `error`, `info`, `done`).
- **`analytics_agent.py`** — LLM calls (reason → generate SQL → interpret), retry/fallback (free → paid Cerebras), SQL safety guard.
- **`data_model.py`** — loads CSVs into DuckDB, builds `*_canonical` columns + summary tables, flags offsetting/artifact rows.
- **`static/index.html`** — single-page chat UI (vanilla JS, inline CSS, Chart.js). Self-contained; talks to `/api/ask`.
- **`tests/test_known_answers.py`** — known-answer + invariant suite.

## Conventions & Patterns

- Frontend calls a **same-origin relative `/api/ask`**, so the frontend and backend must be served by the same FastAPI process (this is why it can't be a static Netlify deploy).
- Commit after impactful changes; push only when asked; deploy is a separate explicit step (see Deployment).
