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
    aa._mark_primary_unusable(False)
    yield
    aa._mark_primary_unusable(False)


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


def test_no_replacement_model_uses_the_other_provider(monkeypatch):
    """OpenRouter retires the pinned slug and offers no other free one. That used
    to kill every question with a service error while a funded Cerebras key sat
    idle — a model_not_found never reaches the fallback through _call_with_retry."""
    import httpx

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(aa, "_resolve_fallback_model", lambda c, m: None)

    def mk(client, model):
        def _call():
            if client == "primary":
                raise openai.NotFoundError(
                    "model_not_found",
                    response=httpx.Response(404, request=httpx.Request("POST", "https://api.test/v1")),
                    body={"code": "model_not_found"},
                )
            return f"answer from {model}"
        return _call

    out = aa._call_with_model_fallback(
        mk, "primary", "dead/model:free",
        fallback_client="cerebras", fallback_model="gpt-oss-120b",
    )
    assert out == "answer from gpt-oss-120b"
    assert aa.get_active_model("dead/model:free") == "dead/model:free"


def test_no_replacement_and_no_fallback_still_raises(monkeypatch):
    import httpx

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(aa, "_resolve_fallback_model", lambda c, m: None)

    def mk(client, model):
        def _call():
            raise openai.NotFoundError(
                "model_not_found",
                response=httpx.Response(404, request=httpx.Request("POST", "https://api.test/v1")),
                body={"code": "model_not_found"},
            )
        return _call

    with pytest.raises(openai.NotFoundError):
        aa._call_with_model_fallback(mk, "primary", "dead/model:free")


def test_a_failing_fallback_keeps_the_model_not_found_classification(monkeypatch):
    """app.py reads the surfaced exception; the fallback's own error would
    misroute the message."""
    import httpx

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(aa, "_resolve_fallback_model", lambda c, m: None)

    def mk(client, model):
        def _call():
            if client == "primary":
                raise openai.NotFoundError(
                    "model_not_found",
                    response=httpx.Response(404, request=httpx.Request("POST", "https://api.test/v1")),
                    body={"code": "model_not_found"},
                )
            raise RuntimeError("cerebras unreachable")
        return _call

    with pytest.raises(openai.NotFoundError) as exc:
        aa._call_with_model_fallback(mk, "primary", "dead/model:free",
                                     fallback_client="cerebras", fallback_model="gpt-oss-120b")
    assert isinstance(exc.value.__cause__, RuntimeError)


def test_provenance_is_per_call_not_a_shared_global():
    """SSE requests run on threadpool threads: one call's provenance must not be
    answerable by another's."""
    a, b = {}, {}
    aa._call_with_retry(lambda: "primary served", provenance=a, fallback_fn=lambda: "fallback served")

    def broken():
        raise aa.EmptyCompletionError("empty")

    aa._call_with_retry(broken, provenance=b, fallback_fn=lambda: "fallback served")
    assert a == {"used_fallback": False}
    assert b == {"used_fallback": True}


def _not_found():
    import httpx
    return openai.NotFoundError(
        "model_not_found",
        response=httpx.Response(404, request=httpx.Request("POST", "https://api.test/v1")),
        body={"code": "model_not_found"},
    )


def test_no_replacement_recovery_gets_the_retry_ladder(monkeypatch):
    """This branch is the only thing serving questions while the slug is gone, so
    a single transient 429 on the fallback must not kill the question."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(aa, "_resolve_fallback_model", lambda c, m: None)
    monkeypatch.setattr(aa.time, "sleep", lambda s: None)
    progress = []
    fallback_attempts = []

    def mk(client, model):
        def _call():
            if client == "primary":
                raise _not_found()
            fallback_attempts.append(model)
            if len(fallback_attempts) == 1:
                import httpx
                raise openai.RateLimitError(
                    "429 slow down",
                    response=httpx.Response(429, request=httpx.Request("POST", "https://api.test/v1")),
                    body=None,
                )
            return f"answer from {model}"
        return _call

    out = aa._call_with_model_fallback(
        mk, "primary", "dead/model:free",
        on_retry=lambda *a: progress.append(a),
        fallback_client="cerebras", fallback_model="gpt-oss-120b",
    )
    assert out == "answer from gpt-oss-120b"
    assert len(fallback_attempts) == 2, "the fallback should have been retried"
    assert progress, "the SSE stream should have been told a retry was happening"


def test_no_replacement_latches_so_the_catalogue_is_not_re_listed(monkeypatch):
    """Every call lands here while the slug is gone; re-listing a several-hundred
    entry catalogue 2-3 times per question is the cost the latch exists to avoid."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    listings = []

    def fake_resolve(client, model):
        listings.append(model)
        return None

    monkeypatch.setattr(aa, "_resolve_fallback_model", fake_resolve)

    def mk(client, model):
        def _call():
            if client == "primary":
                raise _not_found()
            return "answer from cerebras"
        return _call

    for _ in range(3):
        assert aa._call_with_model_fallback(
            mk, "primary", "dead/model:free",
            fallback_client="cerebras", fallback_model="gpt-oss-120b",
        ) == "answer from cerebras"
    assert len(listings) == 1, f"catalogue listed {len(listings)} times; should be latched after the first"
    assert aa._primary_is_unusable()


