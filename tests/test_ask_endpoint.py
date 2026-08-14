"""End-to-end tests for the /api/ask SSE flow (event_stream).

The product path — POST /api/ask -> generate SQL -> execute on DuckDB ->
interpret/refine -> stream SSE — had no behavioral test: the only prior
endpoint test hit /api/config, and the rag suite asserted on app.py's SOURCE
TEXT rather than running the handler. These drive the real router and the real
DuckDB with a FAKE LLM (no network), so event ordering, the dev/non-dev split,
the off-topic guard, the empty-result path, cache write/replay, and the
per-IP rate limit are all exercised as they are actually wired in event_stream.

Like the rest of the suite, this loads the local `data/` corpus at app startup
(see tests/test_known_answers.py). The CI/clean-clone fixture is tracked
separately in louisville-open-data-ll9.
"""
import json

import pytest
from fastapi.testclient import TestClient


# A real, executable query against the loaded expenditures table. The fake
# SQL-generator returns this so execute_sql_safe runs it for real.
REAL_SQL = (
    "SELECT agency_canonical, ROUND(SUM(extended_amount), 2) AS total_spend "
    "FROM expenditures WHERE fiscal_year = 2025 AND is_data_artifact = FALSE "
    "AND agency_canonical IS NOT NULL GROUP BY agency_canonical "
    "ORDER BY total_spend DESC LIMIT 3"
)
EMPTY_SQL = "SELECT agency_canonical FROM expenditures WHERE 1 = 0"


class _FakeResp:
    """Stand-in for the OpenAI raw response; event_stream only reads .headers."""
    headers = {}


def _fake_generate_sql(sql):
    def _gen(client, model, system, question, **kw):
        return sql, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, _FakeResp()
    return _gen


def _fake_interpret_stream(*chunks):
    def _stream(client, model, system, question, sql, results, **kw):
        for c in chunks:
            yield c
    return _stream


def _fake_refine_stream(*chunks):
    def _stream(client, model, question, sql, results, draft, **kw):
        for c in chunks:
            yield c
    return _stream


@pytest.fixture(scope="module")
def client():
    """One TestClient for the module: startup loads `data/` once (~seconds).

    The context-manager form runs the startup event so con/sql_system/etc. are
    populated exactly as in production.
    """
    import app
    with TestClient(app.app) as c:
        yield c


@pytest.fixture(autouse=True)
def _fast_and_isolated(monkeypatch):
    """No real sleeps; fresh rate-limit + cache state per test."""
    import app
    monkeypatch.setattr(app.time, "sleep", lambda *a, **k: None)
    app.ip_requests.clear()
    app.response_cache.clear()
    yield
    app.ip_requests.clear()
    app.response_cache.clear()


def _events(resp):
    """Parse an SSE body into a list of decoded event dicts."""
    out = []
    for line in resp.text.splitlines():
        line = line.strip()
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out


def _types(events):
    return [e["type"] for e in events]


def _post(client, question, **body):
    return client.post("/api/ask", json={"question": question, **body})


# ── Happy path ───────────────────────────────────────────────────────────────

def test_happy_path_streams_sql_results_interpretation_then_done(client, monkeypatch):
    import app
    monkeypatch.setattr(app, "generate_sql", _fake_generate_sql(REAL_SQL))
    monkeypatch.setattr(app, "interpret_results_stream", _fake_interpret_stream("Top agencies by spend."))
    monkeypatch.setattr(app, "refine_interpretation_stream", _fake_refine_stream("Public Works led FY2025 spending."))

    resp = _post(client, "which agencies spent the most in 2025?", dev_mode=True)
    assert resp.status_code == 200
    events = _events(resp)
    types = _types(events)

    # The SQL we injected actually ran and its frame was emitted.
    sql_evt = next(e for e in events if e["type"] == "sql")
    assert "expenditures" in sql_evt["content"]
    # Results came back and carried a row count.
    results_evt = next(e for e in events if e["type"] == "results")
    assert results_evt["row_count"] >= 1
    # An interpretation was streamed, and the stream terminated cleanly.
    assert "interpretation" in types
    assert types[-1] == "done"
    assert "error" not in types
    # Ordering invariant: sql before results before the first interpretation.
    assert types.index("sql") < types.index("results") < types.index("interpretation")


