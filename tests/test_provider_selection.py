"""Primary/fallback across TWO providers.

OpenRouter (free models) is the primary when OPENROUTER_API_KEY is set, with
Cerebras pay-as-you-go behind it. The two speak different base URLs and
different model ids, so anything that assumes one provider — a shared
LLM_BASE_URL, or handing the primary's model slug to the fallback — is a bug.
"""

import os
import sys

import openai
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analytics_agent as aa  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch):
    for var in ("OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "OPENROUTER_MODEL",
                "CEREBRAS_API_KEY", "CEREBRAS_PAID_API_KEY", "GEMINI_API_KEY",
                "MODEL", "LLM_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    yield


def test_openrouter_key_makes_it_the_primary(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("CEREBRAS_PAID_API_KEY", "cb-key")
    client = aa.make_client()
    assert client.api_key == "or-key"
    assert "openrouter.ai" in str(client.base_url)
    assert aa.get_primary_tier() == "openrouter"
    assert aa.get_primary_model() == aa.DEFAULT_OPENROUTER_MODEL


def test_cerebras_stays_the_fallback_with_its_own_url_and_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("CEREBRAS_PAID_API_KEY", "cb-key")
    paid = aa.make_paid_client()
    assert paid is not None, "the Cerebras key must still be reachable as a fallback"
    assert paid.api_key == "cb-key"
    assert "cerebras.ai" in str(paid.base_url)
    assert aa.get_fallback_model() == aa.DEFAULT_FALLBACK_MODEL
    assert aa.get_fallback_model() != aa.get_primary_model()


def test_without_openrouter_nothing_changes(monkeypatch):
    monkeypatch.setenv("CEREBRAS_PAID_API_KEY", "cb-key")
    monkeypatch.setenv("MODEL", "gpt-oss-120b")
    assert aa.make_client().api_key == "cb-key"
    assert aa.get_primary_tier() == "paid"
    assert aa.get_primary_model() == "gpt-oss-120b"
    assert aa.make_paid_client() is None  # would be falling back to itself


def test_fallback_gets_its_own_model_not_the_openrouter_slug():
    """The whole point: 'vendor/model:free' means nothing to Cerebras."""
    import httpx

    seen = []

    def mk(client, model):
        seen.append((client, model))
        def _call():
            if client == "primary":
                raise openai.APIStatusError(
                    "402 payment_required",
                    response=httpx.Response(402, request=httpx.Request("POST", "https://api.test/v1")),
                    body={"code": "payment_required"},
                )
            return f"ok:{model}"
        return _call

    out = aa._call_with_model_fallback(
        mk, "primary", "vendor/model:free",
        fallback_client="cerebras", fallback_model="gpt-oss-120b",
    )
    assert out == "ok:gpt-oss-120b"
    assert ("cerebras", "vendor/model:free") not in seen


def test_openrouter_client_sends_attribution_headers(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    headers = aa.make_client().default_headers
    assert headers.get("X-Title") == "Ask Lou"
    assert "louisville" in headers.get("HTTP-Referer", "")


def test_explicit_base_url_still_wins(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    assert "example.test" in str(aa.make_client(base_url="https://example.test/v1").base_url)


def test_empty_reply_raises_a_named_error_not_a_typeerror():
    """A model that returns content=None (all budget spent on reasoning) used to
    blow up with TypeError deep in strip_sql_fences."""
    class _Msg:
        content = None

    class _Choice:
        message = _Msg()
        finish_reason = "length"

    class _Resp:
        choices = [_Choice()]

    with pytest.raises(ValueError, match="empty message"):
        aa._message_text(_Resp())
