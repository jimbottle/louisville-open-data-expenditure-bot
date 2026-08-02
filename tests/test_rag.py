"""RAG retrieval tests over a tiny fixture corpus (no network; skips cleanly
if the DuckDB FTS extension can't be loaded in this environment)."""

import os
import sys

import duckdb
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rag  # noqa: E402

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
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    served = open(os.path.join(repo, "app.py")).read()
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


HITS = [
    {"file_no": "O-374-22", "url": "u1", "matter_type": "Ordinance",
     "intro_date": "2022-12-12", "text": "ARP reappropriations"},
    {"file_no": "NDF102021BLC06", "url": "u2", "matter_type": "Fund",
     "intro_date": "2021-10-20", "text": "$1,000 district appropriation"},
]


def test_uncited_retrievals_are_filtered_out_of_the_footer():
    """BM25 returns a loosely-matching ordinance for almost any question; only
    what the answer cites belongs under it."""
    import app
    answer = "Spending rose sharply in 2022, largely ARP money (per O-374-22)."
    assert [h["file_no"] for h in app._cited_documents(HITS, answer)] == ["O-374-22"]


def test_an_answer_citing_nothing_produces_no_footer():
    import app
    answer = "The three highest paid positions are Director, Chief, and Manager."
    assert app._cited_documents(HITS, answer) == []
    assert app._cited_documents(HITS, "") == []
    assert app._cited_documents([], "cites O-374-22") == []


def test_every_cited_document_is_carried_through():
    import app
    answer = "See O-374-22 and NDF102021BLC06 for the appropriations."
    assert len(app._cited_documents(HITS, answer)) == 2


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


def test_refine_receives_the_documents_or_it_deletes_every_citation():
    """The refiner's own rule is that anything the results don't support must
    go, and a file number never appears in a results table — without the
    document block it strips the citation the draft just made."""
    import analytics_agent
    captured = {}

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    captured.update(kw)
                    return iter([])

    list(analytics_agent.refine_interpretation_stream(
        FakeClient(), "m", "q", "SELECT 1", "RESULTS", "draft citing R-083-21",
        documents="## Related city legislation\n- [R-083-21] priorities"))
    user = [m for m in captured["messages"] if m["role"] == "user"][0]["content"]
    assert "R-083-21] priorities" in user
    # the document block must precede the draft, so "the draft" is unambiguous
    assert user.index("Related city legislation") < user.index("DRAFT ANSWER")


def test_refine_without_documents_is_unchanged():
    import analytics_agent
    captured = {}

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    captured.update(kw)
                    return iter([])

    list(analytics_agent.refine_interpretation_stream(
        FakeClient(), "m", "q", "SELECT 1", "RESULTS", "the draft"))
    user = [m for m in captured["messages"] if m["role"] == "user"][0]["content"]
    assert "legislation" not in user.lower()
    assert user.endswith("DRAFT ANSWER:\nthe draft")


def test_the_refine_rubric_carves_citations_out_of_its_delete_rule():
    import re
    from analytics_agent import REFINE_SYSTEM_PROMPT
    flat = re.sub(r"\s+", " ", REFINE_SYSTEM_PROMPT)
    assert "Delete anything the results don't support" in flat
    assert "ONE exception to that deletion rule" in flat
    # the carve-out must not become a licence to invent citations
    assert "Never introduce a citation the draft did not make" in flat
