"""Prebuilt-database artifact: build_database / load_prebuilt.

The split exists so the serving process opens a finished DuckDB file (~0.5s,
~400MB) instead of rebuilding it from 531MB of CSVs (~6s, ~1.9GB) on every
boot — see LOU_MIGRATION_COMPAT.md and louisville-open-data-e5n.

Two properties matter and are covered here:
  1. The artifact must be EQUIVALENT to the CSV path. A prebuilt DB that
     serves subtly different numbers is worse than no prebuilt DB at all.
  2. The artifact must be LOCKED DOWN for serving. read_only=True alone still
     permits read_csv() of an arbitrary path and COPY TO — the connection
     serves LLM-generated SQL, so the filesystem must be unreachable from it.
"""
import hashlib
import os

import duckdb
import pytest

import data_model as dm


# Queries spanning the raw table, the canonical columns, the data-quality
# flags, and every summary table the prompt recommends.
EQUIVALENCE_QUERIES = [
    "SELECT agency_canonical, ROUND(SUM(extended_amount),2) s FROM expenditures "
    "WHERE is_data_artifact=FALSE GROUP BY 1 ORDER BY s DESC, 1 LIMIT 20",
    "SELECT payee_canonical, COUNT(DISTINCT agency_canonical) a FROM expenditures "
    "WHERE is_data_artifact=FALSE AND payee_canonical IS NOT NULL "
    "GROUP BY 1 ORDER BY a DESC, 1 LIMIT 20",
    "SELECT fiscal_year, ROUND(SUM(extended_amount),2) FROM expenditures GROUP BY 1 ORDER BY 1",
    "SELECT COUNT(*), SUM(is_offsetting::INT), SUM(is_data_artifact::INT) FROM expenditures",
    "SELECT * FROM summary_agency_spend ORDER BY 1 LIMIT 50",
    "SELECT * FROM summary_annual_spend ORDER BY 1 LIMIT 50",
    "SELECT * FROM summary_top_contractors ORDER BY total_spend DESC, payee LIMIT 50",
    "SELECT * FROM summary_grant_funding ORDER BY 1, 2 LIMIT 50",
]


def _hash(con, sql: str) -> str:
    return hashlib.sha1(str(con.execute(sql).fetchall()).encode()).hexdigest()


def _tiny_db(path: str) -> str:
    """A minimal artifact — enough to exercise open/lockdown without paying
    for a full 8s build."""
    con = duckdb.connect(path)
    con.execute("CREATE TABLE expenditures AS SELECT 1 AS fiscal_year, 2.0 AS extended_amount")
    con.close()
    return path


# ── load_prebuilt: contract and lockdown (fast, no real data needed) ─────────

def test_missing_artifact_raises_with_the_build_command(tmp_path):
    """A missing artifact must say how to make one. This fires at container
    startup, where the reader is whoever is debugging a failed deploy."""
    missing = str(tmp_path / "nope.duckdb")
    with pytest.raises(FileNotFoundError) as e:
        dm.load_prebuilt(missing)
    assert "--materialize" in str(e.value)
    assert missing in str(e.value)


def test_prebuilt_connection_is_read_only(tmp_path):
    con = dm.load_prebuilt(_tiny_db(str(tmp_path / "t.duckdb")))
    try:
        with pytest.raises(Exception):
            con.execute("INSERT INTO expenditures VALUES (9, 9.0)")
        with pytest.raises(Exception):
            con.execute("CREATE TABLE mischief AS SELECT 1")
    finally:
        con.close()


@pytest.mark.parametrize("escape", [
    "SELECT * FROM read_csv_auto('/etc/hosts')",
    "SELECT * FROM read_json_auto('/etc/hosts')",
    "COPY (SELECT 1) TO '/tmp/lou_test_should_not_exist.csv'",
])
def test_prebuilt_connection_cannot_reach_the_filesystem(tmp_path, escape):
    """read_only=True does NOT block these; the explicit lockdown does. This
    connection executes model-generated SQL, so the escape must be closed."""
    con = dm.load_prebuilt(_tiny_db(str(tmp_path / "t.duckdb")))
    try:
        with pytest.raises(duckdb.Error):
            con.execute(escape)
    finally:
        con.close()
    assert not os.path.exists("/tmp/lou_test_should_not_exist.csv")


