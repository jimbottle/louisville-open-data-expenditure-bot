"""The API served with STATE_BACKEND=dynamodb (moto), end to end.

test_state_store.py pins the backend's contract in isolation; this file pins
that app.py actually ROUTES through it — a cache hit is replayed from the
table, the rate limit is the table's not the process's, /api/health keeps its
shape, and the CloudFront client-IP mode cannot be spoofed from the first
X-Forwarded-For hop.
"""
import json
import os

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

import state_store as ss
from test_ask_endpoint import (REAL_SQL, _events, _fake_generate_sql,
                               _fake_interpret_stream, _fake_refine_stream, _types)

TABLE = "lou-state-app-test"


@pytest.fixture
def dyn_client(require_data, monkeypatch):
    """The API with app.STATE swapped for a DynamoState on a moto table."""
    import app
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ss.ensure_table(ddb, TABLE)
        state = ss.DynamoState(TABLE, client=ddb, rpm_limit=app.IP_RPM_LIMIT)
        monkeypatch.setattr(app, "STATE", state)
        monkeypatch.setattr(app, "generate_sql", _fake_generate_sql(REAL_SQL))
        monkeypatch.setattr(app, "interpret_results_stream", _fake_interpret_stream("body"))
        monkeypatch.setattr(app, "refine_interpretation_stream", _fake_refine_stream("body"))
        with TestClient(app.app) as c:
            yield c, state, ddb


def _post(c, q, **headers):
    return c.post("/api/ask", json={"question": q}, headers=headers)


def test_answer_is_cached_in_dynamodb_and_replayed_from_a_fresh_instance(dyn_client):
    import app
    c, state, ddb = dyn_client
    calls = []
    orig = app.generate_sql
    app.generate_sql = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]
    first = _events(_post(c, "cached via dynamo"))
    assert "interpretation" in _types(first) and "error" not in _types(first)
    assert len(calls) == 1
    # The item is in the TABLE under the version-prefixed key ...
    key = app._cache_key("cached via dynamo")
    item = ddb.get_item(TableName=TABLE, Key={"pk": {"S": f"cache#{key}"}}).get("Item")
    assert item and any('"type": "interpretation"' in f for f in json.loads(item["events"]["S"]))
    # ... and a "new container" (a second DynamoState on the same table) serves it
    # with no LLM call.
    app.STATE = ss.DynamoState(TABLE, client=ddb, rpm_limit=app.IP_RPM_LIMIT)
    second = _events(_post(c, "cached via dynamo"))
    assert _types(second) == _types(first)
    assert len(calls) == 1, "replayed from DynamoDB, no second generation"
    assert key not in app.response_cache, "the local dict must not be used on this backend"


