"""RAG retrieval tests over a tiny fixture corpus (no network; skips cleanly
if the DuckDB FTS extension can't be loaded in this environment)."""

import os
import sys

import duckdb
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rag  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_repo_file(*parts) -> str:
    """Read a repo file without leaking the handle (ResourceWarning under -W error)."""
    with open(os.path.join(REPO, *parts)) as f:
        return f.read()

DOCS = [
    (1, "R-057-21", "Resolution", "Passed", "2021-06-07", None, None,
     "A RESOLUTION AUTHORIZING THE MAYOR TO ACCEPT FUNDING FROM THE AMERICAN RESCUE PLAN ACT OF 2021", "u1"),
    (2, "O-416-21", "Ordinance", "Passed", "2021-08-10", None, None,
     "AN ORDINANCE APPROPRIATING FUNDS TO THE FULLER CENTER FOR HOUSING OF LOUISVILLE", "u2"),
    (3, "O-999-22", "Ordinance", "Passed", "2022-01-01", None, None,
     "AN ORDINANCE CONCERNING STORMWATER DRAINAGE MAINTENANCE SCHEDULES", "u3"),
]


@pytest.fixture()
def fixture_db(tmp_path):
    db = str(tmp_path / "docs.duckdb")
    con = duckdb.connect(db)
    try:
        con.execute("INSTALL fts; LOAD fts;")
    except Exception:
        con.close()
        pytest.skip("DuckDB FTS extension unavailable in this environment")
    con.execute("""
        CREATE TABLE documents (
            doc_id INTEGER, file_no VARCHAR, matter_type VARCHAR, status VARCHAR,
            intro_date VARCHAR, passed_date VARCHAR, enactment_no VARCHAR,
            text VARCHAR, url VARCHAR
        )
    """)
    con.executemany("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?)", DOCS)
    con.execute("PRAGMA create_fts_index('documents', 'doc_id', 'text', overwrite=1)")
    con.close()
    return db


def test_build_db_swap_and_stale_partial_cleanup(tmp_path):
    db = str(tmp_path / "sub" / "docs.duckdb")  # parent dir must be created
    # simulate leftovers from a hard-killed prior ingest
    os.makedirs(os.path.dirname(db), exist_ok=True)
    for stale in (db + ".part", db + ".part.wal"):
        with open(stale, "w") as f:
            f.write("garbage")
    try:
        n = rag._build_db(DOCS, db)
    except Exception as e:
        if "fts" in str(e).lower() or "extension" in str(e).lower():
            pytest.skip("DuckDB FTS extension unavailable in this environment")
        raise
    assert n == len(DOCS)
    assert os.path.exists(db)
    assert not os.path.exists(db + ".part")
    assert not os.path.exists(db + ".part.wal")
    # the built DB serves retrieval end-to-end
    hits = rag.retrieve("american rescue plan", k=1, db_path=db, min_score=0.1)
    assert hits and hits[0]["file_no"] == "R-057-21"


def test_failed_build_preserves_existing_corpus(tmp_path):
    db = str(tmp_path / "docs.duckdb")
    try:
        rag._build_db(DOCS, db)
    except Exception as e:
        if "fts" in str(e).lower() or "extension" in str(e).lower():
            pytest.skip("DuckDB FTS extension unavailable in this environment")
        raise
    # a rebuild with malformed rows (wrong tuple arity) must fail...
    with pytest.raises(Exception):
        rag._build_db([("only", "three", "fields")], db)
    # ...while the original corpus stays intact and queryable
    hits = rag.retrieve("american rescue plan", k=1, db_path=db, min_score=0.1)
    assert hits and hits[0]["file_no"] == "R-057-21"
    assert not os.path.exists(db + ".part")
    assert not os.path.exists(db + ".part.wal")


def test_retrieve_ranks_relevant_doc_first(fixture_db):
    hits = rag.retrieve("american rescue plan funding", k=2, db_path=fixture_db, min_score=0.1)
    assert hits and hits[0]["file_no"] == "R-057-21"
    assert hits[0]["url"] == "u1"


def test_retrieve_empty_below_threshold(fixture_db):
    assert rag.retrieve("zzz nothing matches this", k=3, db_path=fixture_db, min_score=0.1) == []


