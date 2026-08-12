"""Behaviour tests for monitoring/louisville-bot-heartbeat.sh.

The script is the thing that decides whether production is considered up, and
every one of its failure modes is silent by construction: a regression in the
health gate or the self-heal cap only shows up during an outage, which is the
one moment nobody is reading it.

It takes all of its external state from curl, docker, date, $HOME and files
under the state dir, so it can be driven entirely by stubs on PATH — no network,
no Docker, no waiting on a real clock.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "monitoring" / "louisville-bot-heartbeat.sh"

# Stub curl: appends a marker per outbound call so tests can assert on intent,
# and serves the health/ask responses the test asked for.
CURL_STUB = """#!/bin/sh
for a in "$@"; do
  case "$a" in
    *hc-ping.com*) echo PING >>"$CALLS"; echo OK; exit 0 ;;
    *ntfy.sh*)     echo "NTFY:${NTFY_PRIORITY_SEEN:-}" >>"$CALLS"; exit 0 ;;
    */api/health)  [ -n "$FAKE_HEALTH" ] || exit 22; printf '%s' "$FAKE_HEALTH"; exit 0 ;;
    */api/ask)     printf '%s' "${FAKE_ASK:-000}"; exit 0 ;;
  esac
done
exit 0
"""

# Records the Priority header so the degraded notice can be told apart from the
# self-heal push.
CURL_STUB_WITH_PRIORITY = """#!/bin/sh
prio=none
prev=
for a in "$@"; do
  case "$prev" in -H) case "$a" in Priority:*) prio=${a#Priority: } ;; esac ;; esac
  prev=$a
done
for a in "$@"; do
  case "$a" in
    *hc-ping.com*) echo PING >>"$CALLS"; echo OK; exit 0 ;;
    *ntfy.sh*)     echo "NTFY:$prio" >>"$CALLS"; exit 0 ;;
    */api/health)  [ -n "$FAKE_HEALTH" ] || exit 22; printf '%s' "$FAKE_HEALTH"; exit 0 ;;
    */api/ask)     printf '%s' "${FAKE_ASK:-000}"; exit 0 ;;
  esac
done
exit 0
"""

DOCKER_STUB = """#!/bin/sh
case "$1" in
  inspect) printf '%s' "${FAKE_STATE:-}" ;;
  start)   echo DOCKER_START >>"$CALLS"; [ "${FAKE_START_OK:-1}" = 1 ] ;;
esac
"""

# $FAKE_NOW drives the cooldown arithmetic and $FAKE_DAY the calendar day, which
# has to stay consistent between the log timestamp and warn_once's date scope.
DATE_STUB = """#!/bin/sh
day=${FAKE_DAY:-2026-01-01}
for a in "$@"; do
  case "$a" in
    +%s) echo "${FAKE_NOW:-1000000}"; exit 0 ;;
    +%Y-%m-%d) echo "$day"; exit 0 ;;
  esac
