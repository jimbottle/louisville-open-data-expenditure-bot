"""Out-of-credit (HTTP 402) handling.

Cerebras retired its always-free tier in July 2026: the free key now answers
every call with `payment_required`, which the openai SDK raises as a bare
APIStatusError. Two things must follow — the funded paid key gets used, and if
nothing is funded the user is told it's a billing problem, not a bad question.
"""

import os
import sys

import httpx
import openai
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analytics_agent as aa  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_free_tier_latch():
    """The exhausted-free-tier latch is module state; don't leak it between tests."""
    aa._mark_primary_unusable(False)
    yield
    aa._mark_primary_unusable(False)


def _payment_required():
    """The exact shape Cerebras returns: 402, no dedicated openai subclass."""
    req = httpx.Request("POST", "https://api.test/v1/chat/completions")
    resp = httpx.Response(402, request=req)
    return openai.APIStatusError(
        "Error code: 402 - {'message': 'Payment required to access this resource. "
        "Visit your billing tab.', 'type': 'payment_required_error', 'param': 'quota', "
        "'code': 'payment_required'}",
        response=resp,
        body={"code": "payment_required"},
    )


def _insufficient_quota_429():
    """Billing exhaustion as OpenAI-compatible providers other than Cerebras send it:
    a 429, i.e. the same exception class as an ordinary rate limit."""
    req = httpx.Request("POST", "https://api.test/v1/chat/completions")
    resp = httpx.Response(429, request=req)
    return openai.RateLimitError(
        "Error code: 429 - {'code': 'insufficient_quota', 'message': 'You exceeded your current quota'}",
        response=resp,
        body={"code": "insufficient_quota"},
    )


def _rate_limited():
    req = httpx.Request("POST", "https://api.test/v1/chat/completions")
    resp = httpx.Response(429, request=req)
    return openai.RateLimitError("429 too many requests", response=resp, body=None)


def test_classifies_402_as_quota():
    assert aa.is_quota_error(_payment_required())


def test_classifies_insufficient_quota_429_as_quota():
    """OpenAI reports billing exhaustion as a 429 — still not a wait-a-minute error."""
    req = httpx.Request("POST", "https://api.test/v1/chat/completions")
    resp = httpx.Response(429, request=req)
    err = openai.RateLimitError("insufficient_quota: exceeded current quota", response=resp, body=None)
    assert aa.is_quota_error(err)


def test_plain_rate_limit_is_not_quota():
    assert not aa.is_quota_error(_rate_limited())


def test_sql_error_is_not_quota():
    assert not aa.is_quota_error(Exception('Table "expenditures" does not exist'))


def test_quota_error_falls_back_to_paid_tier():
    calls = []

    def free():
        calls.append("free")
        raise _payment_required()

    def paid():
        calls.append("paid")
        return "answer"

    assert aa._call_with_retry(free, fallback_fn=paid) == "answer"
    assert calls == ["free", "paid"]
    assert aa.get_last_tier_used() == "paid"


def test_quota_error_without_paid_key_raises_immediately():
    """No paid client configured: fail fast, don't burn retries on a 402."""
    calls = []

    def free():
        calls.append("free")
        raise _payment_required()

    with pytest.raises(openai.APIStatusError):
        aa._call_with_retry(free)
    assert calls == ["free"]


def test_quota_error_on_both_tiers_propagates_quota_error():
    """The app must still see a quota error so it shows the funding message."""
    def free():
        raise _payment_required()

    def paid():
        raise _payment_required()

    with pytest.raises(openai.APIStatusError) as exc:
        aa._call_with_retry(free, fallback_fn=paid)
    assert aa.is_quota_error(exc.value)


def test_free_tier_is_skipped_after_it_runs_out_of_credit():
    """A 402 doesn't clear on its own, so later calls shouldn't re-discover it —
    that's a wasted round trip on every one of the three LLM calls per question."""
    calls = []

    def free():
        calls.append("free")
        raise _payment_required()

    def paid():
        calls.append("paid")
        return "answer"

    aa._call_with_retry(free, fallback_fn=paid)
    calls.clear()
    assert aa._call_with_retry(free, fallback_fn=paid) == "answer"
    assert calls == ["paid"]


def test_latch_expires_so_topped_up_credit_is_picked_back_up(monkeypatch):
    def free():
        return "free answer"

    def paid():
        return "paid answer"

    aa._mark_primary_unusable()
    real_time = aa.time.time
    monkeypatch.setattr(aa.time, "time", lambda: real_time() + aa.PRIMARY_RECHECK_SECONDS + 1)
    assert aa._call_with_retry(free, fallback_fn=paid) == "free answer"


