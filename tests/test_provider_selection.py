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
    """Env AND module state. A 402 driven through _call_with_retry sets the
    exhaustion latch for 15 minutes of wall clock, which outlives the test and
    silently sends any later test straight to its fallback."""
    for var in ("OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "OPENROUTER_MODEL",
                "CEREBRAS_API_KEY", "CEREBRAS_PAID_API_KEY", "GEMINI_API_KEY",
                "MODEL", "LLM_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(aa, "_active_model", None)
    aa._mark_free_tier_exhausted(False)
    yield
    aa._mark_free_tier_exhausted(False)


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

    with pytest.raises(aa.EmptyCompletionError, match="empty message"):
        aa._message_text(_Resp())


# ── Deprecation replacement on OpenRouter ────────────────────────────────────

class _FakeModels:
    def __init__(self, ids):
        self._ids = ids

    def list(self):
        return type("R", (), {"data": [type("M", (), {"id": i})() for i in self._ids]})()


class _FakeClient:
    def __init__(self, ids):
        self.models = _FakeModels(ids)


def test_replacement_is_never_an_arbitrary_paid_openrouter_model(monkeypatch):
    """OpenRouter lists hundreds of models, nearly all paid. The Cerebras
    preference ids can never match a 'vendor/model:free' slug, so the old
    'anything available' last resort picked whatever happened to be first."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    client = _FakeClient([
        "openai/gpt-5.6-sol-ultrafast",      # first in the catalogue, paid
        "anthropic/claude-opus-5",           # paid
        "poolside/laguna-s-2.1:free",        # a known-good free slug
    ])
    assert aa._resolve_fallback_model(client, "nvidia/nemotron-3-super-120b-a12b:free") \
        == "poolside/laguna-s-2.1:free"


def test_unknown_free_slug_beats_a_paid_model(monkeypatch):
    """No preferred slug present: still refuse to cross into paid territory."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    client = _FakeClient(["openai/gpt-5.6-sol-ultrafast", "some-vendor/new-model:free"])
    assert aa._resolve_fallback_model(client, "dead/model:free") == "some-vendor/new-model:free"


def test_no_free_model_available_means_no_replacement(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    client = _FakeClient(["openai/gpt-5.6-sol-ultrafast", "anthropic/claude-opus-5"])
    assert aa._resolve_fallback_model(client, "dead/model:free") is None


def test_cerebras_only_config_keeps_the_anything_available_last_resort(monkeypatch):
    """Unchanged where it was safe: one small vendor catalogue, no free/paid split."""
    monkeypatch.setenv("CEREBRAS_PAID_API_KEY", "cb-key")
    client = _FakeClient(["some-new-cerebras-model"])
    assert aa._resolve_fallback_model(client, "gone-model") == "some-new-cerebras-model"


def test_switch_is_not_recorded_when_the_other_provider_served_the_call(monkeypatch):
    """A replacement rescued by the cross-provider fallback is unproven: pinning
    it would put a failing round trip in front of every later question."""
    import httpx

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

    def mk(client, model):
        def _call():
            if client == "primary":
                if model == "dead/model:free":
                    raise openai.NotFoundError(
                        "model_not_found",
                        response=httpx.Response(404, request=httpx.Request("POST", "https://api.test/v1")),
                        body={"code": "model_not_found"},
                    )
                # The replacement exists but has no credit behind it.
                raise openai.APIStatusError(
                    "402 payment_required",
                    response=httpx.Response(402, request=httpx.Request("POST", "https://api.test/v1")),
                    body={"code": "payment_required"},
                )
            return "answer from cerebras"
        return _call

    monkeypatch.setattr(aa, "_resolve_fallback_model", lambda c, m: "other/model:free")
    out = aa._call_with_model_fallback(
        mk, "primary", "dead/model:free",
        fallback_client="cerebras", fallback_model="gpt-oss-120b",
    )
    assert out == "answer from cerebras"
    assert aa.get_active_model("dead/model:free") == "dead/model:free", "unproven model must not be pinned"
    assert aa.get_model_fallback_event() is None