def test_format_context(fixture_db):
    hits = rag.retrieve("housing appropriation", k=1, db_path=fixture_db, min_score=0.1)
    block = rag.format_context(hits)
    assert "[O-416-21]" in block and "cite by file number" in block
    assert rag.format_context([]) == ""


# ── config-pack driven corpus (a second city is a client name, not code) ─────

def test_corpus_settings_come_from_the_pack_not_the_module():
    from city_config import CityConfig
    cfg = CityConfig({"rag": {
        "legistar_client": "cincinnati", "matter_type_ids": [1, 2],
        "since": "2019-01-01", "min_score": 4.5, "k": 7,
    }}, ".")
    s = rag.corpus_settings(cfg)
    assert s["client"] == "cincinnati"
    assert "cincinnati" in s["api"] and "cincinnati" in s["web"]
    assert s["matter_type_ids"] == (1, 2)
    assert s["since"] == "2019-01-01"
    assert s["min_score"] == 4.5 and s["k"] == 7


def test_a_pack_with_no_rag_block_falls_back_to_the_module_defaults():
    from city_config import CityConfig
    s = rag.corpus_settings(CityConfig({}, "."))
    assert s["client"] == rag.LEGISTAR_CLIENT
    assert s["matter_type_ids"] == rag.MATTER_TYPE_IDS
    assert rag.corpus_settings(None)["client"] == rag.LEGISTAR_CLIENT


def test_db_path_resolves_against_the_deployments_data_dir():
    """The container mounts /data; a dev checkout uses ./data. The pack
    declares a bare filename so the same pack works in both."""
    from city_config import CityConfig
    cfg = CityConfig({"rag": {"db": "rag_documents.duckdb"}}, ".")
    assert rag.db_path(cfg, "/data") == os.path.join("/data", "rag_documents.duckdb")
    assert rag.db_path(cfg, "data") == os.path.join("data", "rag_documents.duckdb")
    # an explicit path is taken literally
    explicit = CityConfig({"rag": {"db": "/srv/corpus.duckdb"}}, ".")
    assert rag.db_path(explicit, "/data") == "/srv/corpus.duckdb"


def test_the_shipped_louisville_pack_declares_a_usable_corpus():
    from city_config import load_city_config
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = load_city_config(os.path.join(repo, "cities", "louisville", "city.yaml"))
    s = rag.corpus_settings(cfg)
    assert s["client"] == "louisville"
    assert len(s["matter_type_ids"]) >= 1
    # bare filename, or the container would look for ./data inside /app
    assert not os.path.dirname(s["db"])


# ── the ask pipeline degrades instead of failing ─────────────────────────────

def test_app_retrieval_returns_empty_when_no_corpus_exists(monkeypatch):
    import app
    monkeypatch.setattr(app, "RAG_DB", "/nonexistent/corpus.duckdb")
    assert app._retrieve_documents("anything") == []


def test_app_retrieval_swallows_a_corrupt_corpus(tmp_path, monkeypatch):
    """A broken corpus must cost citations, never the answer."""
    import app
    bad = tmp_path / "corpus.duckdb"
    bad.write_text("this is not a duckdb file")
    monkeypatch.setattr(app, "RAG_DB", str(bad))
    monkeypatch.setattr(app, "RAG_SETTINGS", {"min_score": 3.0, "k": 3})
    assert app._retrieve_documents("anything") == []


def test_app_retrieval_returns_hits_from_a_real_corpus(fixture_db, monkeypatch):
    import app
    monkeypatch.setattr(app, "RAG_DB", fixture_db)
    # BM25 scores scale with corpus size — the production floor of 3.0 is
    # calibrated for 1,400 documents, not this three-row fixture.
    monkeypatch.setattr(app, "RAG_SETTINGS", {"min_score": 0.1, "k": 3})
    hits = app._retrieve_documents("American Rescue Plan Act funding")
    assert hits and hits[0]["file_no"] == "R-057-21"
    # every field the sources SSE event and the frontend link need
    for key in ("file_no", "url", "matter_type", "intro_date", "text"):
        assert hits[0][key] is not None