def test_rate_limit_is_shared_across_instances(dyn_client):
    import app
    c, state, ddb = dyn_client
    limit = app.IP_RPM_LIMIT
    # Half the budget through "container A" ...
    for i in range(limit // 2):
        assert "error" not in _types(_events(_post(c, f"a{i}")))
    # ... the rest through "container B" — same table, same client IP.
    app.STATE = ss.DynamoState(TABLE, client=ddb, rpm_limit=limit)
    for i in range(limit - limit // 2):
        assert "error" not in _types(_events(_post(c, f"b{i}")))
    blocked = _events(_post(c, "over"))
    assert blocked[0]["type"] == "error" and "lot of questions" in blocked[0]["content"].lower()
    assert app.ip_requests == {}, "the local dict must not be used on this backend"


def test_health_shape_is_identical_on_the_dynamodb_backend(dyn_client, monkeypatch):
    """uptime.yml reads .status, .model_fallback, .errors.errors_last_hour;
    the heartbeat parses the same object. Compare the full key sets with the
    local backend's, then prove a tracked error flows through the table."""
    import app
    c, state, ddb = dyn_client
    dyn = c.get("/api/health").json()
    monkeypatch.setattr(app, "STATE", None)
    local = c.get("/api/health").json()
    assert set(dyn) == set(local)
    assert set(dyn["errors"]) == set(local["errors"])
    assert dyn["tables"] == local["tables"]
    assert dyn["status"] == "ok" and dyn["errors"]["errors_last_hour"] == 0

    monkeypatch.setattr(app, "STATE", state)
    app.track_error("quota", "402 payment_required")
    h = c.get("/api/health").json()
    assert h["status"] == "degraded" and "402" in h["degraded_reason"]
    assert h["errors"]["errors_last_hour"] == 1 and h["errors"]["quota_errors"] == 1


def test_health_table_counts_are_served_from_the_startup_snapshot(dyn_client):
    import app
    c, *_ = dyn_client
    first = c.get("/api/health").json()["tables"]
    assert first["expenditures"] > 1_000_000
    assert app.TABLE_COUNTS is first or app.TABLE_COUNTS == first


def test_cloudfront_ip_mode_keys_on_the_hop_cloudfront_appended(dyn_client, monkeypatch):
    """Behind OAC only CloudFront reaches the origin, and it APPENDS the viewer
    address to X-Forwarded-For. A client rotating its own (first-hop) value
    must still share one bucket; distinct viewers get distinct buckets."""
    import app
    c, state, ddb = dyn_client
    monkeypatch.setattr(app, "CLIENT_IP_SOURCE", "cloudfront")
    limit = app.IP_RPM_LIMIT
    for i in range(limit):
        r = _post(c, f"s{i}", **{"X-Forwarded-For": f"198.51.100.{i}, 203.0.113.1"})
        assert "error" not in _types(_events(r))
    spoofed = _post(c, "over", **{"X-Forwarded-For": "198.51.100.250, 203.0.113.1"})
    assert _events(spoofed)[0]["type"] == "error", "first-hop rotation bypassed the limit"
    other = _post(c, "other viewer", **{"X-Forwarded-For": "1.1.1.1, 203.0.113.2"})
    assert "error" not in _types(_events(other))
    # The bucket key is the CloudFront-appended address, port-free.
    assert ddb.get_item(TableName=TABLE, Key={"pk": {"S": _rl_pk("203.0.113.1", ddb)}}).get("Item")


def _rl_pk(ip, ddb):
    items = ddb.scan(TableName=TABLE, ProjectionExpression="pk")["Items"]
    for it in items:
        if it["pk"]["S"].startswith(f"rl#{ip}#"):
            return it["pk"]["S"]
    raise AssertionError(f"no rate-limit bucket for {ip}: {[i['pk']['S'] for i in items]}")


@pytest.mark.parametrize("header,expected", [
    ("198.51.100.7:4433", "198.51.100.7"),
    ("[2001:db8::1]:443", "2001:db8::1"),
    ("2001:db8::1", "2001:db8::1"),
])
def test_cloudfront_viewer_address_header_wins_and_drops_the_port(dyn_client, monkeypatch, header, expected):
    import app
    c, state, ddb = dyn_client
    monkeypatch.setattr(app, "CLIENT_IP_SOURCE", "cloudfront")
    _post(c, "viewer header", **{"CloudFront-Viewer-Address": header, "X-Forwarded-For": "9.9.9.9, 8.8.8.8"})
    assert _rl_pk(expected, ddb)


def test_cache_admin_endpoints_use_the_table(dyn_client, monkeypatch):
    import app
    c, state, ddb = dyn_client
    monkeypatch.setattr(app, "ADMIN_TOKEN", "s3cret")
    auth = {"X-Admin-Token": "s3cret"}
    _post(c, "admin listed")
    listing = c.get("/api/cache", headers=auth).json()
    assert listing["cached_questions"] == 1
    assert list(listing["entries"]) == [app._cache_key("admin listed")]
    assert c.request("DELETE", "/api/cache", json={"question": "admin listed"}, headers=auth).json() == {"cleared": "admin listed"}
    assert c.request("DELETE", "/api/cache", json={"question": "admin listed"}, headers=auth).json() == {"error": "Not in cache"}
    _post(c, "one more")
    assert c.request("DELETE", "/api/cache", headers=auth).json() == {"cleared": "all"}
    assert state.cache_len() == 0
