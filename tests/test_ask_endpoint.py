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
def client(require_data):
    """One TestClient for the module: startup loads `data/` once (~seconds).

    The context-manager form runs the startup event so con/sql_system/etc. are
    populated exactly as in production. Skips when the golden data is absent
    (clean clone / CI) — see tests/conftest.py.
    """
    import app
    with TestClient(app.app) as c:
        yield c


@pytest.fixture(autouse=True)
def _fast_and_isolated(monkeypatch, tmp_path):
    """No real sleeps; fresh rate-limit + cache state per test; and — critically
    — redirect the cache/stats persistence to a throwaway dir so `pytest` never
    rewrites the developer's (or a configured STATS_DIR's) real on-disk
    .response_cache.json / .stats.json with test fixtures. CACHE_FILE and
    STATS_FILE are read at call time in _save_cache/_save_stats, so patching the
    module globals is enough."""
    import app
    monkeypatch.setattr(app.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(app, "CACHE_FILE", str(tmp_path / "cache.json"))
    monkeypatch.setattr(app, "STATS_FILE", str(tmp_path / "stats.json"))
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


def test_distinct_cf_connecting_ips_get_separate_buckets_when_peer_is_trusted(client, monkeypatch):
    """Behind a TRUSTED proxy, the limit keys on the real client (CF-Connecting-IP),
    so distinct clients get independent buckets instead of one site-wide one."""
    import app
    monkeypatch.setattr(app, "generate_sql", _fake_generate_sql(REAL_SQL))
    monkeypatch.setattr(app, "interpret_results_stream", _fake_interpret_stream("x"))
    monkeypatch.setattr(app, "refine_interpretation_stream", _fake_refine_stream("x"))
    # TestClient's peer address is "testclient"; trust it so forwarded headers
    # are honored (as the tunnel peer would be in production).
    monkeypatch.setattr(app, "TRUSTED_PROXY_IPS", {"testclient"})

    limit = app.IP_RPM_LIMIT
    # Exhaust client A entirely (by forwarded IP).
    for i in range(limit):
        client.post("/api/ask", json={"question": f"aa{i}", "dev_mode": True},
                    headers={"CF-Connecting-IP": "203.0.113.1"})
    a_blocked = client.post("/api/ask", json={"question": "a over", "dev_mode": True},
                            headers={"CF-Connecting-IP": "203.0.113.1"})
    assert _events(a_blocked)[0]["type"] == "error"
    # A different forwarded client IP is unaffected.
    b_ok = client.post("/api/ask", json={"question": "b first", "dev_mode": True},
                       headers={"CF-Connecting-IP": "203.0.113.2"})
    assert "error" not in _types(_events(b_ok))


def test_forwarded_ip_headers_are_ignored_from_an_untrusted_peer(client, monkeypatch):
    """A direct-to-origin client (peer not trusted) must NOT be able to spoof
    CF-Connecting-IP to dodge the limit — all its requests key on the real peer."""
    import app
    monkeypatch.setattr(app, "generate_sql", _fake_generate_sql(REAL_SQL))
    monkeypatch.setattr(app, "interpret_results_stream", _fake_interpret_stream("x"))
    monkeypatch.setattr(app, "refine_interpretation_stream", _fake_refine_stream("x"))
    monkeypatch.setattr(app, "TRUSTED_PROXY_IPS", set())  # nothing trusted

    limit = app.IP_RPM_LIMIT
    # Rotate a fresh fake IP every request; without trust they all share the
    # peer bucket, so the cap still bites.
    for i in range(limit):
        client.post("/api/ask", json={"question": f"s{i}", "dev_mode": True},
                    headers={"CF-Connecting-IP": f"198.51.100.{i}"})
    spoofed = client.post("/api/ask", json={"question": "spoof over", "dev_mode": True},
                          headers={"CF-Connecting-IP": "198.51.100.254"})
    assert _events(spoofed)[0]["type"] == "error", "header rotation bypassed the limit"


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


# ── Response cache is bounded (louisville-open-data-vbv) ──────────────────────

def test_response_cache_evicts_oldest_past_the_cap(client, monkeypatch):
    import app
    monkeypatch.setattr(app, "generate_sql", _fake_generate_sql(REAL_SQL))
    monkeypatch.setattr(app, "interpret_results_stream", _fake_interpret_stream("body"))
    monkeypatch.setattr(app, "refine_interpretation_stream", _fake_refine_stream("body"))
    monkeypatch.setattr(app, "MAX_CACHE_ENTRIES", 3)
    # Rate limit would block after IP_RPM_LIMIT, so drive _cache_put directly
    # with the same event shape the ask path produces.
    frames = ['data: {"type": "interpretation", "content": "x"}\n\n']
    for i in range(5):
        app._cache_put(f"{app.CACHE_VERSION}:q{i}", list(frames))
    assert len(app.response_cache) == 3, "cache must not grow past MAX_CACHE_ENTRIES"
    # FIFO: the three most-recently inserted survive.
    assert set(app.response_cache) == {f"{app.CACHE_VERSION}:q{i}" for i in (2, 3, 4)}


# ── security headers (louisville-open-data-e8d / 3691) ───────────────────────

def test_anti_framing_headers_are_sent(client):
    """frame-ancestors is ignored in a <meta> CSP, so the server must send the
    anti-clickjacking headers itself."""
    r = client.get("/")
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in r.headers.get("Content-Security-Policy", "")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    # And the SSE stream still works with the middleware in place.
    assert client.post("/api/ask", json={"question": ""}).status_code == 200


# ── Out-of-credit (HTTP 402) ─────────────────────────────────────────────────

def _payment_required():
    """The exact error Cerebras raises once an account's credit runs out."""
    import httpx
    import openai
    resp = httpx.Response(402, request=httpx.Request("POST", "https://api.test/v1/chat/completions"))
    return openai.APIStatusError(
        "Error code: 402 - {'message': 'Payment required to access this resource. "
        "Visit your billing tab.', 'code': 'payment_required'}",
        response=resp,
        body={"code": "payment_required"},
    )


def test_quota_exhaustion_says_it_is_a_funding_problem(client, monkeypatch):
    """A 402 must NOT be reported as 'reword your question' — the user cannot
    fix it by rewording, and the project is happy to say the bill is unpaid."""
    import app

    def _broke(*a, **kw):
        raise _payment_required()

    monkeypatch.setattr(app, "generate_sql", _broke)
    r = _post(client, "How much grant funding has Louisville received?")
    events = _events(r)
    errors = [e["content"] for e in events if e["type"] == "error"]
    assert errors == [app.QUOTA_MSG]
    assert "out of credit" in errors[0]
    assert "reword" not in errors[0].lower()
    assert errors[0] != app.RATE_LIMIT_MSG


def test_quota_exhaustion_is_tracked_separately_from_rate_limits(client, monkeypatch):
    import app

    def _broke(*a, **kw):
        raise _payment_required()

    monkeypatch.setattr(app, "generate_sql", _broke)
    before = app.persistent_stats["errors"].get("quota_errors", 0)
    _post(client, "another question about spending")
    assert app.persistent_stats["errors"].get("quota_errors", 0) == before + 1


def test_daily_cap_says_it_resets_rather_than_asking_for_money(client, monkeypatch):
    """Free allowance spent is not the same as out of credit: it clears by itself."""
    import app

    def _capped(*a, **kw):
        import httpx
        import openai
        resp = httpx.Response(429, request=httpx.Request("POST", "https://openrouter.ai/api/v1"))
        raise openai.RateLimitError(
            "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model requests per day",
            response=resp, body=None,
        )

    monkeypatch.setattr(app, "generate_sql", _capped)
    errors = [e["content"] for e in _events(_post(client, "spending by agency")) if e["type"] == "error"]
    assert errors == [app.DAILY_CAP_MSG]
    assert errors[0] != app.QUOTA_MSG
    assert errors[0] != app.RATE_LIMIT_MSG


# ── Vocabulary grounding + verify-and-repair ─────────────────────────────────
# The production failure these guard: a SUM over a filter that matched no rows
# came back as one NaN row, sailed past the `len == 0` check, and was
# interpreted as "no recorded vehicle spending" (real figure ~$1.6M).

NARROW_SQL = (
    "SELECT ROUND(SUM(extended_amount), 2) AS total_spend FROM expenditures "
    "WHERE agency_canonical = 'Louisville Fire' AND fiscal_year = 2024 "
    "AND spend_category ILIKE '%Vehicle%' AND is_data_artifact = FALSE"
)
REPAIRED_SQL = (
    "SELECT ROUND(SUM(extended_amount), 2) AS total_spend FROM expenditures "
    "WHERE agency_canonical = 'Louisville Fire' AND fiscal_year = 2024 "
    "AND (spend_category ILIKE '%Automotive%' OR spend_category ILIKE '%Vehicle%') "
    "AND is_data_artifact = FALSE"
)
GENUINE_EMPTY_SQL = (
    "SELECT ROUND(SUM(extended_amount), 2) AS total_spend FROM expenditures "
    "WHERE agency_canonical = 'Louisville Fire' AND fiscal_year = 2031"
)


def _fake_generate_sql_sequence(*sqls):
    """Returns each SQL in turn and records every call's question/context."""
    calls = []

    def _gen(client, model, system, question, **kw):
        calls.append({"question": question, "context": kw.get("context")})
        sql = sqls[min(len(calls) - 1, len(sqls) - 1)]
        return sql, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, _FakeResp()
    _gen.calls = calls
    return _gen


def test_first_sql_request_carries_the_vocabulary_block(client, monkeypatch):
    import app
    gen = _fake_generate_sql_sequence(REAL_SQL)
    monkeypatch.setattr(app, "generate_sql", gen)
    monkeypatch.setattr(app, "interpret_results_stream", _fake_interpret_stream("draft"))
    monkeypatch.setattr(app, "refine_interpretation_stream", _fake_refine_stream("final"))
    _post(client, "How much did Louisville Fire spend on vehicles in fiscal year 2024?", dev_mode=True)
    ctx = gen.calls[0]["context"]
    assert ctx and ctx.startswith("## Data vocabulary matched to this question")
    assert "Automotive" in ctx and "spend_category" in ctx


def test_null_aggregate_is_repaired_with_the_real_vocabulary(client, monkeypatch):
    import app
    gen = _fake_generate_sql_sequence(NARROW_SQL, REPAIRED_SQL)
    monkeypatch.setattr(app, "generate_sql", gen)
    monkeypatch.setattr(app, "interpret_results_stream", _fake_interpret_stream("draft"))
    monkeypatch.setattr(app, "refine_interpretation_stream", _fake_refine_stream("Louisville Fire spent $1.6M."))
    events = _events(_post(client, "How much did Louisville Fire spend on vehicles in FY2024?", dev_mode=True))
    types = _types(events)

    # Two SQL generations: the original, then a repair whose prompt names the
    # narrow filter and the real category family.
    assert len(gen.calls) == 2
    repair_prompt = gen.calls[1]["question"]
    assert "returned nothing" in repair_prompt and "%Vehicle%" in repair_prompt
    assert "Automotive" in repair_prompt
    # The repaired query is what was executed and shown.
    assert types.count("sql") == 2
    results = next(e for e in events if e["type"] == "results")
    assert results["row_count"] == 1 and "NaN" not in results["content"]
    # The answer streams (not the empty-result path) and the reader is told.
    assert "interpretation" in types and "error" not in types
    info = [e for e in events if e["type"] == "info"]
    assert info and "re-checked the data's own category names" in info[0]["content"]
    # The self-correction is part of the answer's provenance, after the answer.
    assert types.index("info") > types.index("interpretation")
    assert types[-1] == "done"


def test_genuine_empty_result_is_not_retried(client, monkeypatch):
    import app
    gen = _fake_generate_sql_sequence(GENUINE_EMPTY_SQL, REPAIRED_SQL)
    monkeypatch.setattr(app, "generate_sql", gen)
    monkeypatch.setattr(app, "interpret_results_stream", _fake_interpret_stream("no data for 2031"))
    events = _events(_post(client, "What did Louisville Fire spend in 2031?", dev_mode=True))
    assert len(gen.calls) == 1  # every filter matched a real value: nothing to repair
    assert _types(events).count("sql") == 1
    assert "info" not in _types(events)
    # A NULL aggregate takes the empty-result path, not the "here is NaN" one.
    assert "no data for 2031" in "".join(e.get("content", "") for e in events if e["type"] == "interpretation")


def test_failed_repair_falls_through_to_the_empty_path(client, monkeypatch):
    import app
    gen = _fake_generate_sql_sequence(NARROW_SQL, GENUINE_EMPTY_SQL)  # repair also empty
    monkeypatch.setattr(app, "generate_sql", gen)
    monkeypatch.setattr(app, "interpret_results_stream", _fake_interpret_stream("nothing matched"))
    events = _events(_post(client, "How much did Louisville Fire spend on vehicles in FY2024?", dev_mode=True))
    types = _types(events)
    assert len(gen.calls) == 2
    assert types.count("sql") == 1  # the failed repair is not shown as the query
    assert "info" not in types and "error" not in types
    logs = " ".join(e["content"] for e in events if e["type"] == "log")
    assert "Repaired query also returned nothing" in logs
    assert types[-1] == "done"


def test_repair_exception_never_becomes_a_user_error(client, monkeypatch):
    import app
    n = {"calls": 0}

    def _gen(client_, model, system, question, **kw):
        n["calls"] += 1
        if n["calls"] == 1:
            return NARROW_SQL, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, _FakeResp()
        raise RuntimeError("provider hiccup")
    monkeypatch.setattr(app, "generate_sql", _gen)
    monkeypatch.setattr(app, "interpret_results_stream", _fake_interpret_stream("nothing matched"))
    events = _events(_post(client, "How much did Louisville Fire spend on vehicles in FY2024?", dev_mode=True))
    types = _types(events)
    assert "error" not in types and "interpretation" in types and types[-1] == "done"
    assert any("Repair attempt failed" in e["content"] for e in events if e["type"] == "log")


def test_empty_explanation_prompt_carries_the_findings_without_the_rewrite_order(client, monkeypatch):
    import app
    seen = {}

    def _stream(client_, model, system, question, sql, results, **kw):
        seen["question"] = question
        yield "explained"
    monkeypatch.setattr(app, "generate_sql", _fake_generate_sql_sequence(NARROW_SQL, GENUINE_EMPTY_SQL))
    monkeypatch.setattr(app, "interpret_results_stream", _stream)
    _post(client, "How much did Louisville Fire spend on vehicles in FY2024?", dev_mode=True)
    assert "checked against the data's vocabulary" in seen["question"]
    assert "Automotive" in seen["question"]
    assert "Rewrite the query" not in seen["question"]
    assert "no SQL" in seen["question"]


# ── Off-topic guard ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("reply", [
    "I’m sorry, but I can only help with data-related questions.",   # 'WITH' in prose
    "-- The question is not about spending",
    "",
    "/* nothing to query */",
])
def test_prose_or_comment_replies_take_the_off_topic_path(client, monkeypatch, reply):
    """The guard used to look for SQL keywords ANYWHERE in the reply, so a
    refusal containing the word 'with' was executed as SQL, failed, and came
    back from the error-retry as SELECT '<the refusal>' AS message."""
    import app
    monkeypatch.setattr(app, "generate_sql", _fake_generate_sql(reply))
    events = _events(_post(client, "Tell me a joke about cats.", dev_mode=True))
    types = _types(events)
    assert "sql" not in types and "results" not in types and "error" not in types
    assert any("doesn't appear to be answerable" in e.get("content", "")
               for e in events if e["type"] == "interpretation")


