# Monitoring — louisville.raylytics.io uptime alerting

Alerting for when **https://louisville.raylytics.io/** stops serving. Built 2026-08-11
after a 5-hour unnoticed outage (Cloudflare 502 from ~15:01 UTC until manually
restarted at ~20:30 UTC).

## Why the existing alert didn't catch it

The Air server has a `mac-server` dead-man's-switch check (see
`~/Projects/personal/mac-server/monitoring/README.md`) that pings healthchecks.io every
60s. During this outage **the Air was up and pinging happily** — only the
`louisville-bot` container was dead. A host heartbeat cannot see that, so a second,
**service-level** check was added rather than duplicating the host one.

```
Air (launchd, every 60s)                     healthchecks.io "louisville-bot" check
  probe https://louisville.raylytics.io      period 1 min, grace 3 min
    ├── GET  /api/health   → serving?          │ no ping for ~4 min → DOWN
    └── POST /api/ask      → HTTP 200?         ├─▶ ntfy.sh push, MAX priority (bypasses DND)
  both pass → curl hc-ping.com/<uuid>  ──────▶ └─▶ email to evan.j.ray@gmail.com
  either fails → no ping (silence = alarm)
                 + self-heal if container exited
```

- **Detection latency:** ~4 minutes.
- **What it covers:** container dead/hung/exited, app failing to boot, cloudflared
  tunnel down, Cloudflare edge blocking `/api`, and (redundantly with `mac-server`)
  total host loss.
- **Recovery:** healthchecks sends an "UP" notification when pings resume.

The probe deliberately hits the **public URL**, not `localhost:8000` — that is the only
way to catch tunnel/edge failures, and it is the path real users take. It also POSTs to
`/api/ask`, because loading the page proves nothing about the API: a Cloudflare WAF rule
once 403'd `/api` while `/` served fine.

## Components

### 1. Probe — `monitoring/louisville-bot-heartbeat.sh` (on the Air)

Installed as a LaunchAgent from a git checkout at
`/Users/macserver/projects/louisville-open-data`, so `git pull` is the update path.

An empty `{"question":""}` POST short-circuits to an SSE error with **no LLM call**, so
the once-a-minute probe costs nothing and cannot burn Cerebras quota. At 1 req/min it
also stays well under the app's 5/min per-IP rate limit.

**What counts as "serving":** the health gate matches the `"status"` *field*, not the
response body. The body carries table names and raw exception text (`last_error`), and
`token` / `max_tokens` contain `ok` as a substring — a bare substring match would go
green based on the spelling of the most recent error. `status:"degraded"` counts as
**up**: `app.py` sets it at >5 upstream errors in an hour, which is routine on a
free-tier LLM backend and says nothing about reachability. Paging max-priority through
Do Not Disturb for that would be a false alarm. A non-2xx, a timeout, or an
unrecognized body withholds the heartbeat.

**Sustained degradation still surfaces.** After **30 consecutive** degraded probes
(~30 min) the script sends one `default`-priority ntfy notice — quiet enough not to be
a page, loud enough not to be invisible. This closes a real hole: probe 2 sends an empty
question, which `app.py:869` answers *before* reaching the LLM, so a completely dead
backend still returns 200. That is not hypothetical — it is the documented Cerebras
model-deprecation case, where live queries all 404 while cached starter answers keep
working. Without escalation the bot could serve nothing but cache indefinitely with the
check showing green. The counter resets on the first healthy probe and on any hard
failure, so a degraded → down → degraded sequence re-arms.

Logs to `~/Library/Logs/louisville-bot-heartbeat.log` (failures and self-heals only —
successful probes are silent).

### 2. Self-heal — the gap autoheal can't cover

The Air runs an `autoheal` container that restarts containers Docker marks *unhealthy*.
It does **nothing** for a container that has fully **exited** — which is exactly how the
Aug 11 outage persisted: autoheal's own restart attempt failed
(`Restarting container 72b2ed9d32a8 failed`) and left `louisville-bot` in
`Exited (255)`, where `--restart unless-stopped` did not revive it.

So when a probe fails, the script checks the container's state and runs `docker start`
only if it is not running. That sends a **high**-priority (not max) ntfy note so a
silent restart loop can't hide. Alerting does not depend on this working: the missing
heartbeat fires the max-priority alarm either way.

**Bounded on purpose.** At most one restart per **10 minutes**, and at most **3**
attempts before the watchdog stands down and lets the withheld heartbeat carry the
alarm alone. A container that keeps exiting (bad image, missing volume, OOM, bad
`MODEL`) is not something restarting fixes, and without a cap the once-a-minute retry
would fire ~60 high-priority pushes an hour onto the *same topic* as the max-priority
down alert — burying the notification that matters under the noise it was meant to
surface. The counter resets on the first healthy probe. State lives in
`~/Library/Application Support/louisville-bot-heartbeat/`.