def test_app_retrieval_honors_the_packs_threshold(fixture_db, monkeypatch):
    """The junk floor is a pack setting, so it has to be read per request —
    not baked in at the call site."""
    import app
    monkeypatch.setattr(app, "RAG_DB", fixture_db)
    monkeypatch.setattr(app, "RAG_SETTINGS", {"min_score": 0.1, "k": 3})
    assert app._retrieve_documents("American Rescue Plan Act funding")
    monkeypatch.setattr(app, "RAG_SETTINGS", {"min_score": 99.0, "k": 3})
    assert app._retrieve_documents("American Rescue Plan Act funding") == []


def test_app_retrieval_honors_the_packs_k(fixture_db, monkeypatch):
    import app
    monkeypatch.setattr(app, "RAG_DB", fixture_db)
    monkeypatch.setattr(app, "RAG_SETTINGS", {"min_score": 0.0, "k": 1})
    assert len(app._retrieve_documents("american rescue plan housing funding")) == 1


# ── documents reach the prompt without poisoning the cache key ───────────────

def test_documents_ride_the_user_message_not_the_system_prompt():
    """Per-question context in the system prompt would change CACHE_VERSION on
    every question and prune the whole response cache."""
    import analytics_agent
    captured = {}

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    captured.update(kw)
                    return iter([])

    list(analytics_agent.interpret_results_stream(
        FakeClient(), "m", "SYSTEM", "q", "SELECT 1", "RESULTS",
        documents="## Related city legislation\n- [O-1] text",
    ))
    system = [m for m in captured["messages"] if m["role"] == "system"][0]["content"]
    user = [m for m in captured["messages"] if m["role"] == "user"][0]["content"]
    assert system == "SYSTEM"
    assert "O-1" in user and "RESULTS" in user


def test_no_documents_leaves_the_prompt_untouched():
    import analytics_agent
    captured = {}

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    captured.update(kw)
                    return iter([])

    list(analytics_agent.interpret_results_stream(
        FakeClient(), "m", "SYSTEM", "q", "SELECT 1", "RESULTS"))
    user = [m for m in captured["messages"] if m["role"] == "user"][0]["content"]
    assert user.endswith("RESULTS")
    assert "legislation" not in user.lower()


def test_both_interpret_prompts_guard_against_over_citing():
    """A retrieved document is background, not a source for the numbers. Both
    the served prompt and the CLI-path prompt must say so, or the model will
    attribute figures to whatever legislation the keyword search returned."""
    import analytics_agent
    served = read_repo_file("app.py")
    cli = analytics_agent.build_interpret_prompt("SCHEMA")
    import re
    for text in (served, cli):
        # the CLI prompt is wrapped, so compare on normalized whitespace
        flat = re.sub(r"\s+", " ", text)
        assert "Related city legislation" in flat
        # the model must judge relevance, cite what it uses, and never let a
        # document become the authority for a number
        assert "never attribute a figure to a document" in flat
        assert "never list documents you did not use" in flat


# ── the footer lists citations, not search results ──────────────────────────

def test_refine_sink_collects_the_text_actually_served():
    """The caller needs the served answer to know what it cited."""
    from analytics_agent import refine_events_with_fallback
    sink = []
    list(refine_events_with_fallback(
        iter(["Spending rose ", "per O-374-22."]), "DRAFT",
        send=lambda t, d: (t, d), transform=str.upper, sink=sink))
    assert "".join(sink) == "SPENDING ROSE PER O-374-22."


def test_refine_sink_collects_the_draft_when_refinement_produces_nothing():
    from analytics_agent import refine_events_with_fallback
    sink = []
    list(refine_events_with_fallback(
        iter([]), "the draft answer", send=lambda t, d: (t, d), sink=sink))
    assert "".join(sink) == "the draft answer"


@pytest.fixture()
def hits():
    """Fresh per test — a module-level list is shared mutable state."""
    return [
        {"file_no": "O-374-22", "url": "u1", "matter_type": "Ordinance",
         "intro_date": "2022-12-12", "text": "ARP reappropriations"},
        {"file_no": "NDF102021BLC06", "url": "u2", "matter_type": "Fund",
         "intro_date": "2021-10-20", "text": "$1,000 district appropriation"},
    ]