def test_latch_clears_when_the_paid_tier_also_fails():
    """Don't strand calls on the paid key if it breaks while the latch is set."""
    calls = []

    def free():
        calls.append("free")
        return "free answer"

    def paid():
        calls.append("paid")
        raise RuntimeError("paid key revoked")

    aa._mark_primary_unusable()
    assert aa._call_with_retry(free, fallback_fn=paid) == "free answer"
    assert calls == ["paid", "free"]
    assert not aa._primary_is_unusable()


# ── Key selection (single-key operation) ─────────────────────────────────────

def test_paid_key_is_used_when_no_free_key_is_configured(monkeypatch):
    """The free tier is gone; a deployment carrying only the paid key must work."""
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    monkeypatch.setenv("CEREBRAS_PAID_API_KEY", "paid-key")
    assert aa.make_client().api_key == "paid-key"
    assert aa.get_primary_tier() == "paid"


def test_free_key_still_wins_when_both_are_set(monkeypatch):
    monkeypatch.setenv("CEREBRAS_API_KEY", "free-key")
    monkeypatch.setenv("CEREBRAS_PAID_API_KEY", "paid-key")
    assert aa.make_client().api_key == "free-key"
    assert aa.get_primary_tier() == "free"
    assert aa.make_paid_client().api_key == "paid-key"


def test_no_self_fallback_when_the_paid_key_is_the_only_key(monkeypatch):
    """Falling back from a key to itself just doubles every failure."""
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    monkeypatch.setenv("CEREBRAS_PAID_API_KEY", "paid-key")
    assert aa.make_paid_client() is None


def test_empty_free_key_is_treated_as_unset(monkeypatch):
    """`CEREBRAS_API_KEY=` in .env means 'no free key', not 'authenticate as nobody'."""
    monkeypatch.setenv("CEREBRAS_API_KEY", "")
    monkeypatch.setenv("CEREBRAS_PAID_API_KEY", "paid-key")
    assert aa.make_client().api_key == "paid-key"
    assert aa.get_primary_tier() == "paid"


def test_insufficient_quota_429_skips_the_rate_limit_ladder(monkeypatch):
    """A quota error dressed as a 429 must NOT go through wait-and-retry: the key
    cannot recover by waiting, so the ladder would stall every call for ~48s and
    never reach the latch."""
    slept = []
    monkeypatch.setattr(aa.time, "sleep", lambda s: slept.append(s))
    calls = []

    def primary():
        calls.append("primary")
        raise _insufficient_quota_429()

    def paid():
        calls.append("paid")
        return "answer"

    assert aa._call_with_retry(primary, fallback_fn=paid) == "answer"
    assert calls == ["primary", "paid"]
    assert slept == []
    assert aa._primary_is_unusable()


def test_insufficient_quota_429_without_a_fallback_fails_fast(monkeypatch):
    slept = []
    monkeypatch.setattr(aa.time, "sleep", lambda s: slept.append(s))
    attempts = []

    def primary():
        attempts.append(1)
        raise _insufficient_quota_429()

    with pytest.raises(openai.RateLimitError) as exc:
        aa._call_with_retry(primary)
    assert attempts == [1]
    assert slept == []
    assert aa.is_quota_error(exc.value)


def test_ordinary_rate_limit_still_retries_and_falls_back(monkeypatch):
    """The 429 ladder must survive the quota short-circuit: a plain rate limit
    still waits, retries the primary key, then uses the paid key."""
    slept = []
    monkeypatch.setattr(aa.time, "sleep", lambda s: slept.append(s))
    calls = []

    def primary():
        calls.append("primary")
        raise _rate_limited()

    def paid():
        calls.append("paid")
        return "answer"

    assert aa._call_with_retry(primary, fallback_fn=paid) == "answer"
    assert calls == ["primary", "primary", "paid"]
    assert slept == [aa.RETRY_BASE_DELAY]
    assert not aa._primary_is_unusable()


# ── OpenRouter free daily allowance ──────────────────────────────────────────

def _daily_cap_429():
    """OpenRouter's free-tier daily allowance (50/day under $10 of credits)."""
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    resp = httpx.Response(429, request=req)
    return openai.RateLimitError(
        "Error code: 429 - {'error': {'message': 'Rate limit exceeded: free-models-per-day. "
        "Add 10 credits to unlock 1000 free model requests per day', 'code': 429}}",
        response=resp,
        body=None,
    )