If that state cannot be written (a full disk — exactly the host-pressure condition
behind the original outage), the script **stands down instead of restarting**. Failing
open there would silently disable the cooldown and cap and restore the every-60s
restart-and-push loop; the withheld heartbeat still raises the alarm either way.

The degraded counter lives in the same place, so an unwritable state dir also disables
the sustained-degradation escalation — including when the counter file is *readable but
not writable* (left root-owned by a single `sudo` run), which freezes it at a stale
value. A frozen counter is never acted on: escalating off a number the script has just
proved it cannot maintain would re-fire the notice every 60s forever.

Both cases log a `WARN`, deduped against the log itself since the state needed to dedupe
is the thing that is broken. The dedupe is scoped to **the current day**, not to all
history: nothing rotates this log, so matching the whole file would silence every
recurrence after the first — and a silent repeat of the degraded case is the
green-check-over-a-dead-backend hole all over again. Once a day is visible without being
per-minute spam.

### 3. Check — healthchecks.io `louisville-bot`

- Project: `evan.j.ray@gmail.com` (free tier, 4 of 20 checks used)
- Ping URL: `https://hc-ping.com/e5f9d7ce-478f-49c5-b3c3-f3be7a7442e6`
  > ⚠️ **Treat the ping UUID as a credential.** Anyone with it can spoof pings to keep
  > the check green through a real outage, or hit `/fail` to fire max-priority pushes.
  > Committed here deliberately (private repo); if the repo goes public, rotate it:
  > Danger Zone → *Create a Copy* (keeps schedule + integrations, gets a fresh UUID),
  > point the script at the new URL, verify pings land, delete the old check.
- Schedule: **period 1 minute, grace 3 minutes**
- Notification methods (both ON, both inherited from the existing setup):
  - **email** `mac-server` → evan.j.ray@gmail.com
  - **ntfy** `air-server ntfy push` → topic `air-server-evan-ee4b28dd81`, max priority
    on down. Same topic as the host alert, so no new phone subscription is needed —
    the message body names which check fired.

### 4. Container health check start period

Contributing cause of the outage, fixed at the same time. The container was created with
`--health-start-period 120s`, but cold startup (CSV load → DuckDB → RAG corpus →
response cache) took **over 4 minutes** under host memory/CPU pressure. Health checks
began before the app was listening, marked it unhealthy, and autoheal restart-looped it
roughly every 5–6 minutes from 10:23 to 11:00 UTC. Now **600s**, both on the running
container and in the `docker run` line in the root `CLAUDE.md` deploy snippet, so a
redeploy from the runbook cannot silently reinstate the 120s window.

### 5. Tests

`tests/test_heartbeat_script.py` drives the script with stub `curl`/`docker`/`date` on
`PATH` and `$HOME` pointed at a tmpdir — no network, no Docker, no real waiting. It
covers the health gate, the degraded escalation, the cooldown, the cap, the stand-down,
the recovery reset, and the unwritable-state case. Runs as part of `python -m pytest -q`.
Worth keeping: every failure mode here is silent by construction and only manifests
during an outage, which is when nobody is reading the script.

## Deploy / update

The MCP shell runner takes one command at a time (no `&&`, `;`, `|`, or redirection):

```bash
git -C /Users/macserver/projects/louisville-open-data pull
launchctl bootout gui/501/com.raylytics.louisville-bot-healthcheck
launchctl bootstrap gui/501 /Users/macserver/Library/LaunchAgents/com.raylytics.louisville-bot-healthcheck.plist
```

If the plist itself changed, `cp` it into `~/Library/LaunchAgents/` before bootstrapping.

> ⚠️ **LaunchAgents only run inside a logged-in GUI session.** The Air auto-logs-in as
> `macserver`, so this is fine — but if auto-login is ever disabled, the heartbeat dies
> at the login screen and you get a false "down" alert after every reboot.

## Testing

```bash
# Fire the whole alert chain without touching the server (auto-recovers next minute):
curl https://hc-ping.com/e5f9d7ce-478f-49c5-b3c3-f3be7a7442e6/fail

# See what ntfy actually delivered, no phone needed:
curl -s "https://ntfy.sh/air-server-evan-ee4b28dd81/json?poll=1&since=10m"

# Real drill — stop the container and wait out the grace period (~4-5 min).
# NOTE: the watchdog will `docker start` it again within 60s, which is the point;
# to test the alert itself, use /fail above instead.
```

Verified end-to-end 2026-08-11: `/fail` → max-priority ntfy + email delivered; next
60s heartbeat flipped it back UP.
