"""Model-fallback tests: a provider deprecating the configured model
(model_not_found 404) must trigger a one-time runtime switch to an available
model, remembered for subsequent calls — the qwen-3-235b incident, prevented."""

import os
import sys

import httpx
import openai
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analytics_agent as aa  # noqa: E402


def _not_found(message="Model gone-model does not exist or you do not have access to it. model_not_found"):
    resp = httpx.Response(404, request=httpx.Request("POST", "https://api.test/v1/chat/completions"))
    return openai.NotFoundError(message, response=resp, body={"code": "model_not_found"})


class FakeModel:
    def __init__(self, id):
        self.id = id


class FakeModels:
    def __init__(self, ids):
        self._ids = ids

    def list(self):
        class R:
            pass
        r = R()
        r.data = [FakeModel(i) for i in self._ids]
        return r


class FakeClient:
    """Raises model_not_found for dead models; returns the model name otherwise."""

    def __init__(self, available):
        self.available = available
        self.models = FakeModels(available)
        self.calls = []

    def complete(self, model):
        self.calls.append(model)
        if model not in self.available:
            raise _not_found()
        return f"ok:{model}"


def make_call(client, model):
    return lambda: client.complete(model)


@pytest.fixture(autouse=True)
def reset_fallback_state(monkeypatch):
    monkeypatch.setattr(aa, "_active_model", None)
    monkeypatch.setattr(aa, "_model_fallback_event", None)
    monkeypatch.delenv("MODEL_FALLBACKS", raising=False)


def test_healthy_model_passes_through():
    client = FakeClient(["gpt-oss-120b"])
    result = aa._call_with_model_fallback(make_call, client, "gpt-oss-120b")
    assert result == "ok:gpt-oss-120b"
    assert aa.get_model_fallback_event() is None
    assert aa.get_active_model("gpt-oss-120b") == "gpt-oss-120b"


def test_deprecated_model_falls_back_and_is_remembered():
    client = FakeClient(["zai-glm-4.7", "gpt-oss-120b"])
    result = aa._call_with_model_fallback(make_call, client, "dead-model")
    # preference order: gpt-oss-120b comes before zai-glm-4.7
    assert result == "ok:gpt-oss-120b"
    event = aa.get_model_fallback_event()
    assert event["from"] == "dead-model" and event["to"] == "gpt-oss-120b"
    # subsequent calls go straight to the replacement — no second 404
    client.calls.clear()
    result = aa._call_with_model_fallback(make_call, client, "dead-model")
    assert result == "ok:gpt-oss-120b"
    assert client.calls == ["gpt-oss-120b"]


def test_fallback_prefers_env_override(monkeypatch):
    monkeypatch.setenv("MODEL_FALLBACKS", "zai-glm-4.7,gpt-oss-120b")
    client = FakeClient(["gpt-oss-120b", "zai-glm-4.7"])
    result = aa._call_with_model_fallback(make_call, client, "dead-model")
    assert result == "ok:zai-glm-4.7"


def test_no_preference_match_uses_any_available():
    client = FakeClient(["some-new-model"])
    result = aa._call_with_model_fallback(make_call, client, "dead-model")
    assert result == "ok:some-new-model"


def test_paid_fallback_client_rebinds_to_replacement_model():
    free = FakeClient(["gpt-oss-120b"])
    paid = FakeClient(["gpt-oss-120b"])
    built = []

    def mk(c, m):
        built.append((c, m))
        return lambda: c.complete(m)

    result = aa._call_with_model_fallback(mk, free, "dead-model", fallback_client=paid)
    assert result == "ok:gpt-oss-120b"
    # after the fallback engaged, the paid-tier callable must be rebuilt with
    # the REPLACEMENT model, not left bound to the dead one
    assert (paid, "gpt-oss-120b") in built
    assert (paid, "dead-model") in built  # pre-fallback build used the configured model


def test_failed_replacement_is_not_recorded():
    # models.list() advertises a model that itself 404s: the retry fails, and
    # neither _active_model nor the health event may claim the switch worked.
    class LyingClient(FakeClient):
        def __init__(self):
            super().__init__(available=[])
            self.models = FakeModels(["also-dead"])

    with pytest.raises(openai.NotFoundError):
        aa._call_with_model_fallback(make_call, LyingClient(), "dead-model")
    assert aa.get_model_fallback_event() is None
    assert aa.get_active_model("dead-model") == "dead-model"


def test_raced_request_reuses_other_threads_switch():
    # A request in flight when the model died: by the time its 404 lands,
    # another thread has already switched. It must reuse that switch without
    # consulting models.list(), and must not re-record the event.
    client = FakeClient(["raced-model"])
    list_calls = []
    orig_list = client.models.list
    client.models.list = lambda: (list_calls.append(1), orig_list())[1]

    attempted = []

    def mk(c, m):
        def _c():
            attempted.append(m)
            if m == "dead-model":
                aa._active_model = "raced-model"  # the "other thread" wins mid-flight
                raise _not_found()
            return c.complete(m)
        return _c

    result = aa._call_with_model_fallback(mk, client, "dead-model")
    assert result == "ok:raced-model"
    assert attempted == ["dead-model", "raced-model"]
    assert not list_calls, "models.list() must not be consulted when already switched"
    # idempotent recording: _active_model already held the target, so no event
    assert aa.get_model_fallback_event() is None


def test_non_model_errors_propagate():
    class Boom(Exception):
        pass

    def bad_call(client, model):
        def _c():
            raise Boom("unrelated")
        return _c

    with pytest.raises(Boom):
        aa._call_with_model_fallback(bad_call, FakeClient(["m"]), "m")
    assert aa.get_model_fallback_event() is None


def test_unresolvable_fallback_reraises():
    client = FakeClient([])  # provider offers nothing
    with pytest.raises(openai.NotFoundError):
        aa._call_with_model_fallback(make_call, client, "dead-model")
    assert aa.get_model_fallback_event() is None