def test_dev_mode_flag_controls_humanization(client, monkeypatch):
    import app
    monkeypatch.setattr(app, "generate_sql", _fake_generate_sql(REAL_SQL))
    monkeypatch.setattr(app, "interpret_results_stream", _fake_interpret_stream("x"))
    monkeypatch.setattr(app, "refine_interpretation_stream", _fake_refine_stream("x"))

    dev = next(e for e in _events(_post(client, "q dev", dev_mode=True)) if e["type"] == "results")
    assert dev["humanized"] is False
    app.response_cache.clear()
    prod = next(e for e in _events(_post(client, "q prod", dev_mode=False)) if e["type"] == "results")
    assert prod["humanized"] is True


# ── Guard paths ──────────────────────────────────────────────────────────────

def test_off_topic_response_short_circuits_without_results(client, monkeypatch):
    import app
    # A comment-only / prose answer trips the off-topic guard in event_stream.
    monkeypatch.setattr(app, "generate_sql", _fake_generate_sql("-- The question is not about spending"))
    resp = _post(client, "what's the weather?", dev_mode=True)
    types = _types(_events(resp))
    assert "results" not in types
    assert "interpretation" in types
    assert types[-1] == "done"


def test_empty_result_takes_the_no_rows_path(client, monkeypatch):
    import app
    monkeypatch.setattr(app, "generate_sql", _fake_generate_sql(EMPTY_SQL))
    monkeypatch.setattr(app, "interpret_results_stream", _fake_interpret_stream("No matching rows; try broadening."))
    resp = _post(client, "spend on nonexistent thing", dev_mode=True)
    events = _events(resp)
    results_evt = next(e for e in events if e["type"] == "results")
    assert results_evt["row_count"] == 0
    assert "interpretation" in _types(events)
    assert _types(events)[-1] == "done"


def test_blank_question_returns_error_event(client):
    resp = _post(client, "   ")
    events = _events(resp)
    assert events[0]["type"] == "error"
    assert "done" in _types(events)


def test_malformed_json_body_returns_error_sse(client):
    resp = client.post("/api/ask", content=b"not json",
                       headers={"content-type": "application/json"})
    events = _events(resp)
    assert events[0]["type"] == "error"


# ── Rate limiting ────────────────────────────────────────────────────────────

def test_per_ip_rate_limit_blocks_after_the_cap(client, monkeypatch):
    import app
    monkeypatch.setattr(app, "generate_sql", _fake_generate_sql(REAL_SQL))
    monkeypatch.setattr(app, "interpret_results_stream", _fake_interpret_stream("x"))
    monkeypatch.setattr(app, "refine_interpretation_stream", _fake_refine_stream("x"))

    limit = app.IP_RPM_LIMIT
    for i in range(limit):
        assert "error" not in _types(_events(_post(client, f"allowed {i}", dev_mode=True)))
    blocked = _events(_post(client, "one too many", dev_mode=True))
    assert blocked[0]["type"] == "error"
    assert "lot of questions" in blocked[0]["content"].lower()