def test_daily_cap_is_distinguished_from_an_ordinary_rate_limit():
    assert aa.is_daily_cap_error(_daily_cap_429())
    assert not aa.is_daily_cap_error(_rate_limited())
    assert not aa.is_daily_cap_error(_payment_required())


def test_daily_cap_goes_straight_to_the_fallback_provider(monkeypatch):
    """It resets at midnight UTC, so the 16s retry ladder can only waste the
    user's time before failing the same way."""
    slept = []
    monkeypatch.setattr(aa.time, "sleep", lambda s: slept.append(s))
    calls = []

    def primary():
        calls.append("primary")
        raise _daily_cap_429()

    def cerebras():
        calls.append("cerebras")
        return "answer"

    assert aa._call_with_retry(primary, fallback_fn=cerebras) == "answer"
    assert calls == ["primary", "cerebras"]
    assert slept == []
    assert aa._primary_is_unusable()


# ── Mixed failure sequences (the ladder's second attempt) ────────────────────

def test_rate_limit_then_quota_falls_over_and_latches(monkeypatch):
    """429 first, out of credit second. The old ladder special-cased attempt 1,
    so this sequence reached the unclassified path: no fallback on a 402, no
    latch on a quota 429, and the ~16s wait repeated on every later call."""
    slept = []
    monkeypatch.setattr(aa.time, "sleep", lambda s: slept.append(s))
    errors = [_rate_limited(), _payment_required()]
    calls = []

    def primary():
        calls.append("primary")
        raise errors.pop(0)

    def fallback():
        calls.append("fallback")
        return "answer"

    assert aa._call_with_retry(primary, fallback_fn=fallback) == "answer"
    assert calls == ["primary", "primary", "fallback"]
    assert slept == [aa.RETRY_BASE_DELAY]   # one wait, for the ordinary 429 only
    assert aa._primary_is_unusable()


def test_rate_limit_then_daily_cap_falls_over_and_latches(monkeypatch):
    """Same shape via OpenRouter, which enforces a 20/min AND a 50/day cap on
    one key, so the per-minute limit landing first is routine."""
    monkeypatch.setattr(aa.time, "sleep", lambda s: None)
    errors = [_rate_limited(), _daily_cap_429()]

    def primary():
        raise errors.pop(0)

    assert aa._call_with_retry(primary, fallback_fn=lambda: "answer") == "answer"
    assert aa._primary_is_unusable()


def test_plain_rate_limit_fallback_does_not_latch(monkeypatch):
    """A per-minute limit clears in a minute; latching it would hand 15 minutes
    of traffic to the billed provider over one spike."""
    monkeypatch.setattr(aa.time, "sleep", lambda s: None)

    def primary():
        raise _rate_limited()

    assert aa._call_with_retry(primary, fallback_fn=lambda: "answer") == "answer"
    assert not aa._primary_is_unusable()


def test_failed_fallback_surfaces_the_primary_error_not_the_fallback_one():
    """app.py picks the user's message from this exception. The fallback's own
    error (a Cerebras 402) would say "the bill is unpaid" for an allowance that
    resets at midnight."""
    def primary():
        raise _daily_cap_429()

    def fallback():
        raise _payment_required()

    with pytest.raises(Exception) as exc:
        aa._call_with_retry(primary, fallback_fn=fallback)
    assert aa.is_daily_cap_error(exc.value), "daily-cap classification must survive"
    assert isinstance(exc.value.__cause__, openai.APIStatusError)


def test_empty_completion_fails_over_to_the_other_provider():
    """An empty reply will not fill itself in on a retry — try the other side."""
    calls = []

    def primary():
        calls.append("primary")
        raise aa.EmptyCompletionError("LLM returned an empty message (finish_reason=length)")

    def fallback():
        calls.append("fallback")
        return "answer"

    assert aa._call_with_retry(primary, fallback_fn=fallback) == "answer"
    assert calls == ["primary", "fallback"]
    assert not aa._primary_is_unusable()  # nothing was exhausted


def test_empty_completion_without_a_fallback_raises_its_own_type():
    def primary():
        raise aa.EmptyCompletionError("LLM returned an empty message (finish_reason=length)")

    with pytest.raises(aa.EmptyCompletionError):
        aa._call_with_retry(primary)


# ── Provider-side failures cross to the fallback (seen live 2026-08-31) ──────
# OpenRouter answered HTTP 200 and then delivered "Upstream error from Nvidia:
# Service temporarily overloaded" as an in-stream error payload, which the SDK
# raises as a bare APIError. The generic branch re-raised it without ever
# trying the funded Cerebras fallback.

