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
  -e CEREBRAS_API_KEY=... -e CEREBRAS_PAID_API_KEY=... -e MODEL=gpt-oss-120b \
  -e LLM_BASE_URL=https://api.cerebras.ai/v1 -e DATA_DIR=/data -e STATS_DIR=/state -e LOG_DIR=/logs \
  --health-cmd "curl -sf --max-time 5 http://localhost:8000/api/health || exit 1" \
  --health-interval 30s --health-start-period 600s --health-retries 3 \
  louisville-bot:new
docker tag louisville-bot:new louisville-bot:latest
# Rollback: docker stop louisville-bot; docker rm louisville-bot; docker rename louisville-bot-prev louisville-bot; docker start louisville-bot
```

> ⚠️ **Keep `--health-start-period` at 600s.** Cold start (CSVs → DuckDB → RAG corpus →
> response cache) can exceed 4 minutes under host load. It was 120s, so health checks
> began before the app was listening and the `autoheal` container restart-looped it
> every ~5 minutes — the 2026-08-11 502 outage. See `monitoring/README.md`.

### Uptime alerting

A dead-man's switch pings healthchecks.io every 60s **only if
https://louisville.raylytics.io actually serves the API**; ~4 minutes of silence fires a
max-priority ntfy push + email. This is separate from the Air's host-level heartbeat,
which cannot see a dead container on a healthy machine. Setup, ping UUID, and drill
procedure: `monitoring/README.md`.

### Deploy verification (MANDATORY — do not skip)

A deploy is not "done" until it is tested **end-to-end through the production URL**, not just by loading the page. Loading `/` proves static files; it does NOT prove the API works (the edge can block the API path while serving the page fine — exactly what the Cloudflare `/api` WAF block did).

1. Container reachable: `curl -sf http://localhost:8000/api/health` on the server returns `ok`.
2. **End-to-end through production:** `curl -i -X POST https://louisville.raylytics.io/<ask-path> -H 'Content-Type: application/json' --data '{"question":""}'` must return **HTTP 200** and `content-type: text/event-stream` (an empty question short-circuits to an SSE error with no LLM call, so it's a free, safe probe). A `403`/`5xx` here means the edge or app is blocking the API, fix before calling the deploy good.
3. One real question in the browser at https://louisville.raylytics.io/ returns an answer.

Persistent state lives in Docker named volumes, not the image: `louisville-data` (CSVs, read-only at `/data`), `louisville-state` (`.stats.json`, response cache), `louisville-logs` (rotating logs). Env (incl. the Cerebras keys) is set on the container, sourced from the gitignored `.env` locally; the keys are not in git.

The repo's `docker-compose.yml` and `deploy.sh` describe an **older** flow (bind-mounted `./data`, `DATA_DIR=/app/data`, SSH rsync) that does **not** match the current production container (named volumes at `/data`,`/state`,`/logs`; deployed via the MCP Docker flow above). Trust the running container's config over those files.

### LLM model (`MODEL` env)

Currently `gpt-oss-120b` on Cerebras. **Cerebras deprecates models without notice** — the bot previously ran `qwen-3-235b-a22b-instruct-2507`, which started returning `404 model_not_found` on every live call (cached starter answers still worked, which masked it). If live queries suddenly fail, list the account's current models and switch:

```bash
curl -s https://api.cerebras.ai/v1/models -H "Authorization: Bearer $CEREBRAS_API_KEY"   # pick a valid id
# then recreate the container with the new -e MODEL=... (no image rebuild needed)
```

`gpt-oss-120b` is a reasoning model: its chain-of-thought comes back in a separate `reasoning` field, the final answer is in `message.content`/`delta.content` (what the app reads), so reasoning never pollutes output as long as `max_tokens` leaves room after the reasoning tokens.

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
- **`data_model.py`** — generic city data engine: loads CSVs into DuckDB, builds `*_canonical` columns + summary tables, flags offsetting/artifact rows — all driven by a city config pack. Nothing city-specific lives here.
- **`city_config.py`** + **`cities/<city>/city.yaml`** — city config packs (sources/era mappings, canonical map CSVs, data-quality params, summary SQL, data dictionary). `CITY_CONFIG` env var selects the pack (default: Louisville). Format documented in `docs/canonical-model.md`; `cities/cincinnati/` and `cities/kansas_city/` are paper configs (not yet runnable).
- **`static/index.html`** — single-page chat UI (vanilla JS, inline CSS, Chart.js). Self-contained; talks to `/api/ask`.
- **`tests/test_known_answers.py`** — known-answer + invariant suite.

## Conventions & Patterns

- Frontend calls a **same-origin relative `/api/ask`**, so the frontend and backend must be served by the same FastAPI process (this is why it can't be a static Netlify deploy).
- Commit after impactful changes; push only when asked; deploy is a separate explicit step (see Deployment).

<!-- BEGIN WYK CONVENTIONS v:1 -->
## wyk — planning & handoff over bd

This repo uses **wyk**, a view + handoff layer over **bd (beads)**. "Plan
it in wyk" = **file the plan as bd issues** (deps via `bd dep add`), not
markdown/TodoWrite. File with **`wyk create`** (same flags as `bd create`,
forwarded verbatim) — it also stamps the Claude session so the TUI's
Session column traces work back to a conversation. A PreToolUse hook
blocks raw `bd create` and tells you to switch; that's expected — just
re-run as `wyk create`.

**Owner column** — whose move it is, label-driven (NOT bd's owner/assignee):
- `human` → **HUMAN** (a human must act).
- `agent-handoff` → **AGENT-HANDOFF**: another agent owns it; don't touch,
  a human coordinates. Excluded from `wyk inbox`.
- agent task blocked by a `human`-flagged dep → **HUMAN-BLOCK** (skip it).
- else → **AGENT** (the default; a null owner is never blank — so a task
  that needs a human MUST be handed off, or the human never sees it).

**Hand off to a human**: `wyk handoff <id>` (or `wyk handoff -create "<title>"`)
sets `human` + writes the runbook. Never hand-roll labels; `-a`/`--claim`
are bd's status, not the badge.

**Pick up work**: `wyk inbox` FIRST (items bounced back to you — WORK them),
then `wyk` / `bd ready`. `wyk conventions` prints the full contract.

**Something wrong? Act — don't shrug.** If a wyk/bd command errors, a
convention looks broken, or the workflow rubs wrong, file a bd issue (with
an owner) and fix or hand it off — don't route around it silently.
Friction with wyk is product data; surfacing it is the job.
<!-- END WYK CONVENTIONS -->