def test_distinct_cf_connecting_ips_get_separate_buckets(client, monkeypatch):
    """The per-IP limit must key on the real client (CF-Connecting-IP behind the
    tunnel), not the shared peer address — otherwise it acts site-wide."""
    import app
    monkeypatch.setattr(app, "generate_sql", _fake_generate_sql(REAL_SQL))
    monkeypatch.setattr(app, "interpret_results_stream", _fake_interpret_stream("x"))
    monkeypatch.setattr(app, "refine_interpretation_stream", _fake_refine_stream("x"))

    limit = app.IP_RPM_LIMIT
    # Exhaust the peer bucket (no forwarded header) entirely.
    for i in range(limit):
        _post(client, f"a{i}", dev_mode=True)
    # A forwarded client IP must still be served, not collateral-damaged.
    b_ok = client.post("/api/ask", json={"question": "b first", "dev_mode": True},
                       headers={"CF-Connecting-IP": "203.0.113.2"})
    assert "error" not in _types(_events(b_ok))
    # And user A (by header) is independently limited from user B.
    for i in range(limit):
        client.post("/api/ask", json={"question": f"aa{i}", "dev_mode": True},
                    headers={"CF-Connecting-IP": "203.0.113.1"})
    a_hdr_blocked = client.post("/api/ask", json={"question": "a hdr over", "dev_mode": True},
                                headers={"CF-Connecting-IP": "203.0.113.1"})
    assert _events(a_hdr_blocked)[0]["type"] == "error"


# ── Cache write + replay ─────────────────────────────────────────────────────

def test_answer_is_cached_and_replayed_without_a_second_llm_call(client, monkeypatch):
    import app
    calls = {"n": 0}

    def counting_gen(c, m, s, q, **kw):
        calls["n"] += 1
        return REAL_SQL, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, _FakeResp()

    monkeypatch.setattr(app, "generate_sql", counting_gen)
    monkeypatch.setattr(app, "interpret_results_stream", _fake_interpret_stream("cached answer body"))
    monkeypatch.setattr(app, "refine_interpretation_stream", _fake_refine_stream("cached answer body"))

    q = "a uniquely worded cacheable question about spending"
    first = _events(_post(client, q, dev_mode=False))
    assert "interpretation" in _types(first)
    assert calls["n"] == 1
    # Second identical ask: served from cache, no new SQL generation.
    second = _events(_post(client, q, dev_mode=False))
    assert calls["n"] == 1, "second ask should replay the cache, not call the LLM"
    assert _types(second)[-1] == "done"


def test_dev_mode_answers_are_not_cached(client, monkeypatch):
    import app
    calls = {"n": 0}

    def counting_gen(c, m, s, q, **kw):
        calls["n"] += 1
        return REAL_SQL, {"total_tokens": 2}, _FakeResp()

    monkeypatch.setattr(app, "generate_sql", counting_gen)
    monkeypatch.setattr(app, "interpret_results_stream", _fake_interpret_stream("body"))
    monkeypatch.setattr(app, "refine_interpretation_stream", _fake_refine_stream("body"))

    q = "another unique question that must not persist in dev mode"
    _events(_post(client, q, dev_mode=True))
    _events(_post(client, q, dev_mode=True))
    assert calls["n"] == 2, "dev_mode responses must never be cached"


# ── Admin gate on the cache endpoints (5i8 / 1ls) ────────────────────────────

def test_cache_endpoints_are_disabled_without_a_configured_token(client, monkeypatch):
    import app
    monkeypatch.setattr(app, "ADMIN_TOKEN", "")
    assert client.get("/api/cache").status_code == 503
    assert client.delete("/api/cache").status_code == 503


def test_cache_endpoints_reject_a_missing_or_wrong_token(client, monkeypatch):
    import app
    monkeypatch.setattr(app, "ADMIN_TOKEN", "s3cret")
    assert client.get("/api/cache").status_code == 401
    assert client.get("/api/cache", headers={"X-Admin-Token": "nope"}).status_code == 401
    assert client.delete("/api/cache", headers={"X-Admin-Token": "nope"}).status_code == 401


def test_cache_endpoints_accept_the_correct_token(client, monkeypatch):
    import app
    monkeypatch.setattr(app, "ADMIN_TOKEN", "s3cret")
    ok = client.get("/api/cache", headers={"X-Admin-Token": "s3cret"})
    assert ok.status_code == 200
    assert "cache_version" in ok.json()
    wiped = client.delete("/api/cache", headers={"X-Admin-Token": "s3cret"})
    assert wiped.status_code == 200
