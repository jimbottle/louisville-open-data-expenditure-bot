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
