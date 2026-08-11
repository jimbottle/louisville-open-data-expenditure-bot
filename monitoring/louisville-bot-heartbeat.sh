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
DOCKER='/usr/local/bin/docker'
LOG_DIR="$HOME/Library/Logs"
LOG="$LOG_DIR/louisville-bot-heartbeat.log"
STATE_DIR="$HOME/Library/Application Support/louisville-bot-heartbeat"
HEAL_COUNT_FILE="$STATE_DIR/heal_attempts"
HEAL_TIME_FILE="$STATE_DIR/heal_last_epoch"
DEGRADED_FILE="$STATE_DIR/degraded"

# At most one self-heal per cooldown, and only so many before giving up. A
# container that keeps exiting is not something restarting will fix, and each
# attempt costs a push on the same topic as the max-priority down alert.
HEAL_COOLDOWN=600
HEAL_MAX_ATTEMPTS=3

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

write_state() {
    mkdir -p "$STATE_DIR" 2>/dev/null
    echo "$2" >"$1"
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
# be a false alarm; it is logged instead.
degraded=0
case "$health" in
    *'"status":"ok"'*) health_ok=1 ;;
    *'"status":"degraded"'*) health_ok=1; degraded=1 ;;
    *) health_ok=0 ;;
esac

if [ "$health_ok" = 1 ] && [ "$ask_code" = '200' ]; then
    curl -fsS -m 10 --retry 3 "https://hc-ping.com/$CHECK_UUID" >/dev/null 2>&1

    # Log degraded once per episode rather than every 60s.
    if [ "$degraded" = 1 ]; then
        if [ ! -f "$DEGRADED_FILE" ]; then
            log 'app reports status=degraded (>5 upstream errors in the last hour) - still serving, not paging'
            write_state "$DEGRADED_FILE" 1
        fi
    else
        rm -f "$DEGRADED_FILE" 2>/dev/null
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
write_state "$HEAL_COUNT_FILE" "$attempts"
write_state "$HEAL_TIME_FILE" "$now"

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
