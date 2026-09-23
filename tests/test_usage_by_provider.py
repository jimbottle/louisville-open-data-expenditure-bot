"""The usage line must not mix OpenRouter's free allowance with Cerebras's paid
account (2026-09-23: it showed Cerebras's 1,440,000/day as the limit and the
refine call's "paid" as the tier of the whole answer).
"""
import os

import boto3
import pytest
from moto import mock_aws

import analytics_agent as aa
import state_store as ss


def test_provider_and_label_mapping():
    assert aa.provider_of("openrouter") == "openrouter"
    assert aa.provider_of("paid") == "cerebras" and aa.provider_of("free") == "cerebras"
    assert aa.tier_label("openrouter") == "free (OpenRouter)"
    assert aa.tier_label("paid") == "paid (Cerebras)"


@pytest.fixture
def dynamo():
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        c = boto3.client("dynamodb", region_name="us-east-1")
        ss.ensure_table(c, "t")
        yield ss.DynamoState("t", client=c)


def test_dynamodb_counts_each_provider_separately(dynamo):
    dynamo.stats_usage(100, 10, today="2026-09-23", provider="openrouter")
    dynamo.stats_usage(0, 50, today="2026-09-23", provider="openrouter")
    dynamo.stats_usage(0, 30, today="2026-09-23", provider="cerebras")
    dynamo.stats_usage(5, 5, today="2026-09-23")  # unattributed still counts in the totals
    u = dynamo.stats_usage_get(today="2026-09-23")
    assert u["requests_today"] == 4 and u["tokens_today"] == 200
    assert u["by_provider"] == {"openrouter": {"requests": 2, "tokens": 160},
                                "cerebras": {"requests": 1, "tokens": 30}}


class _Resp:
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): pass
    def json(self): return self._p


@pytest.mark.parametrize("free,rpd", [(True, 50), (False, 1000)])
def test_openrouter_limits_follow_is_free_tier(monkeypatch, free, rpd):
    import httpx
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(aa, "_openrouter_limits_cache", {"at": 0.0, "value": None})
    seen = {}
    def fake_get(url, headers, timeout):
        seen["url"] = url
        return _Resp({"data": {"is_free_tier": free, "usage": 1.5}})
    monkeypatch.setattr(httpx, "get", fake_get)
    lim = aa.get_openrouter_limits()
    assert lim == {"rpd": rpd, "rpm": 20, "is_free_tier": free, "credits_used": 1.5}
    assert seen["url"].endswith("/key")
    # cached: a second call does not hit the network
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("not cached"))
    assert aa.get_openrouter_limits() == lim


def test_openrouter_limits_unknown_on_failure_and_without_key(monkeypatch):
    import httpx
    monkeypatch.setattr(aa, "_openrouter_limits_cache", {"at": 0.0, "value": None})
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    def boom(*a, **k): raise httpx.ConnectError("down")
    monkeypatch.setattr(httpx, "get", boom)
    assert aa.get_openrouter_limits() is None
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setattr(aa, "_openrouter_limits_cache", {"at": 0.0, "value": None})
    assert aa.get_openrouter_limits() is None


