#!/bin/sh
# Dead-man's-switch heartbeat for the Louisville bot.
#
# Runs every 60s on the Air (LaunchAgent com.raylytics.louisville-bot-healthcheck).
# It pings healthchecks.io ONLY if https://louisville.raylytics.io is actually
# serving the API. Silence for ~4 min (period 1m + grace 3m) fires a max-priority
# ntfy push + email. See monitoring/README.md.
#
# Deliberately probes the PUBLIC URL, not localhost: that covers the container,
# the cloudflared tunnel, and the Cloudflare edge in one shot. The separate
# mac-server check already covers "the whole machine is gone", so this one's job
# is to distinguish "the bot is down" from "the host is down".

CHECK_UUID='e5f9d7ce-478f-49c5-b3c3-f3be7a7442e6'
BASE_URL='https://louisville.raylytics.io'
NTFY_TOPIC='air-server-evan-ee4b28dd81'
CONTAINER='louisville-bot'
# Overridable so the test harness can point at a stub; production leaves it be.
DOCKER="${DOCKER:-/usr/local/bin/docker}"
LOG_DIR="$HOME/Library/Logs"
LOG="$LOG_DIR/louisville-bot-heartbeat.log"
STATE_DIR="$HOME/Library/Application Support/louisville-bot-heartbeat"
HEAL_COUNT_FILE="$STATE_DIR/heal_attempts"
HEAL_TIME_FILE="$STATE_DIR/heal_last_epoch"
DEGRADED_COUNT_FILE="$STATE_DIR/degraded_cycles"

# At most one self-heal per cooldown, and only so many before giving up. A
# container that keeps exiting is not something restarting will fix, and each
# attempt costs a push on the same topic as the max-priority down alert.
HEAL_COOLDOWN=600
HEAL_MAX_ATTEMPTS=3

# Consecutive degraded probes (~30 min) before a low-priority nudge. Long enough
# that a routine burst of upstream errors ages out on its own.
DEGRADED_ALERT_AFTER=30

log() {
    mkdir -p "$LOG_DIR" 2>/dev/null
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >>"$LOG"
}

# Reads a counter file as an integer, treating missing/corrupt as 0.
read_int() {
    _v=$(cat "$1" 2>/dev/null)
    case "$_v" in
        '' | *[!0-9]*) echo 0 ;;
        *) echo "$_v" ;;
    esac
}

# Returns non-zero if the state could not be persisted. Callers must treat that
# as fail-safe: an unwritable state dir (a full disk is exactly the host-pressure
# condition behind the original outage) would otherwise silently disable the
# cooldown and cap, restoring the once-a-minute restart-and-push loop.
write_state() {
    mkdir -p "$STATE_DIR" 2>/dev/null
    echo "$2" >"$1" 2>/dev/null
}

# Probe 1: the app answers on the health endpoint.
health=$(curl -fsS -m 10 "$BASE_URL/api/health" 2>/dev/null)

# Probe 2: the API path itself is reachable end-to-end. An empty question
# short-circuits to an SSE error with no LLM call, so this is free — and it is
# the only probe that catches an edge rule blocking /api while / still loads.
ask_code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 \
    -X POST "$BASE_URL/api/ask" \
    -H 'Content-Type: application/json' \
    --data '{"question":""}' 2>/dev/null)

# Match the status FIELD, not the payload. The health body carries table names
# and raw exception text (app.py's `last_error`), and "token"/"max_tokens"
# contain "ok" as a substring — a bare *ok* match would go green on the spelling
# of the most recent error. FastAPI emits compact separators, so there is no
# whitespace to allow for.
#
# `degraded` counts as SERVING: app.py sets it at >5 upstream errors in an hour,
# which on a free-tier LLM backend is routine and says nothing about whether the
# site is reachable. Paging max-priority through Do Not Disturb for that would
# be a false alarm — but a SUSTAINED degraded state must not be silent either.
# Probe 2 sends an empty question, which app.py:869 answers before reaching the
# LLM, so a dead backend (e.g. Cerebras deprecating MODEL, the exact case in
# CLAUDE.md) still returns 200. Without escalation the bot could serve nothing
# but cached answers indefinitely while the check stayed green.
degraded=0
case "$health" in
    *'"status":"ok"'*) health_ok=1 ;;
    *'"status":"degraded"'*) health_ok=1; degraded=1 ;;
    *) health_ok=0 ;;
