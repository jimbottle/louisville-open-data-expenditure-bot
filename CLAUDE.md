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
# TRUSTED_PROXY_IPS: the bridge-gateway IP the tunnel's requests arrive from.
# REQUIRED — without it the per-IP rate limit keys on that single peer and acts
# site-wide (5/min for ALL users). Find it: docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}'  (usually 172.17.0.1).
# ADMIN_TOKEN: any long random secret; gates /api/cache (warm_cache.py and
# refresh_data.py must be run with the same ADMIN_TOKEN in their env).
docker run -d --name louisville-bot --restart unless-stopped -p 8000:8000 \
  -v louisville-data:/data:ro -v louisville-state:/state -v louisville-logs:/logs \
  -e CEREBRAS_PAID_API_KEY=... -e MODEL=gpt-oss-120b \
  -e LLM_BASE_URL=https://api.cerebras.ai/v1 -e DATA_DIR=/data -e STATS_DIR=/state -e LOG_DIR=/logs \
  -e TRUSTED_PROXY_IPS=172.17.0.1 -e ADMIN_TOKEN=... \
  --health-cmd "curl -sf --max-time 5 http://localhost:8000/api/health || exit 1" \
  --health-interval 30s --health-start-period 600s --health-retries 3 \
  louisville-bot:new
docker tag louisville-bot:new louisville-bot:latest
# Rollback: docker stop louisville-bot; docker rm louisville-bot; docker rename louisville-bot-prev louisville-bot; docker start louisville-bot
```

> ⚠️ **Set `TRUSTED_PROXY_IPS`** to the Docker bridge gateway (the peer the
> cloudflared tunnel reaches the container from). It is unset by default and the
> app logs a warning at startup when missing; without it the per-IP rate limit
> collapses to a single site-wide bucket. Verify the gateway with
> `docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}'`.

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

The old `docker-compose.yml` and `deploy.sh` were **removed** (they described a superseded SSH-rsync flow that leaked `.env` and carried the 45s health `start_period` that caused the 2026-08-11 outage). The authoritative deploy is the MCP Docker flow above; there is no compose/rsync path.

### Providers: OpenRouter primary, Cerebras fallback

**Cerebras retired its always-free tier in July 2026** (replaced by a one-time $5
trial that expires after 30 days), so the old free key answers **HTTP 402
`payment_required`** on every call. The bot's primary provider is now
**OpenRouter**, which still has genuinely free models; the Cerebras
pay-as-you-go key stays behind it as the fallback.

| | primary | fallback |
|---|---|---|
| key | `OPENROUTER_API_KEY` | `CEREBRAS_PAID_API_KEY` |
| base URL | `OPENROUTER_BASE_URL` (default `https://openrouter.ai/api/v1`) | `LLM_BASE_URL` (Cerebras) |
| model | `OPENROUTER_MODEL` (default `nvidia/nemotron-3-super-120b-a12b:free`) | `MODEL` (default `gpt-oss-120b`) |

The two providers have **separate base URLs and separate model ids** — a slug
like `vendor/model:free` means nothing to Cerebras — so `fallback_model` is
threaded alongside `fallback_client` through every LLM call. Drop
`OPENROUTER_API_KEY` and everything reverts to Cerebras-only.

The fallback engages on a 429 (after the retry ladder) and immediately on a 402;
a 402 with no working fallback shows the user `QUOTA_MSG` ("out of credit"),
never "try rewording your question".

**Picking the OpenRouter model.** Free slugs churn weekly, and the free roster is
uneven. Benchmarked 2026-08-18 against the real NL→SQL task (3 questions,
executed against DuckDB): `nvidia/nemotron-3-super-120b-a12b:free` 3/3 at ~7s,
`poolside/laguna-s-2.1:free` 3/3 at ~17s, `openai/gpt-oss-20b:free` 2/3 at ~29s
(one reply had no content at all), `google/gemma-4-31b-it:free` 0/3 (upstream
429s). Re-run that comparison before switching models rather than trusting a
model card. If a slug disappears, the 404 `model_not_found` handler switches to
another model — but only to one of `DEFAULT_OPENROUTER_MODEL_FALLBACKS`, or
failing that any `:free` slug the account can see. It will never cross into
OpenRouter's paid catalogue, and a replacement that had to be rescued by the
Cerebras fallback is not pinned (it would put a failing round trip in front of
every later question). If no free slug is available at all, the question is
served by Cerebras — through the normal retry ladder — and the primary is
latched out for 15 minutes (`PRIMARY_RECHECK_SECONDS`) so each later call skips
the dead 404 and the catalogue listing behind it, healing by itself if the slug
comes back.

