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
LOG="$HOME/Library/Logs/louisville-bot-heartbeat.log"

log() {
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >>"$LOG"
}

# Probe 1: the app answers and reports itself healthy.
health=$(curl -fsS -m 10 "$BASE_URL/api/health" 2>/dev/null)

# Probe 2: the API path itself is reachable end-to-end. An empty question
# short-circuits to an SSE error with no LLM call, so this is free — and it is
# the only probe that catches an edge rule blocking /api while / still loads.
ask_code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 \
    -X POST "$BASE_URL/api/ask" \
    -H 'Content-Type: application/json' \
    --data '{"question":""}' 2>/dev/null)

case "$health" in
    *ok*) health_ok=1 ;;
    *)    health_ok=0 ;;
esac

if [ "$health_ok" = 1 ] && [ "$ask_code" = '200' ]; then
    curl -fsS -m 10 --retry 3 "https://hc-ping.com/$CHECK_UUID" >/dev/null 2>&1
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

log "container state='$state' - attempting docker start"
if "$DOCKER" start "$CONTAINER" >/dev/null 2>&1; then
    log 'docker start succeeded'
    curl -fsS -m 10 \
        -H 'Title: louisville-bot auto-restarted' \
        -H 'Priority: high' \
        -H 'Tags: warning' \
        -d "Container was '$state'; the watchdog ran docker start. Check why it exited." \
        "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1
else
    log 'docker start FAILED'
fi