def test_uncited_retrievals_are_filtered_out_of_the_footer(hits):
    """BM25 returns a loosely-matching ordinance for almost any question; only
    what the answer cites belongs under it."""
    import app
    answer = "Spending rose sharply in 2022, largely ARP money (per O-374-22)."
    assert [h["file_no"] for h in app._cited_documents(hits, answer)] == ["O-374-22"]


def test_an_answer_citing_nothing_produces_no_footer(hits):
    import app
    answer = "The three highest paid positions are Director, Chief, and Manager."
    assert app._cited_documents(hits, answer) == []
    assert app._cited_documents(hits, "") == []
    assert app._cited_documents([], "cites O-374-22") == []


def test_every_cited_document_is_carried_through(hits):
    import app
    answer = "See O-374-22 and NDF102021BLC06 for the appropriations."
    assert len(app._cited_documents(hits, answer)) == 2


def test_retrieve_installs_fts_when_the_host_lacks_it(monkeypatch, fixture_db):
    """A fresh container has no FTS extension, and LOAD does not install one.
    Without the fallback every answer silently loses its citations."""
    calls = []
    real_execute = duckdb.DuckDBPyConnection.execute

    def flaky(self, sql, *a, **k):
        calls.append(sql)
        if sql == "LOAD fts;" and len([c for c in calls if c == "LOAD fts;"]) == 1:
            raise duckdb.IOException('Extension "fts.duckdb_extension" not found')
        return real_execute(self, sql, *a, **k)

    monkeypatch.setattr(duckdb.DuckDBPyConnection, "execute", flaky)
    hits = rag.retrieve("american rescue plan", k=1, db_path=fixture_db, min_score=0.1)
    assert hits and hits[0]["file_no"] == "R-057-21"
    assert any("INSTALL fts" in c for c in calls), "did not recover by installing"


# ── the sources SSE frame the frontend reads verbatim ────────────────────────

FRONTEND_SOURCE_KEYS = {"file_no", "url", "matter_type", "intro_date", "title"}


def test_the_sources_frame_carries_exactly_the_keys_the_page_reads(hits):
    """static/index.html reads these five names off each item; renaming one on
    either side silently drops content from every source row."""
    import json
    import app

    def send(event_type, data):
        return f"data: {json.dumps({'type': event_type, **data})}\n\n"

    frames = app._sources_event(hits, "as authorized by O-374-22", send)
    assert len(frames) == 1
    payload = json.loads(frames[0][len("data: "):])
    assert payload["type"] == "sources"
    assert len(payload["items"]) == 1
    assert set(payload["items"][0]) == FRONTEND_SOURCE_KEYS
    assert payload["items"][0]["file_no"] == "O-374-22"

    page = read_repo_file("static", "index.html")
    block = page[page.index("event.type === 'sources'"):page.index("event.type === 'log'")]
    for key in FRONTEND_SOURCE_KEYS:
        assert f"s.{key}" in block, f"the page stopped reading {key}"


def test_retrieved_but_uncited_documents_produce_a_diagnostic_not_a_footer(hits):
    import app
    frames = app._sources_event(hits, "No legislation is named here.", lambda t, d: (t, d))
    assert [f[0] for f in frames] == ["debug"]
    assert "none cited" in frames[0][1]["content"]
    assert app._sources_event([], "anything", lambda t, d: (t, d)) == []


def test_the_zero_row_path_emits_the_footer_too():
    """The empty-result branch returns early; it fed documents to the model
    but skipped the footer, so a citation there shipped with no link — on the
    one path with no results table to fall back on either."""
    src = read_repo_file("app.py")
    branch = src[src.index("if len(result_df) == 0:"):src.index('yield send("log", {"content": "Interpreting results..."})')]
    assert "_sources_event(doc_hits" in branch, "zero-row path skips the citation footer"
    assert branch.index("_sources_event") < branch.index('send("done"')