**Free-tier limits.** OpenRouter caps `:free` models at 20 requests/minute and
**50 requests/day** — 1,000/day once the account has purchased $10 of credits.
The bot makes 2-3 LLM calls per question, so 50/day is roughly 16-25 questions
before the Cerebras fallback carries everything — the deliberate choice
(2026-08-18) is to let it, rather than buy credits. That cap arrives as a 429
saying `free-models-per-day`, which `is_daily_cap_error` routes onto the quota
path (immediate fallback, no 16s retry ladder, since it only resets at midnight
UTC). If both providers are exhausted the user sees `DAILY_CAP_MSG`, which says
the allowance resets — not `QUOTA_MSG`, which asks for money. Check a key's status with
`curl -s https://openrouter.ai/api/v1/key -H "Authorization: Bearer $OPENROUTER_API_KEY"`
(`is_free_tier` tells you which cap applies).

### LLM model (`MODEL` env)

Currently `gpt-oss-120b` on Cerebras. **Cerebras deprecates models without notice** — the bot previously ran `qwen-3-235b-a22b-instruct-2507`, which started returning `404 model_not_found` on every live call (cached starter answers still worked, which masked it). If live queries suddenly fail, list the account's current models and switch:

```bash
curl -s https://api.cerebras.ai/v1/models -H "Authorization: Bearer $CEREBRAS_PAID_API_KEY"   # pick a valid id
# then recreate the container with the new -e MODEL=... (no image rebuild needed)
```

`gpt-oss-120b` is a reasoning model: its chain-of-thought comes back in a separate `reasoning` field, the final answer is in `message.content`/`delta.content` (what the app reads), so reasoning never pollutes output as long as `max_tokens` leaves room after the reasoning tokens.

## AWS interaction policy (migration in progress)

The Lambda migration is tracked as bd epic `louisville-open-data-ru6`; the
analysis and cost model are in `LOU_MIGRATION_COMPAT.md`. Agents have
**standing authority to read AWS state** and **no authority to spend money
without a human in the loop**. Three tiers, enforced by
`.claude/settings.local.json` and stated here so the intent survives:

**Tier 1 — auto-allowed (read-only, zero cost).** `describe-*`, `list-*`,
`get-*` on Lambda, CloudFormation, DynamoDB, S3, CloudFront, ECR, IAM, Logs,
CloudWatch, Scheduler, WAF; `sts get-caller-identity`; and local IaC
evaluation (`cdk synth|diff|ls`, `terraform validate|plan`) which renders
templates and deploys nothing. Inspect freely — never guess at cloud state
that can simply be read.

**Tier 2 — ALWAYS ASK FIRST, EVERY TIME (anything that can appear on a bill).**
Confirmed preference, 2026-08-28: per-command approval, not per-session
batching. Not allowlisted, so it falls through to a normal permission prompt.
Do not batch these into a larger command to slip them past review, and do not
treat one approval as covering the next. Before asking, state **what will be
created, in which account and principal, and the expected monthly cost**:

- Any `create-*`, `put-*`, `update-*`, `run-*`, `deploy` verb.
- `cdk deploy` / `cdk bootstrap`, `terraform apply`.
- `s3 cp` / `s3 sync` of the 531 MB dataset — storage plus egress.
- `ecr` image pushes (~600 MB/image; free tier is 500 MB and only for 12 months).
- Anything creating a NAT Gateway, VPC endpoint, Aurora cluster, or
  provisioned-capacity resource. **These are out of architecture** — the plan
  is explicitly no-VPC/no-NAT because a NAT alone (~$32/mo) exceeds the entire
  cost envelope. Treat a proposed NAT as a design bug, not a purchase decision.
- `ce get-cost-and-usage` — the Cost Explorer API bills **$0.01 per request**.
  Small, but it is the one "read" that is not free, so it is Tier 2 on principle.