esac

if [ "$health_ok" = 1 ] && [ "$ask_code" = '200' ]; then
    curl -fsS -m 10 --retry 3 "https://hc-ping.com/$CHECK_UUID" >/dev/null 2>&1

    # Count consecutive degraded probes: log the first, escalate a sustained run
    # at default priority so it is distinguishable from the max-priority down alert.
    if [ "$degraded" = 1 ]; then
        degraded_cycles=$(($(read_int "$DEGRADED_COUNT_FILE") + 1))
        write_state "$DEGRADED_COUNT_FILE" "$degraded_cycles"
        if [ "$degraded_cycles" -eq 1 ]; then
            log 'app reports status=degraded (>5 upstream errors in the last hour) - still serving, not paging'
        fi
        if [ "$degraded_cycles" -eq "$DEGRADED_ALERT_AFTER" ]; then
            log "app has reported degraded for $degraded_cycles consecutive probes - sending low-priority notice"
            curl -fsS -m 10 \
                -H 'Title: louisville-bot degraded' \
                -H 'Priority: default' \
                -H 'Tags: warning' \
                -d "The site is serving, but /api/health has reported degraded for $degraded_cycles minutes straight. Live queries may all be failing (check MODEL against the provider's current model list)." \
                "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1
        fi
    else
        rm -f "$DEGRADED_COUNT_FILE" 2>/dev/null
    fi

    # A healthy probe ends any self-heal episode.
    if [ "$(read_int "$HEAL_COUNT_FILE")" -gt 0 ]; then
        log 'probe healthy again - resetting self-heal counter'
        rm -f "$HEAL_COUNT_FILE" "$HEAL_TIME_FILE" 2>/dev/null
    fi
    exit 0
fi

# Withholding the ping IS the alert — don't try to notify from a host that may
# itself be the thing that's broken.
log "PROBE FAILED health='$health' ask_http='$ask_code' - withholding heartbeat"

# A hard failure ends any degraded episode, so a degraded → down → degraded
# sequence logs and escalates the second episode too.
rm -f "$DEGRADED_COUNT_FILE" 2>/dev/null

# Self-heal the one gap autoheal cannot cover: autoheal restarts containers that
# are running-but-unhealthy, and does nothing for a container that has fully
# exited (which is how the 2026-08-11 outage stayed down for 5 hours).
[ -x "$DOCKER" ] || exit 0

state=$("$DOCKER" inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null)
[ -n "$state" ] || exit 0
[ "$state" = 'running' ] && exit 0

attempts=$(read_int "$HEAL_COUNT_FILE")
if [ "$attempts" -ge "$HEAL_MAX_ATTEMPTS" ]; then
    log "container state='$state' - $attempts self-heal attempts spent, standing down; the withheld heartbeat carries the alarm"
    exit 0
fi

now=$(date '+%s')
since=$((now - $(read_int "$HEAL_TIME_FILE")))
if [ "$since" -lt "$HEAL_COOLDOWN" ]; then
    log "container state='$state' - last self-heal ${since}s ago, waiting out the ${HEAL_COOLDOWN}s cooldown"
    exit 0
fi

attempts=$((attempts + 1))
if ! write_state "$HEAL_COUNT_FILE" "$attempts" || ! write_state "$HEAL_TIME_FILE" "$now"; then
    log "WARN: cannot persist self-heal state under '$STATE_DIR' - standing down rather than restarting without a cooldown"
    exit 0
fi

log "container state='$state' - attempting docker start ($attempts/$HEAL_MAX_ATTEMPTS)"
if "$DOCKER" start "$CONTAINER" >/dev/null 2>&1; then
    log 'docker start succeeded'
    curl -fsS -m 10 \
        -H 'Title: louisville-bot auto-restarted' \
        -H 'Priority: high' \
        -H 'Tags: warning' \
        -d "Container was '$state'; the watchdog ran docker start ($attempts/$HEAL_MAX_ATTEMPTS). Check why it exited." \
        "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1
else
    log 'docker start FAILED'
fi
