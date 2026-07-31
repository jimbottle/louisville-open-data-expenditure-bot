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