def test_prebuilt_still_answers_queries(tmp_path):
    """The lockdown must not cost normal query ability."""
    con = dm.load_prebuilt(_tiny_db(str(tmp_path / "t.duckdb")))
    try:
        assert con.execute("SELECT COUNT(*) FROM expenditures").fetchone()[0] == 1
    finally:
        con.close()


# ── build_database: atomicity ────────────────────────────────────────────────

def test_failed_build_leaves_no_artifact(tmp_path, monkeypatch):
    """An interrupted build must not leave a half-populated file where the app
    would open it and serve wrong answers. It writes to a sibling and renames."""
    out = str(tmp_path / "out.duckdb")
    monkeypatch.setattr(dm, "_ingest",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        dm.build_database(out, "data")
    assert not os.path.exists(out), "partial artifact published"
    assert not os.path.exists(out + ".building"), "temp build file left behind"


def test_build_replaces_a_previous_artifact_and_stale_temp(tmp_path, monkeypatch):
    """A rebuild must overwrite the old artifact rather than fail on an
    existing file — refresh_data.py reruns into the same path — and must not
    be confused by a .building file left by an earlier hard kill."""
    out = str(tmp_path / "out.duckdb")
    _tiny_db(out)                                    # previous artifact
    duckdb.connect(out + ".building").close()        # stale temp from a crash

    monkeypatch.setattr(dm, "_ingest", lambda con, cfg, d: con.execute(
        "CREATE TABLE expenditures AS SELECT 1 AS fiscal_year, 2.0 AS extended_amount, 3 AS extra"))
    dm.build_database(out, "data")

    assert not os.path.exists(out + ".building")
    con = dm.load_prebuilt(out)
    try:
        cols = {c[0] for c in con.execute("DESCRIBE expenditures").fetchall()}
    finally:
        con.close()
    assert "extra" in cols, "artifact was not replaced by the rebuild"


# ── Equivalence against the CSV path (needs the real gitignored data) ────────

@pytest.fixture(scope="module")
def real_artifact(tmp_path_factory, require_louisville_data):
    """Build the real artifact once for this module (~8s)."""
    out = str(tmp_path_factory.mktemp("artifact") / "lou.duckdb")
    dm.build_database(out, "data")
    return out


@pytest.fixture(scope="module")
def in_memory(require_louisville_data):
    return dm.load_all_data("data")


def test_artifact_schema_matches_csv_path(real_artifact, in_memory):
    """The compact schema goes verbatim into the SQL system prompt. Any drift
    here changes what the model is told about the data."""
    pre = dm.load_prebuilt(real_artifact)
    try:
        assert dm.get_compact_schema_description(pre) == \
               dm.get_compact_schema_description(in_memory)
    finally:
        pre.close()


def test_artifact_year_context_matches_csv_path(real_artifact, in_memory):
    """year_context drives the fiscal-year rules in the prompt and the
    complete/partial claim about the newest year."""
    pre = dm.load_prebuilt(real_artifact)
    try:
        assert dm.year_context(pre, 7) == dm.year_context(in_memory, 7)
    finally:
        pre.close()


@pytest.mark.parametrize("sql", EQUIVALENCE_QUERIES)
def test_artifact_query_results_match_csv_path(real_artifact, in_memory, sql):
    pre = dm.load_prebuilt(real_artifact)
    try:
        assert _hash(pre, sql) == _hash(in_memory, sql)
    finally:
        pre.close()


def test_artifact_omits_internal_helper_tables(real_artifact):
    """The canonicalization helpers are CREATE TEMP TABLE, so they do not
    persist. They must not appear in the artifact (they are prompt noise, and
    the model must never query them)."""
    pre = dm.load_prebuilt(real_artifact)
    try:
        tables = [t[0] for t in pre.execute("SHOW TABLES").fetchall()]
    finally:
        pre.close()
    assert not [t for t in tables if t.startswith("_")], tables
    assert "expenditures" in tables
    assert "summary_agency_spend" in tables
