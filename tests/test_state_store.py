"""The DynamoDB state backend (state_store.py) against moto's in-process
DynamoDB — no AWS account, no docker.

What is pinned here is the CONTRACT app.py relies on, each point a documented
reason in app.py or the migration issues (69m, bb0, far):
  - the rate limit holds across independent DynamoState instances (i.e.
    across Lambda containers) and fails open when DynamoDB is unreachable;
  - cache: version-prefixed keys, LRU touch-on-hit, MAX_CACHE_ENTRIES cap
    enforced on insert, dead-citation entries dropped on read;
  - stats: counters survive a "container recycle" (a fresh instance), the
    error summary has exactly the keys /api/health exposes today, and
    errors_last_hour is a rolling hour.
"""
import json
import os
import time

import boto3
import pytest
from moto import mock_aws

import state_store as ss

TABLE = "lou-state-test"


@pytest.fixture
def dynamo():
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        ss.ensure_table(client, TABLE)
        yield client


def _state(client, **kw) -> ss.DynamoState:
    return ss.DynamoState(TABLE, client=client, **kw)


# ── rate limiter ────────────────────────────────────────────────────────────

def test_rate_limit_holds_across_instances(dynamo):
    """Two DynamoState objects = two Lambda containers sharing one table. The
    cap is on the TABLE, so container B is blocked by container A's requests."""
    a, b = _state(dynamo, rpm_limit=5), _state(dynamo, rpm_limit=5)
    now = 1_800_000_000.0
    allowed = [a.rate_allow("203.0.113.9", now) for _ in range(3)] + \
              [b.rate_allow("203.0.113.9", now) for _ in range(2)]
    assert allowed == [True] * 5
    assert a.rate_allow("203.0.113.9", now) is False
    assert b.rate_allow("203.0.113.9", now) is False
    # A different client is unaffected; the same client is fresh next minute.
    assert b.rate_allow("203.0.113.10", now) is True
    assert a.rate_allow("203.0.113.9", now + 60) is True


