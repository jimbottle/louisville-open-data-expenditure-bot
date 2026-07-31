"""Refinement-pass tests: rubric content and the streaming wrapper, using a
fake OpenAI-shaped client (no network)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analytics_agent as aa  # noqa: E402


def test_refine_prompt_carries_the_load_bearing_rules():
    p = aa.REFINE_SYSTEM_PROMPT
    assert "192,770.57" in p and "$192.8K" in p  # the magnitude-check exemplar
    assert "RESULTS" in p                        # accuracy anchor named
    assert "non-technical" in p.lower()
    assert "agency_canonical" in p               # jargon example
    # lean by design: the rubric itself stays small (well under 1K tokens)
    assert len(p) < 2000


class _Delta:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.delta = _Delta(content)


class _Chunk:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class FakeStreamingClient:
    """Captures the create() kwargs and streams canned chunks."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.captured = None
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.captured = kwargs
                return iter([_Chunk(c) for c in outer._chunks])

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def _send(event_type, data):
    return {"type": event_type, **data}


def _interps(events):
    return "".join(e["content"] for e in events if e["type"] == "interpretation")


def test_fallback_serves_draft_when_refiner_fails_before_first_chunk():
    def dead_iter():
        raise RuntimeError("boom")
        yield  # pragma: no cover

    events = list(aa.refine_events_with_fallback(dead_iter(), "the draft answer", _send))
    assert _interps(events) == "the draft answer"
    assert any(e["type"] == "debug" and "serving the draft" in e["content"] for e in events)


def test_fallback_truncation_note_when_refiner_fails_mid_stream():
    def partial_iter():
        yield "Refined so far. "
        raise RuntimeError("boom")

    events = list(aa.refine_events_with_fallback(partial_iter(), "the draft answer", _send))
    text = _interps(events)
    assert text.startswith("Refined so far. ")
    assert "(Response truncated.)" in text
    assert "the draft answer" not in text  # never append the draft after partial refined text


def test_refine_success_streams_only_refined_text():
    counter = {"n": 0}
    events = list(aa.refine_events_with_fallback(
        iter(["Clean ", "answer."]), "the draft answer", _send,
        transform=str.upper, counter=counter,
    ))
    assert _interps(events) == "CLEAN ANSWER."
    assert counter["n"] == 2
    assert any(e["type"] == "debug" and e["content"].startswith("Refined in") for e in events)


def test_refine_timeout_truncates_and_stops_consuming():
    consumed = []

    def two_chunks():
        for c in ("First chunk. ", "Second chunk."):
            consumed.append(c)
            yield c

    # timeout=-1: any elapsed time exceeds it, deterministic regardless of
    # platform clock resolution (timeout=0 could see a 0.0 delta on coarse clocks)
    events = list(aa.refine_events_with_fallback(
        two_chunks(), "the draft answer", _send, timeout=-1,
    ))
    text = _interps(events)
    assert text.startswith("First chunk. ")
    assert "(Response truncated due to timeout)" in text
    assert consumed == ["First chunk. "]  # the break stops the stream
    assert "the draft answer" not in text


def test_refine_stream_yields_chunks_and_uses_lean_context(monkeypatch):
    monkeypatch.setattr(aa, "_active_model", None)
    client = FakeStreamingClient(["Refined ", "answer."])
    out = "".join(aa.refine_interpretation_stream(
        client, "test-model",
        question="How much grant funding?",
        sql="SELECT * FROM summary_grant_funding",
        results="fund total\nGrant Fund 437,477,543.05",
        draft="Louisville received $437.5M from the Grant Fund.",
    ))
    assert out == "Refined answer."
    msgs = client.captured["messages"]
    assert msgs[0]["content"] == aa.REFINE_SYSTEM_PROMPT
    user = msgs[1]["content"]
    # the four inputs are present...
    for marker in ("QUESTION:", "SQL EXECUTED:", "RESULTS:", "DRAFT ANSWER:"):
        assert marker in user
    # ...and the schema is NOT (lean-context invariant)
    assert "## " not in user and "expenditures (" not in user
    assert client.captured["stream"] is True