@pytest.mark.parametrize("reply", [
    "SELECT 1", "  -- assumed FY2025\nSELECT 1", "WITH t AS (SELECT 1) SELECT * FROM t",
    "(SELECT 1) UNION ALL (SELECT 2)", "/* c */ select 1",
])
def test_looks_like_sql_accepts_real_statements(reply):
    import app
    assert app._looks_like_sql(reply)


# ── Retrieved-document topic gate ────────────────────────────────────────────

def test_documents_sharing_no_content_word_with_the_question_are_dropped(client):
    import app
    hits = [
        {"file_no": "O-274-22", "text": "AN ORDINANCE AMENDING THE FISCAL YEAR 2022-2023 CAPITAL BUDGET BY CHANGING THE DUE DATE FOR A PAVING PLAN"},
        {"file_no": "O-088-21", "text": "AN ORDINANCE APPROPRIATING $1,300,000 FOR FIRE FLEET REPLACEMENT VEHICLES"},
    ]
    kept = app._on_topic_hits("How much did Louisville Fire spend on vehicles in FY2024?", hits)
    assert [h["file_no"] for h in kept] == ["O-088-21"]
    # Synonyms count: "vehicles" reaches a document that only says "fleet".
    kept = app._on_topic_hits("vehicles", [{"file_no": "x", "text": "FIRE FLEET REPLACEMENT"}])
    assert kept
    # A question with no content words keeps everything (nothing to judge by).
    assert app._on_topic_hits("Which agencies spend the most?", hits) == hits