done
echo "${day}T00:00:00Z"
"""


class Harness:
    def __init__(self, tmp_path, curl_stub=CURL_STUB):
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.bin = tmp_path / "bin"
        self.calls = tmp_path / "calls"
        self.home.mkdir()
        self.bin.mkdir()
        for name, body in (
            ("curl", curl_stub),
            ("docker", DOCKER_STUB),
            ("date", DATE_STUB),
        ):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)

    @property
    def log(self):
        path = self.home / "Library" / "Logs" / "louisville-bot-heartbeat.log"
        return path.read_text() if path.exists() else ""

    @property
    def state_dir(self):
        return self.home / "Library" / "Application Support" / "louisville-bot-heartbeat"

    def run(self, health="", ask="000", state="", now=1000000, start_ok=True, day="2026-01-01"):
        """One invocation of the script. Returns the list of outbound calls."""
        self.calls.write_text("")
        env = dict(
            os.environ,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            HOME=str(self.home),
            CALLS=str(self.calls),
            DOCKER=str(self.bin / "docker"),
            FAKE_HEALTH=health,
            FAKE_ASK=ask,
            FAKE_STATE=state,
            FAKE_NOW=str(now),
            FAKE_DAY=day,
            FAKE_START_OK="1" if start_ok else "0",
        )
        proc = subprocess.run(
            ["/bin/sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
        )
        assert proc.returncode == 0, f"script exited {proc.returncode}: {proc.stderr}"
        return self.calls.read_text().split()


@pytest.fixture
def hb(tmp_path):
    return Harness(tmp_path)


OK_BODY = '{"status":"ok","tables":{"expenditures":1}}'
DEGRADED_BODY = '{"status":"degraded","errors":{"errors_last_hour":9}}'
# The pre-fix gate substring-matched the whole body, so "max_tokens" (which
# contains "ok") made an unhealthy app look healthy.
UNHEALTHY_WITH_OK_SUBSTRING = (
    '{"status":"unhealthy","errors":{"last_error":"max_tokens exceeded, broken pipe"}}'
)


def test_healthy_pings(hb):
    assert hb.run(health=OK_BODY, ask="200") == ["PING"]


def test_status_word_elsewhere_in_body_does_not_count_as_healthy(hb):
    assert hb.run(health=UNHEALTHY_WITH_OK_SUBSTRING, ask="200") == []
    assert "PROBE FAILED" in hb.log


def test_unreachable_health_withholds_ping(hb):
    assert hb.run(health="", ask="200") == []


def test_ask_endpoint_failure_withholds_ping(hb):
    assert hb.run(health=OK_BODY, ask="503") == []


def test_degraded_still_pings_and_logs_once_per_episode(hb):
    assert hb.run(health=DEGRADED_BODY, ask="200") == ["PING"]
    assert hb.run(health=DEGRADED_BODY, ask="200") == ["PING"]
    assert hb.log.count("status=degraded") == 1


def test_recovery_then_new_degraded_episode_logs_again(hb):
    hb.run(health=DEGRADED_BODY, ask="200")
    hb.run(health=OK_BODY, ask="200")
    hb.run(health=DEGRADED_BODY, ask="200")
    assert hb.log.count("status=degraded") == 2


def test_hard_failure_resets_degraded_episode(hb):
    """A degraded -> down -> degraded sequence must re-arm, not stay latched."""
    hb.run(health=DEGRADED_BODY, ask="200")
    hb.run(health="", ask="000")
    hb.run(health=DEGRADED_BODY, ask="200")
    assert hb.log.count("status=degraded") == 2


def test_sustained_degraded_escalates_once_at_default_priority(tmp_path):
    """A dead LLM backend keeps returning 200, so silence here would be a hole."""
    hb = Harness(tmp_path, curl_stub=CURL_STUB_WITH_PRIORITY)
    pushes = []
    for _ in range(40):
        pushes += [c for c in hb.run(health=DEGRADED_BODY, ask="200") if c.startswith("NTFY")]
    assert pushes == ["NTFY:default"], "expected exactly one default-priority notice"
    assert "30 consecutive probes" in hb.log


def test_self_heal_restarts_exited_container(hb):
    calls = hb.run(health="", ask="000", state="exited")
    assert "DOCKER_START" in calls
    assert any(c.startswith("NTFY") for c in calls)


def test_self_heal_skipped_while_container_is_running(hb):
    assert hb.run(health="", ask="000", state="running") == []


def test_self_heal_respects_cooldown(hb):
    assert "DOCKER_START" in hb.run(health="", ask="000", state="exited", now=1_000_000)
    assert hb.run(health="", ask="000", state="exited", now=1_000_060) == []
    assert hb.run(health="", ask="000", state="exited", now=1_000_500) == []
    assert "cooldown" in hb.log
    assert "DOCKER_START" in hb.run(health="", ask="000", state="exited", now=1_000_601)


def test_self_heal_stands_down_after_cap(hb):
    now = 1_000_000
    for _ in range(3):
        assert "DOCKER_START" in hb.run(health="", ask="000", state="exited", now=now)
        now += 700
    assert hb.run(health="", ask="000", state="exited", now=now) == []
    assert "standing down" in hb.log
    # And it stays stood down rather than resuming on the next cooldown.
    assert hb.run(health="", ask="000", state="exited", now=now + 5000) == []


def test_recovery_resets_self_heal_counter(hb):
    now = 1_000_000
    for _ in range(3):
        hb.run(health="", ask="000", state="exited", now=now)
        now += 700
    assert hb.run(health="", ask="000", state="exited", now=now) == []

    hb.run(health=OK_BODY, ask="200", state="running", now=now)
    assert not (hb.state_dir / "heal_attempts").exists()
    assert "DOCKER_START" in hb.run(health="", ask="000", state="exited", now=now + 1)


# Permission bits do not restrain root, so these cases would silently pass.
requires_nonroot = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="permission checks are void as root"
)


def _make_state_dir_unwritable(hb):
    hb.state_dir.parent.mkdir(parents=True, exist_ok=True)
    hb.state_dir.mkdir(exist_ok=True)
    hb.state_dir.chmod(0o500)  # readable, not writable


@requires_nonroot
def test_unwritable_state_dir_stands_down_instead_of_looping(hb):
    """Fail safe: without persistable state the cooldown and cap cannot hold, so
    restarting anyway would restore the once-a-minute restart-and-push loop."""
    _make_state_dir_unwritable(hb)
    try:
        for _ in range(5):
            calls = hb.run(health="", ask="000", state="exited")
            assert calls == [], f"expected no restart or push, got {calls}"
        # Warned, but not once per minute: the log dir stays writable even when
        # the state dir does not, so an unbounded warning would be real spam.
        assert hb.log.count("cannot persist self-heal state") == 1
    finally:
        hb.state_dir.chmod(0o700)


@requires_nonroot
def test_unwritable_state_dir_disables_degraded_escalation_loudly(tmp_path):
    """The degraded counter cannot advance without persistable state, so the
    escalation silently dies. It has to say so — once — rather than either
    pretending nothing is wrong or repeating a line every 60s."""
    hb = Harness(tmp_path, curl_stub=CURL_STUB_WITH_PRIORITY)
    _make_state_dir_unwritable(hb)
    try:
        pushes = []
        for _ in range(40):
            calls = hb.run(health=DEGRADED_BODY, ask="200")
            assert "PING" in calls, "a degraded app is still serving and must be pinged"
            pushes += [c for c in calls if c.startswith("NTFY")]
        assert pushes == [], "counter cannot advance, so escalation must not claim to fire"
        assert hb.log.count("escalation disabled") == 1
        # The routine first-cycle line is suppressed rather than repeated 40x.
        assert hb.log.count("status=degraded") == 0
    finally:
        hb.state_dir.chmod(0o700)


@requires_nonroot
def test_state_warning_repeats_on_a_later_day(tmp_path):
    """Nothing rotates this log, so deduping against all of history would make
    every recurrence after the first completely silent — which for the degraded
    path is the green-check-over-a-dead-backend hole all over again."""
    hb = Harness(tmp_path, curl_stub=CURL_STUB_WITH_PRIORITY)
    _make_state_dir_unwritable(hb)
    try:
        hb.run(health=DEGRADED_BODY, ask="200", day="2026-01-01")
        hb.run(health=DEGRADED_BODY, ask="200", day="2026-01-01")
        assert hb.log.count("escalation disabled") == 1, "same day should warn once"

        hb.run(health=DEGRADED_BODY, ask="200", day="2026-03-14")
        assert hb.log.count("escalation disabled") == 2, "a later incident must warn again"
    finally:
        hb.state_dir.chmod(0o700)


@requires_nonroot
def test_frozen_counter_does_not_re_escalate_every_cycle(tmp_path):
    """A readable-but-unwritable counter file (left root-owned by one sudo run)
    freezes the count. Acting on that value would re-fire the escalation every
    60s forever, on the same topic as the max-priority down alert."""
    hb = Harness(tmp_path, curl_stub=CURL_STUB_WITH_PRIORITY)
    hb.state_dir.mkdir(parents=True, exist_ok=True)
    counter = hb.state_dir / "degraded_cycles"
    counter.write_text("29\n")  # one short of DEGRADED_ALERT_AFTER
    counter.chmod(0o444)
    try:
        pushes = []
        for _ in range(10):
            pushes += [c for c in hb.run(health=DEGRADED_BODY, ask="200") if c.startswith("NTFY")]
        assert pushes == [], f"stale counter must not escalate, got {pushes}"
        assert hb.log.count("escalation disabled") == 1
    finally:
        counter.chmod(0o644)


def test_script_is_posix_sh_clean():
    assert shutil.which("sh")
    proc = subprocess.run(["/bin/sh", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