def _upstream_overloaded():
    req = httpx.Request("POST", "https://api.test/v1/chat/completions")
    resp = httpx.Response(502, request=req)
    return openai.APIError("Upstream error from Nvidia: Service temporarily overloaded",
                           request=req, body=None)


def test_generic_provider_error_fails_over_to_the_fallback():
    calls = {"primary": 0, "fallback": 0}

    def primary():
        calls["primary"] += 1
        raise _upstream_overloaded()

    def fallback():
        calls["fallback"] += 1
        return "answer"

    assert aa._call_with_retry(primary, fallback_fn=fallback) == "answer"
    assert calls == {"primary": 1, "fallback": 1}   # immediate, no retry ladder
    assert not aa._primary_is_unusable()            # transient: never latched


def test_bad_primary_key_fails_over_to_the_fallback():
    req = httpx.Request("POST", "https://api.test/v1/chat/completions")
    resp = httpx.Response(401, request=req)
    err = openai.AuthenticationError("invalid api key", response=resp, body=None)

    def primary():
        raise err

    assert aa._call_with_retry(primary, fallback_fn=lambda: "saved") == "saved"


def test_provider_error_with_no_fallback_still_raises():
    def primary():
        raise _upstream_overloaded()

    with pytest.raises(openai.APIError):
        aa._call_with_retry(primary)


def test_provider_error_on_both_raises_the_primary_error():
    def primary():
        raise _upstream_overloaded()

    def fallback():
        raise RuntimeError("fallback also down")

    with pytest.raises(openai.APIError) as exc:
        aa._call_with_retry(primary, fallback_fn=fallback)
    assert "overloaded" in str(exc.value)


def test_model_not_found_is_not_treated_as_a_provider_error():
    # NotFoundError must reach _call_with_model_fallback's own resolution path.
    req = httpx.Request("POST", "https://api.test/v1/chat/completions")
    resp = httpx.Response(404, request=req)
    err = openai.NotFoundError("model_not_found", response=resp, body={"code": "model_not_found"})
    assert not aa.is_provider_error(err)

    def primary():
        raise err

    with pytest.raises(openai.NotFoundError):
        aa._call_with_retry(primary, fallback_fn=lambda: "no")


# ── Stall watchdog: an open-but-silent stream is abandoned, not waited out ───

def test_stall_guard_passes_a_healthy_stream_through():
    class _Stream:
        def __iter__(self): return iter([1, 2, 3])
        def close(self): pass
    assert list(aa._iter_with_stall_guard(_Stream(), stall_seconds=1)) == [1, 2, 3]


def test_stall_guard_abandons_a_stream_that_goes_quiet():
    import time as _time
    closed = {"n": 0}

    class _Stream:
        def __iter__(self):
            yield "first"
            _time.sleep(5)      # the dribble: alive, producing nothing
            yield "never seen"
        def close(self): closed["n"] += 1

    got = []
    with pytest.raises(aa.StreamStalledError):
        for x in aa._iter_with_stall_guard(_Stream(), stall_seconds=0.2):
            got.append(x)
    assert got == ["first"] and closed["n"] == 1


def test_stall_guard_propagates_stream_errors_in_order():
    class _Stream:
        def __iter__(self):
            yield "a"
            raise _upstream_overloaded()
        def close(self): pass
    got = []
    with pytest.raises(openai.APIError):
        for x in aa._iter_with_stall_guard(_Stream(), stall_seconds=1):
            got.append(x)
    assert got == ["a"]


def test_stall_guard_disabled_when_zero():
    class _Stream:
        def __iter__(self): return iter("ab")
    assert list(aa._iter_with_stall_guard(_Stream(), stall_seconds=0)) == ["a", "b"]


def test_upstream_404_that_is_not_model_not_found_fails_over():
    """OpenRouter surfaces an upstream breakage as HTTP 404 'Provider returned
    error' (code 404, not 'model_not_found'). The SDK raises NotFoundError, so
    the blanket exclusion left it with neither the model-resolution path nor
    the provider fallback — the call just died."""
    req = httpx.Request("POST", "https://api.test/v1/chat/completions")
    resp = httpx.Response(404, request=req)
    err = openai.NotFoundError(
        "Error code: 404 - {'error': {'message': 'Provider returned error', 'code': 404, "
        "'metadata': {'provider_name': 'Nvidia'}}}",
        response=resp, body={"error": {"message": "Provider returned error", "code": 404}})
    assert aa.is_provider_error(err)
    assert aa._call_with_retry(lambda: (_ for _ in ()).throw(err), fallback_fn=lambda: "saved") == "saved"