def test_file_numbers_match_on_token_boundaries(hits):
    """An unanchored substring test attaches unrelated ordinances: a bare
    numeric file number matches a year in prose, and R-57-21 is a substring
    of R-57-215."""
    import app
    assert app._cited_documents(
        [{"file_no": "R-57-21", "url": "u", "matter_type": "t",
          "intro_date": "d", "text": "x"}],
        "See R-57-215 for details.") == []
    assert app._cited_documents(
        [{"file_no": "2021", "url": "u", "matter_type": "t",
          "intro_date": "d", "text": "x"}],
        "Spending rose in 2021 sharply.") == [], "all-digit file numbers are unsafe tokens"


def test_the_app_wires_documents_into_all_three_llm_calls():
    """Each call site is one keyword away from silently reverting: the draft
    loses its context, the zero-row explanation answers uncited, or the refiner
    strips every citation again. Asserted per site rather than as a total, so
    the failure names the branch that lost it and a fourth legitimate call site
    doesn't fail the test for the wrong reason."""
    src = read_repo_file("app.py")
    zero_row = src[src.index("if len(result_df) == 0:"):
                   src.index('yield send("log", {"content": "Interpreting results..."})')]
    draft = src[src.index('yield send("log", {"content": "Interpreting results..."})'):
                src.index("refine_events_with_fallback(")]
    refine = src[src.index("refine_events_with_fallback("):
                 src.index('_sources_event(doc_hits, "".join(served_text)')]
    for name, region in (("zero-row explanation", zero_row),
                         ("draft interpretation", draft),
                         ("refine pass", refine)):
        assert "documents=documents" in region, f"{name} lost the retrieved documents"


def test_the_interpretation_stream_is_humanized_as_prose_not_as_a_table():
    """Reverting any one of these call sites to humanize_text reintroduces
    'Other Pay notable spends' with the whole suite green."""
    src = read_repo_file("app.py")
    body = src[src.index("def event_stream():"):]
    assert "humanize_text(chunk)" not in body
    assert "humanize_text(draft)" not in body
    assert "transform=humanize_prose" in body


def test_typographic_dashes_still_count_as_a_citation():
    """Models typeset identifiers with non-breaking hyphens: an answer reading
    "Resolution R‑083‑21" shipped with an empty footer while the citation
    sat in plain sight."""
    import app
    hits = [{"file_no": "R-083-21", "url": "u", "matter_type": "Resolution",
             "intro_date": "2021-08-09", "text": "priorities"}]
    for dash in ("‐", "‑", "‒", "–", "−"):
        answer = f"Resolution R{dash}083{dash}21 established the priorities."
        assert app._cited_documents(hits, answer), f"missed U+{ord(dash):04X}"
    # the boundary guard must survive the dash tolerance
    assert app._cited_documents(hits, "See R‑083‑21‑A for more.") == []


def test_a_dash_used_as_prose_punctuation_does_not_block_the_citation():
    """The other direction, and the more common one: em dashes flanking an
    ASCII file number are punctuation, not part of the identifier. Folding them
    into hyphens made them token characters and silently dropped the footer on
    answers that had matched before."""
    import app
    hits = [{"file_no": "R-083-21", "url": "u", "matter_type": "Resolution",
             "intro_date": "2021-08-09", "text": "priorities"}]
    for answer in (
        "Two measures—R-083-21 and O-120-21—were adopted.",
        "The largest areas—R-083-21—were set that year.",
        "Council acted twice―R-083-21―in the same session.",
    ):
        assert app._cited_documents(hits, answer), f"dropped: {answer!r}"


def test_an_em_dash_joined_pair_cites_both_ends():
    """Two file numbers joined as an aside or a range. Documents the limit of
    the split: an en dash between them (R-083-20–R-083-21) is indistinguishable
    from an en dash *inside* an identifier (R–083–21), and identifier wins — so
    that form stays uncited. Em dash, the far more common prose form, matches."""
    import app
    hits = [{"file_no": "R-083-21", "url": "u", "matter_type": "Resolution",
             "intro_date": "2021-08-09", "text": "priorities"}]
    assert app._cited_documents(hits, "Resolutions R-083-20—R-083-21 were adopted.")
    assert app._cited_documents(hits, "Resolutions R-083-20–R-083-21 were adopted.") == []