def test_latch_expires_so_a_restored_slug_is_picked_back_up(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    aa._mark_primary_unusable()
    real_time = aa.time.time
    monkeypatch.setattr(aa.time, "time", lambda: real_time() + aa.PRIMARY_RECHECK_SECONDS + 1)

    def mk(client, model):
        return lambda: f"answer from {client}"

    assert aa._call_with_model_fallback(
        mk, "primary", "back/from/the/dead:free",
        fallback_client="cerebras", fallback_model="gpt-oss-120b",
    ) == "answer from primary"


def test_out_of_credit_fallback_surfaces_as_a_quota_error(monkeypatch):
    """app.py classifies the exception it is handed and never inspects __cause__,
    so this must not arrive as a model_not_found ("try again in a little while")."""
    import httpx

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(aa, "_resolve_fallback_model", lambda c, m: None)

    def mk(client, model):
        def _call():
            if client == "primary":
                raise _not_found()
            raise openai.APIStatusError(
                "402 payment_required",
                response=httpx.Response(402, request=httpx.Request("POST", "https://api.test/v1")),
                body={"code": "payment_required"},
            )
        return _call

    with pytest.raises(Exception) as exc:
        aa._call_with_model_fallback(mk, "primary", "dead/model:free",
                                    fallback_client="cerebras", fallback_model="gpt-oss-120b")
    assert aa.is_quota_error(exc.value)


def test_a_failed_catalogue_listing_does_not_latch_the_primary_out(monkeypatch):
    """One timeout on GET /models must not spend 15 minutes of billed traffic —
    the free replacement is probably still sitting in the catalogue."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    attempts = []

    def flaky_resolve(client, model):
        attempts.append(model)
        raise aa.ModelCatalogueUnavailable("connection reset")

    monkeypatch.setattr(aa, "_resolve_fallback_model", flaky_resolve)

    def mk(client, model):
        def _call():
            if client == "primary":
                raise _not_found()
            return "answer from cerebras"
        return _call

    for _ in range(2):
        assert aa._call_with_model_fallback(
            mk, "primary", "dead/model:free",
            fallback_client="cerebras", fallback_model="gpt-oss-120b",
        ) == "answer from cerebras"

    assert not aa._primary_is_unusable(), "a listing failure is not evidence the primary is dead"
    assert len(attempts) == 2, "resolution must be re-attempted on the next question"


def test_a_listing_failure_still_answers_the_question(monkeypatch):
    """Falling back is right either way; only the latch differs."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(aa, "_resolve_fallback_model",
                        lambda c, m: (_ for _ in ()).throw(aa.ModelCatalogueUnavailable("500")))

    def mk(client, model):
        def _call():
            if client == "primary":
                raise _not_found()
            return f"answer from {model}"
        return _call

    assert aa._call_with_model_fallback(
        mk, "primary", "dead/model:free",
        fallback_client="cerebras", fallback_model="gpt-oss-120b",
    ) == "answer from gpt-oss-120b"


def test_resolver_raises_rather_than_returning_none_when_it_cannot_ask():
    """The two outcomes must stay distinguishable at the source."""
    class _Broken:
        class models:
            @staticmethod
            def list():
                raise TimeoutError("upstream 504")

    with pytest.raises(aa.ModelCatalogueUnavailable):
        aa._resolve_fallback_model(_Broken(), "dead/model:free")