# ── Funding failures page the operator (louisville-open-data-8uk) ────────────

def test_a_quota_error_makes_health_degraded_with_a_reason(client, monkeypatch):
    """Out of credit used to leave /api/health "ok" (cached starters still
    stream) while every live question failed — invisible to the dead-man's
    switch. Now any funding failure in the last hour is a named degradation,
    which the heartbeat escalates."""
    import app
    monkeypatch.setitem(app.persistent_stats["errors"], "last_quota_error_time", None)
    monkeypatch.setitem(app.persistent_stats["errors"], "errors_last_hour", [])
    assert client.get("/api/health").json()["status"] == "ok"
    app.track_error("quota", "Error code: 402 - payment_required")
    h = client.get("/api/health").json()
    assert h["status"] == "degraded"
    assert "funding" in h["degraded_reason"] and "402" in h["degraded_reason"]
    assert h["errors"]["quota_errors"] >= 1
    # An hour later it clears by itself (the operator has been paged by then).
    app.persistent_stats["errors"]["last_quota_error_time"] -= app.QUOTA_DEGRADE_SECONDS + 1
    assert client.get("/api/health").json()["status"] == "ok"


# ── A draft stream that dies mid-flight retries whole on the fallback ────────

def test_mid_stream_draft_failure_retries_on_the_fallback_provider(client, monkeypatch):
    """OpenRouter 200s, streams half an answer, then errors in-stream. The
    draft is server-side only, so the whole draft is retried on the fallback
    and the reader sees one clean answer — not an apology."""
    import app
    monkeypatch.setattr(app, "generate_sql", _fake_generate_sql(REAL_SQL))
    monkeypatch.setattr(app, "paid_client", object())
    calls = {"n": 0}

    def _stream(client_, model, system, question, sql, results, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            yield "half an ans"
            raise RuntimeError("Upstream error from Nvidia: Service temporarily overloaded")
        yield "the whole answer, from the fallback"
    monkeypatch.setattr(app, "interpret_results_stream", _stream)
    monkeypatch.setattr(app, "refine_interpretation_stream", _fake_refine_stream("refined answer"))
    events = _events(_post(client, "How has annual spending changed?", dev_mode=True))
    types = _types(events)
    assert calls["n"] == 2
    assert "error" not in types
    text = "".join(e.get("content", "") for e in events if e["type"] == "interpretation")
    assert "refined answer" in text and "trouble summarizing" not in text
    assert types[-1] == "done"


def test_draft_failure_with_no_fallback_still_degrades_honestly(client, monkeypatch):
    import app
    monkeypatch.setattr(app, "generate_sql", _fake_generate_sql(REAL_SQL))
    monkeypatch.setattr(app, "paid_client", None)

    def _stream(*a, **kw):
        raise RuntimeError("boom")
        yield
    monkeypatch.setattr(app, "interpret_results_stream", _stream)
    events = _events(_post(client, "How has annual spending changed?", dev_mode=True))
    text = "".join(e.get("content", "") for e in events if e["type"] == "interpretation")
    assert "trouble summarizing" in text
    assert _types(events)[-1] == "done"
