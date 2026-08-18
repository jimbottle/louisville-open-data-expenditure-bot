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
    aa._mark_free_tier_exhausted(False)
    yield
    aa._mark_free_tier_exhausted(False)


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

    aa._mark_free_tier_exhausted()
    real_time = aa.time.time
    monkeypatch.setattr(aa.time, "time", lambda: real_time() + aa.FREE_TIER_RECHECK_SECONDS + 1)
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

    aa._mark_free_tier_exhausted()
    assert aa._call_with_retry(free, fallback_fn=paid) == "free answer"
    assert calls == ["paid", "free"]
    assert not aa._free_tier_is_exhausted()


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
