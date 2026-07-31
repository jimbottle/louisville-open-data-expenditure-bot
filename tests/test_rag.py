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