def test_rate_limit_bucket_expires_via_ttl(dynamo):
    s = _state(dynamo)
    now = 1_800_000_000.0
    s.rate_allow("1.2.3.4", now)
    item = dynamo.get_item(TableName=TABLE, Key={"pk": {"S": f"rl#1.2.3.4#{int(now // 60)}"}})["Item"]
    assert int(item["ttl"]["N"]) == (int(now // 60) + 2) * 60


def test_rate_limit_fails_open_when_dynamodb_is_unreachable(dynamo, caplog):
    """A DynamoDB outage must not take the site down: the function's reserved
    concurrency is the hard ceiling, the limiter is the polite one."""
    s = _state(dynamo)
    s.table = "no-such-table"
    assert s.rate_allow("1.2.3.4") is True
    assert "Rate limiter unavailable" in caplog.text


# ── response cache ──────────────────────────────────────────────────────────

FRAMES = ['data: {"type": "interpretation", "content": "x"}\n\n', 'data: {"type": "done"}\n\n']


def test_cache_round_trip_and_miss(dynamo):
    s = _state(dynamo)
    assert s.cache_get("v1:q") is None
    s.cache_put("v1:q", FRAMES)
    assert s.cache_get("v1:q") == FRAMES
    assert _state(dynamo).cache_get("v1:q") == FRAMES, "hit from a fresh container"


def test_cache_version_prefix_isolates_prompt_versions(dynamo):
    """A prompt change changes CACHE_VERSION; old answers must never be served
    and must not appear in the current-version listing."""
    s = _state(dynamo)
    s.cache_put("old1:q", FRAMES)
    s.cache_put("new1:q", FRAMES)
    assert s.cache_get("old1:q") == FRAMES  # still stored (TTL reclaims it)
    assert set(s.cache_items(prefix="new1:")) == {"new1:q"}
    assert s.cache_len(prefix="old1:") == 1


def test_cache_evicts_least_recently_used_not_oldest_inserted(dynamo):
    """The warm starter answers are inserted once and served forever; FIFO
    would evict exactly them. A served entry must survive over a newer,
    never-served one."""
    s = _state(dynamo, max_cache_entries=3)
    t = 1_800_000_000.0
    s.cache_put("v:starter", FRAMES, now=t)
    s.cache_put("v:q1", FRAMES, now=t + 1)
    s.cache_put("v:q2", FRAMES, now=t + 2)
    assert s.cache_get("v:starter", now=t + 3) == FRAMES   # LRU touch
    s.cache_put("v:q3", FRAMES, now=t + 4)                 # over the cap by one
    assert set(s.cache_items()) == {"v:starter", "v:q2", "v:q3"}
    assert s.cache_get("v:q1") is None


def test_cache_cap_is_enforced_on_every_insert(dynamo):
    s = _state(dynamo, max_cache_entries=2)
    for i in range(6):
        s.cache_put(f"v:q{i}", FRAMES, now=1_800_000_000.0 + i)
    assert s.cache_len() == 2
    assert set(s.cache_items()) == {"v:q4", "v:q5"}


def test_cache_drops_entries_with_dead_citation_links_on_read(dynamo):
    s = _state(dynamo)
    dead = ['data: {"type": "interpretation", "content": "see https://x/LegislationDetail.aspx?ID=1"}\n\n']
    s.cache_put("v:dead", dead)
    assert s.cache_get("v:dead") is None
    assert s.cache_len() == 0, "the dead entry is deleted, not just skipped"


def test_cache_delete_and_clear(dynamo):
    s = _state(dynamo)
    s.cache_put("v:a", FRAMES)
    s.cache_put("v:b", FRAMES)
    assert s.cache_delete("v:a") is True
    assert s.cache_delete("v:a") is False
    assert s.cache_clear() == 1
    assert s.cache_len() == 0


def test_cache_hit_refreshes_ttl(dynamo):
    s = _state(dynamo, cache_ttl_days=30)
    t = 1_800_000_000.0
    s.cache_put("v:q", FRAMES, now=t)
    s.cache_get("v:q", now=t + 10 * 86400)
    item = dynamo.get_item(TableName=TABLE, Key={"pk": {"S": "cache#v:q"}})["Item"]
    assert int(item["ttl"]["N"]) == int(t + 10 * 86400 + 30 * 86400)


def test_cache_entries_survive_a_scan_page_boundary(dynamo):
    """moto pages scans at 1 MB like DynamoDB; the listing must follow
    LastEvaluatedKey. Big frames force several pages."""
    s = _state(dynamo, max_cache_entries=100)
    big = ["data: " + "x" * 300_000 + "\n\n"]
    for i in range(8):
        s.cache_put(f"v:big{i}", big)
    assert s.cache_len() == 8


# ── stats ───────────────────────────────────────────────────────────────────

HEALTH_ERROR_KEYS = {
    "total_errors", "errors_last_hour", "sql_gen_errors", "sql_exec_errors",
    "interpretation_errors", "rate_limit_errors", "quota_errors", "last_quota_error",
    "quota_error_recent", "last_error", "last_error_time",
}


def test_error_summary_has_exactly_the_health_contract_keys(dynamo):
    """uptime.yml asserts on .errors.errors_last_hour; the heartbeat parses
    the same object. The shape is the contract."""
    s = _state(dynamo)
    assert set(s.stats_error_summary()) == HEALTH_ERROR_KEYS
    s.stats_error("sql_gen", "boom")
    assert set(s.stats_error_summary()) == HEALTH_ERROR_KEYS


def test_errors_survive_a_container_recycle_and_roll_off_after_an_hour(dynamo):
    t = 1_800_000_000.0
    a = _state(dynamo)
    a.stats_error("sql_gen", "one", now=t)
    a.stats_error("interpretation", "two", now=t + 10)
    b = _state(dynamo)  # "new container"
    summ = b.stats_error_summary(now=t + 20)
    assert summ["total_errors"] == 2
    assert summ["sql_gen_errors"] == 1 and summ["interpretation_errors"] == 1
    assert summ["errors_last_hour"] == 2
    assert summ["last_error"] == "interpretation: two"
    assert b.stats_error_summary(now=t + 3601)["errors_last_hour"] == 1
    assert b.stats_error_summary(now=t + 3700)["errors_last_hour"] == 0


def test_quota_errors_degrade_health_for_an_hour(dynamo):
    t = 1_800_000_000.0
    s = _state(dynamo)
    s.stats_error("quota", "402 payment_required", now=t)
    summ = s.stats_error_summary(now=t + 5)
    assert summ["quota_errors"] == 1
    assert summ["quota_error_recent"] is True
    assert summ["last_quota_error"] == "quota: 402 payment_required"
    assert s.stats_error_summary(now=t + 3601)["quota_error_recent"] is False
    s.stats_error("daily_cap", "free-models-per-day", now=t + 4000)
    assert s.stats_error_summary(now=t + 4001)["quota_errors"] == 2


def test_recent_error_list_is_pruned_past_the_threshold(dynamo):
    t = 1_800_000_000.0
    s = _state(dynamo)
    for i in range(ss._RECENT_PRUNE_AT + 5):
        s.stats_error("service", "x", now=t + i)
    item = dynamo.get_item(TableName=TABLE, Key={"pk": {"S": "stats#errors"}})["Item"]
    assert len(item["recent"]["L"]) <= ss._RECENT_PRUNE_AT + 5
    # Old timestamps beyond the hour are what gets dropped.
    s.stats_error("service", "later", now=t + 10_000)
    item = dynamo.get_item(TableName=TABLE, Key={"pk": {"S": "stats#errors"}})["Item"]
    assert len(item["recent"]["L"]) < ss._RECENT_PRUNE_AT
    assert s.stats_error_summary(now=t + 10_001)["errors_last_hour"] == 1


def test_usage_counters_are_atomic_per_day_and_survive_recycle(dynamo):
    a, b = _state(dynamo), _state(dynamo)
    a.stats_usage(100, 20, today="2026-09-22")
    b.stats_usage(50, 5, today="2026-09-22")
    b.stats_usage(1, 1, today="2026-09-23")
    u = _state(dynamo).stats_usage_get(today="2026-09-22")
    assert u == {"requests_today": 2, "tokens_today": 175, "prompt_tokens_today": 150,
                 "completion_tokens_today": 25, "date": "2026-09-22"}
    assert _state(dynamo).stats_usage_get(today="2026-09-24")["requests_today"] == 0


def test_provider_limits_round_trip(dynamo):
    s = _state(dynamo)
    assert s.stats_limits_get()["rpd"] is None
    s.stats_limits_set({"rpm": 30, "rpd": 14400, "rpm_remaining": 29})
    s.stats_limits_set({})  # no-op
    lim = _state(dynamo).stats_limits_get()
    assert (lim["rpm"], lim["rpd"], lim["rpm_remaining"], lim["tpd"]) == (30, 14400, 29, None)


# ── wiring ──────────────────────────────────────────────────────────────────

def test_from_env_local_is_none_and_dynamodb_needs_a_table(monkeypatch):
    monkeypatch.delenv("STATE_BACKEND", raising=False)
    assert ss.from_env() is None
    monkeypatch.setenv("STATE_BACKEND", "dynamodb")
    monkeypatch.delenv("STATE_TABLE", raising=False)
    with pytest.raises(ValueError):
        ss.from_env()
    monkeypatch.setenv("STATE_BACKEND", "redis")
    with pytest.raises(ValueError):
        ss.from_env()


def test_ensure_table_is_idempotent_and_enables_ttl(dynamo):
    ss.ensure_table(dynamo, TABLE)
    ttl = dynamo.describe_time_to_live(TableName=TABLE)["TimeToLiveDescription"]
    assert ttl["TimeToLiveStatus"] == "ENABLED" and ttl["AttributeName"] == "ttl"


# ── availability over bookkeeping ───────────────────────────────────────────

def test_cache_and_stats_degrade_when_dynamodb_is_unreachable(dynamo, caplog):
    s = _state(dynamo)
    s.table = "no-such-table"
    assert s.cache_get("v:q") is None
    s.cache_put("v:q", FRAMES)                 # no raise
    assert s.cache_delete("v:q") is False
    assert s.cache_clear() == 0
    assert s.cache_items() == {} and s.cache_len() == 0
    s.stats_error("sql_gen", "x")               # no raise
    s.stats_usage(1, 1)
    s.stats_limits_set({"rpm": 1})
    assert set(s.stats_error_summary()) == HEALTH_ERROR_KEYS
    assert s.stats_usage_get()["requests_today"] == 0
    assert s.stats_limits_get()["rpm"] is None
    assert "DynamoDB unavailable" in caplog.text
    # Degrading is recorded, not hidden: health reads this.
    st = s.backend_status()
    assert st["status"] == "degraded" and st["errors_total"] >= 10 and "ResourceNotFoundException" in st["last_error"]
    assert s.backend_status(now=time.time() + 3600)["status"] == "ok", "an old error ages out"
    assert _state(dynamo).backend_status() == {"backend": "dynamodb", "status": "ok", "errors_total": 0, "last_error": None}


def test_cache_hit_survives_eviction_between_read_and_touch(dynamo, monkeypatch):
    s = _state(dynamo)
    s.cache_put("v:q", FRAMES)
    real_update = s._c.update_item
    def delete_then_update(**kw):
        if "touched" in kw.get("UpdateExpression", ""):
            s._c.delete_item(TableName=TABLE, Key=kw["Key"])
        return real_update(**kw)
    monkeypatch.setattr(s._c, "update_item", delete_then_update)
    assert s.cache_get("v:q") == FRAMES, "a hit that lost the touch race is still a hit"
