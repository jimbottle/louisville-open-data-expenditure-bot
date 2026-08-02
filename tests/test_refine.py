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
    assert "mutually exclusive" in p             # the overlapping-views rule
    # lean by design: the rubric itself stays small (well under 1K tokens)
    assert len(p) < 2400


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
    # ...and the schema is NOT (lean-context invariant). Targeted at the
    # schema itself rather than at any markdown header: format_context emits a
    # "## " heading, so the old check would have passed only by accident of
    # this test omitting documents.
    assert "expenditures (" not in user and "Schema context" not in user
    assert client.captured["stream"] is True


# ── retrieved documents reach the refiner ────────────────────────────────────

def _refine(client, **kw):
    return "".join(aa.refine_interpretation_stream(
        client, "m", kw.pop("question", "q"), kw.pop("sql", "SELECT 1"),
        kw.pop("results", "RESULTS"), kw.pop("draft", "the draft"), **kw))


def test_documents_reach_the_refiner_or_it_deletes_every_citation():
    """The refiner's own rule is that anything the results don't support must
    go, and a file number never appears in a results table — without the
    document block it strips the citation the draft just made, which is
    exactly what shipped uncited answers to production."""
    import rag
    hit = {"file_no": "R-083-21", "matter_type": "Resolution", "status": "Passed",
           "intro_date": "2021-08-09", "text": "PRIORITY AREAS FOR ARP FUNDS",
           "url": "u1"}
    # built with the real formatter, so a change to the block's shape in
    # rag.py fails here instead of silently dangling the rubric's reference
    block = rag.format_context([hit])
    client = FakeStreamingClient(["Refined."])
    _refine(client, draft="Priorities were set in R-083-21.", documents=block)
    user = client.captured["messages"][1]["content"]
    assert "R-083-21" in user and "PRIORITY AREAS" in user
    # the block must precede the draft, so "the draft" is unambiguous
    assert user.index("R-083-21]") < user.index("DRAFT ANSWER")


def test_the_rubric_names_the_block_the_formatter_actually_emits():
    """The carve-out refers to the block by name; if the header and the rubric
    drift apart the refiner loses the cue that exempts it from deletion."""
    import re
    import rag
    header = rag.format_context([{"file_no": "X", "matter_type": "t",
                                  "status": "s", "intro_date": "d",
                                  "text": "b"}]).splitlines()[0]
    words = re.sub(r"[^a-z ]", " ", header.lower()).split()
    flat = re.sub(r"\s+", " ", aa.REFINE_SYSTEM_PROMPT).lower()
    for word in ("related", "city", "legislation"):
        assert word in words, f"formatter header lost {word!r}"
        assert word in flat, f"refine rubric no longer names {word!r}"


def test_no_documents_leaves_the_refine_prompt_untouched():
    client = FakeStreamingClient(["Refined."])
    _refine(client, draft="the draft")
    user = client.captured["messages"][1]["content"]
    assert "legislation" not in user.lower()
    assert user.endswith("DRAFT ANSWER:\nthe draft")


def test_the_carve_out_cannot_become_a_licence_to_invent_or_to_restate_scope():
    import re
    flat = re.sub(r"\s+", " ", aa.REFINE_SYSTEM_PROMPT)
    assert "Delete anything the results don't support" in flat
    assert "Never introduce a citation the draft did not make" in flat
    # the carve-out must not reopen the coverage/date hallucination the
    # deletion rule exists to close
    assert "Never use a document to state what a figure includes" in flat


def test_a_citation_survives_the_whole_refine_helper_into_the_sink():
    """End-to-end over the path that produces the served answer: draft with a
    file number -> refine stream -> sink, which is what the footer matches."""
    sink = []
    list(aa.refine_events_with_fallback(
        iter(["Council set the priorities in ", "R-083-21."]),
        "draft", send=_send, sink=sink))
    import app
    hits = [{"file_no": "R-083-21", "url": "u", "matter_type": "Resolution",
             "intro_date": "2021-08-09", "text": "priorities"}]
    assert app._cited_documents(hits, "".join(sink))