**Tier 3 — denied outright, no prompt.** Reading secret values back
(`ssm get-parameter`, `secretsmanager get-secret-value`) — keys are proven by
application behavior, never echoed, hashed, or printed (see the key-handling
memory). Also denied: `s3 rm`/`rb`, `dynamodb delete-table`,
`cloudformation delete-stack`, `cdk destroy`, `terraform destroy`,
`iam delete-*`, `iam create-access-key`. Teardown and credential minting are
human actions performed deliberately, not agent actions.

**Account discipline.** Target account is **012146975534** (`us-east-1`),
shared with an unrelated Airflow workload — decided 2026-08-28.

- Deploy as a **dedicated `lou-deploy` role**, scoped to Lou's resources.
  **Never** as `arn:aws:iam::012146975534:user/airflow-user`, the workstation's
  `default` profile: it is a long-lived key belonging to another workload.
  Until that role exists (`louisville-open-data-n0b`), provision nothing.
- Confirm `aws sts get-caller-identity` before every mutating operation and
  name the account **and principal** in the request.
- Because the account is shared, isolation is by convention and must be
  enforced: prefix every resource `lou-` and tag everything
  `Project=lou,ManagedBy=cdk`. Scope IaC to its own stack so a rollback cannot
  reach Airflow resources. Never issue a destructive command without a
  `lou-` prefix on the named resource.
- Cost attribution needs the tags to be right from the first deploy —
  retro-tagging does not fix historical Cost Explorer data.

## Build & Test

```bash
# Run the test suite (known-answer + canonicalization invariants)
python -m pytest -q

# Run locally for dev (serves frontend + API same-origin on :8000)
uvicorn app:app --host 127.0.0.1 --port 8000
# then open http://127.0.0.1:8000  (DEV ONLY — see "Where the bot runs" above)
```

### Prebuilt database artifact

Startup has two paths. By default the app rebuilds everything from the CSVs in
`DATA_DIR` (~5s, ~1.9GB RSS for Louisville) — fine for dev. Set `PREBUILT_DB`
and it instead opens a finished DuckDB file read-only (~0.5s, ~400MB, 112MB on
disk), which is what production should do:

```bash
python data_model.py --materialize data/lou.duckdb   # offline, ~8s; run in CI
PREBUILT_DB=data/lou.duckdb uvicorn app:app --port 8000
```

The build is atomic (writes `<path>.building`, then renames), so an interrupted
run cannot leave a half-populated artifact the app would open and serve from.
`PREBUILT_DB` pointing at a missing file **fails at boot on purpose** rather
than falling back to the CSV rebuild — on a container sized for the artifact,
that rebuild is exactly what exhausts memory and overruns the health-check
start period (the 2026-08-11 outage).

Rebuild the artifact after any `refresh_data.py` run: it is a snapshot, and a
stale one serves stale numbers with no other symptom. `tests/test_prebuilt_db.py`
asserts the artifact is byte-identical to the CSV path on schema, year context,
and the summary tables, and that the serving connection cannot reach the
filesystem (`read_csv` of an arbitrary path and `COPY TO` are both blocked —
`read_only=True` alone does not stop either).

## Architecture Overview

- **`app.py`** — FastAPI backend. Serves the static frontend AND the `/api/ask` SSE endpoint (same origin). Translates NL → SQL via an OpenAI-compatible client (Cerebras), runs it on DuckDB, streams an interpretation. Per-IP rate limit (5/min), persistent stats + response cache, structured SSE events (`status`, `reasoning`, `sql`, `results`, `chart`, `interpretation`, `log`, `debug`, `usage`, `error`, `info`, `done`).
- **`analytics_agent.py`** — LLM calls (reason → generate SQL → interpret), retry/fallback (free → paid Cerebras), SQL safety guard.
- **`data_model.py`** — generic city data engine: loads CSVs into DuckDB, builds `*_canonical` columns + summary tables, flags offsetting/artifact rows — all driven by a city config pack. Nothing city-specific lives here.
- **`city_config.py`** + **`cities/<city>/city.yaml`** — city config packs (sources/era mappings, canonical map CSVs, data-quality params, summary SQL, data dictionary). `CITY_CONFIG` env var selects the pack (default: Louisville). Format documented in `docs/canonical-model.md`. `cities/cincinnati/` is a **runnable** second-city pack (loads with `CITY_CONFIG=cities/cincinnati/city.yaml DATA_DIR=data_cincinnati`); `cities/kansas_city/` is still a paper config (not yet runnable).
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