def test_answer_attributes_each_call_and_labels_tiers(require_data, monkeypatch, tmp_path):
    """End to end: SQL + draft on OpenRouter, refine on Cerebras. The usage
    event carries both budgets separately and the debug line names all three."""
    import json as _json
    from fastapi.testclient import TestClient
    import app
    from test_ask_endpoint import (REAL_SQL, _events, _fake_generate_sql,
                                   _fake_interpret_stream, _fake_refine_stream)
    monkeypatch.setattr(app, "CACHE_FILE", str(tmp_path / "c.json"))
    monkeypatch.setattr(app, "STATS_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(app, "STATE", None)
    monkeypatch.setattr(app, "persistent_stats", _json.loads(_json.dumps(app._default_stats)))
    monkeypatch.setattr(app, "get_openrouter_limits", lambda: {"rpd": 1000, "rpm": 20, "is_free_tier": False, "credits_used": 0})
    app.ip_requests.clear(); app.response_cache.clear()
    tiers = iter(["openrouter", "openrouter", "paid"])  # sql, draft, refine
    current = {"t": "openrouter"}
    monkeypatch.setattr(app, "get_last_tier_used", lambda: current["t"])
    gen = _fake_generate_sql(REAL_SQL)
    def gen_and_mark(*a, **k):
        current["t"] = next(tiers); return gen(*a, **k)
    interp = _fake_interpret_stream("Draft body.")
    def interp_and_mark(*a, **k):
        current["t"] = next(tiers); return interp(*a, **k)
    refine = _fake_refine_stream("Refined body.")
    def refine_and_mark(*a, **k):
        current["t"] = next(tiers); return refine(*a, **k)
    monkeypatch.setattr(app, "generate_sql", gen_and_mark)
    monkeypatch.setattr(app, "interpret_results_stream", interp_and_mark)
    monkeypatch.setattr(app, "refine_interpretation_stream", refine_and_mark)
    with TestClient(app.app) as c:
        ev = _events(c.post("/api/ask", json={"question": "which agency spent most", "dev_mode": True}))
    usage = next(e for e in ev if e["type"] == "usage")
    p = usage["providers"]
    assert p["openrouter"]["requests_today"] == 2 and p["cerebras"]["requests_today"] == 1
    assert p["openrouter"]["rpd"] == 1000 and p["openrouter"]["rpm"] == 20
    final = [e["content"] for e in ev if e["type"] == "debug" and "Interpretation streamed" in e["content"]][0]
    assert "SQL free (OpenRouter) · draft free (OpenRouter) · refine paid (Cerebras)" in final
    draft_line = [e["content"] for e in ev if e["type"] == "debug" and "Draft interpretation" in e["content"]][0]
    assert draft_line.endswith("Tier: free (OpenRouter)")


def test_tier_record_is_per_request_under_interleaving():
    """Two requests interleaved on threadpool threads, each step running in a
    COPY of its request's context (how Starlette iterates a sync SSE generator).
    Each must read back its own served tier, not the other's — the review 4771
    hazard with the old process-wide global."""
    import contextvars
    import threading

    def request_context():
        ctx = contextvars.copy_context()
        ctx.run(aa.begin_request_tier_tracking)
        return ctx

    a, b = request_context(), request_context()
    seen = {}
    # A's call is served by OpenRouter; then B's by Cerebras (overwriting the
    # global); then A reads its tier in a fresh copy on another thread.
    t = threading.Thread(target=lambda: a.copy().run(aa._set_tier, "openrouter")); t.start(); t.join()
    t = threading.Thread(target=lambda: b.copy().run(aa._set_tier, "paid")); t.start(); t.join()
    t = threading.Thread(target=lambda: seen.update(a=a.copy().run(aa.get_last_tier_used))); t.start(); t.join()
    t = threading.Thread(target=lambda: seen.update(b=b.copy().run(aa.get_last_tier_used))); t.start(); t.join()
    assert seen == {"a": "openrouter", "b": "paid"}
    assert aa._last_tier_used == "paid", "the global still reflects the latest call (display only)"


def test_recorder_survives_the_real_streaming_path(require_data, monkeypatch, tmp_path):
    """No patch on get_last_tier_used: the fakes write the tier the way the
    real call path does (_set_tier), and the endpoint's recorder must carry it
    across Starlette's per-step context copies into the usage event."""
    import json as _json
    from fastapi.testclient import TestClient
    import app
    from test_ask_endpoint import (REAL_SQL, _events, _fake_generate_sql,
                                   _fake_interpret_stream, _fake_refine_stream)
    monkeypatch.setattr(app, "CACHE_FILE", str(tmp_path / "c.json"))
    monkeypatch.setattr(app, "STATS_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(app, "STATE", None)
    monkeypatch.setattr(app, "persistent_stats", _json.loads(_json.dumps(app._default_stats)))
    monkeypatch.setattr(app, "get_openrouter_limits", lambda: None)
    app.ip_requests.clear(); app.response_cache.clear()

    def marking(fake, tier):
        def f(*a, **k):
            aa._set_tier(tier); return fake(*a, **k)
        return f
    monkeypatch.setattr(app, "generate_sql", marking(_fake_generate_sql(REAL_SQL), "openrouter"))
    monkeypatch.setattr(app, "interpret_results_stream", marking(_fake_interpret_stream("Draft."), "openrouter"))
    monkeypatch.setattr(app, "refine_interpretation_stream", marking(_fake_refine_stream("Refined."), "paid"))
    aa._set_tier("free")  # a stale global from "another request"
    with TestClient(app.app) as c:
        ev = _events(c.post("/api/ask", json={"question": "recorder path", "dev_mode": True}))
    p = next(e for e in ev if e["type"] == "usage")["providers"]
    assert p["openrouter"]["requests_today"] == 2 and p["cerebras"]["requests_today"] == 1
    assert p["openrouter"]["rpd"] is None  # key endpoint unavailable -> unknown, not a wrong number
