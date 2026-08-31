"""Unit tests for the engine's source-resolution and loading paths that the
big-city datasets exercise but CI can't (city data dirs are gitignored):
year-less glob/literal sources and column_map renames on the duckdb_union
reader — verified here with tiny fixtures."""

import os
import sys

import duckdb
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from city_config import CityConfig  # noqa: E402
from data_model import (  # noqa: E402
    _build_summaries,
    _load_expenditures,
    _source_files,
    get_compact_schema_description,
    get_full_schema_description,
)


def _touch(d, name, content="a,b\n1,2\n"):
    path = os.path.join(d, name)
    with open(path, "w") as f:
        f.write(content)
    return path


# ── Schema description determinism ───────────────────────────────────────────
# The schema text goes into every LLM prompt AND into app.py's prompt-hash
# cache version. Unordered DISTINCT scans once made it vary per process, so
# every restart invalidated the whole response cache (louisville-open-data-xmf).

def _schema_fixture_con():
    con = duckdb.connect()
    con.execute("CREATE TABLE expenditures (fund VARCHAR, amount DOUBLE)")
    # deliberately inserted out of alphabetical order
    con.executemany("INSERT INTO expenditures VALUES (?, ?)",
                    [("Zebra Fund", 1.0), ("Alpha Fund", 2.0), ("Middle Fund", 3.0)])
    return con


def test_compact_schema_enums_are_sorted_and_stable():
    con = _schema_fixture_con()
    out = get_compact_schema_description(con)
    assert out == get_compact_schema_description(con)
    enums = out[out.index("{") + 1:out.index("}")].split(", ")
    assert enums == sorted(enums), f"enum values must be ordered, got {enums}"


def test_full_schema_samples_are_sorted_and_stable():
    con = _schema_fixture_con()
    out = get_full_schema_description(con)
    assert out == get_full_schema_description(con)
    assert out.index("'Alpha Fund'") < out.index("'Middle Fund'") < out.index("'Zebra Fund'")


# ── _source_files ────────────────────────────────────────────────────────────

def test_source_files_year_range(tmp_path):
    d = str(tmp_path)
    _touch(d, "exp_2020.csv")
    _touch(d, "exp_2022.csv")  # 2021 intentionally missing
    src = {"files": "exp_{year}.csv", "years": [2020, 2022]}
    files = _source_files(src, d)
    assert [y for y, _ in files] == [2020, 2022]


def test_source_files_literal_no_years(tmp_path):
    d = str(tmp_path)
    _touch(d, "payments.csv")
    files = _source_files({"files": "payments.csv"}, d)
    assert len(files) == 1
    assert files[0][0] is None
    assert files[0][1].endswith("payments.csv")


def test_source_files_glob_no_years(tmp_path):
    d = str(tmp_path)
    _touch(d, "part_a.csv")
    _touch(d, "part_b.csv")
    _touch(d, "other.txt")
    files = _source_files({"files": "part_*.csv"}, d)
    assert [os.path.basename(p) for _, p in files] == ["part_a.csv", "part_b.csv"]


def test_source_files_missing(tmp_path):
    assert _source_files({"files": "nope.csv"}, str(tmp_path)) == []
    assert _source_files({"files": "exp_{year}.csv", "years": [2020, 2021]}, str(tmp_path)) == []


# ── duckdb_union with column_map (the Cincinnati path) ───────────────────────

def _cfg(tmp_path, sources):
    return CityConfig({"expenditures": {"table": "expenditures", "sources": sources}}, str(tmp_path))


def test_duckdb_union_column_map_renames(tmp_path):
    d = str(tmp_path)
    _touch(d, "payments.csv", "VENDOR_NAME,AMOUNT,DEPT\nAcme,12.5,Parks\nBolt,3.25,Water\n")
    cfg = _cfg(tmp_path, [{
        "id": "s1",
        "reader": "duckdb_union",
        "files": "payments.csv",
        "column_map": {"VENDOR_NAME": "payee", "AMOUNT": "amount", "DEPT": "DEPT"},  # identity entry skipped
    }])
    con = duckdb.connect()
    _load_expenditures(con, cfg, d)
    cols = [c[0] for c in con.execute("DESCRIBE expenditures").fetchall()]
    assert "payee" in cols and "amount" in cols and "DEPT" in cols
    assert "VENDOR_NAME" not in cols
    assert con.execute("SELECT COUNT(*) FROM expenditures").fetchone()[0] == 2
    assert con.execute("SELECT payee FROM expenditures ORDER BY payee").fetchall() == [("Acme",), ("Bolt",)]


def test_duckdb_union_quoted_identifier_rename(tmp_path):
    # An embedded double quote in a source column must not produce broken SQL
    d = str(tmp_path)
    _touch(d, "q.csv", '"weird""name",x\n1,2\n')
    cfg = _cfg(tmp_path, [{
        "id": "s1",
        "reader": "duckdb_union",
        "files": "q.csv",
        "column_map": {'weird"name': "clean_name"},
    }])
    con = duckdb.connect()
    _load_expenditures(con, cfg, d)
    cols = [c[0] for c in con.execute("DESCRIBE expenditures").fetchall()]
    assert "clean_name" in cols


def test_pandas_mapped_yearless_source(tmp_path):
    # year-less sources are legal for pandas_mapped too; no 'None' year leakage
    d = str(tmp_path)
    _touch(d, "old.csv", "Old_A,Old_B\n5,6\n")
    cfg = _cfg(tmp_path, [{
        "id": "s1",
        "reader": "pandas_mapped",
        "files": "old.csv",
        "column_map": {"Old_A": "a", "Old_B": "b"},
    }])
    con = duckdb.connect()
    _load_expenditures(con, cfg, d)
    assert con.execute("SELECT a, b FROM expenditures").fetchone() == (5, 6)


# ── _build_summaries diagnostics ─────────────────────────────────────────────
# Both warnings are operator-facing signals for a misconfigured city pack;
# neither was covered, so a refactor could re-raise from the probe or drop the
# empty warning with the suite still green.

def _summary_cfg(tmp_path, summaries):
    return CityConfig({"summaries": summaries}, str(tmp_path))


def _seeded_con():
    con = duckdb.connect()
    con.execute("CREATE TABLE expenditures (fiscal_year INTEGER, amount DOUBLE)")
    con.execute("INSERT INTO expenditures VALUES (2025, 10.0), (2026, 5.0)")
    return con


def test_build_summaries_warns_when_a_summary_materializes_empty(tmp_path, caplog):
    cfg = _summary_cfg(tmp_path, [{
        "table": "summary_empty",
        "sql": "CREATE TABLE summary_empty AS SELECT * FROM expenditures WHERE fiscal_year = 1900",
    }])
    with caplog.at_level("WARNING"):
        _build_summaries(_seeded_con(), cfg)
    assert "materialized EMPTY" in caplog.text, caplog.text
    assert "summary_empty" in caplog.text


@pytest.mark.parametrize("table_key,expected_exc", [
    ("summary_wrong_name", "CatalogException"),      # valid identifier, no such table
    ("not a bare identifier", "ParserException"),    # can't even be parsed as a name
])
def test_build_summaries_survives_a_table_key_that_does_not_match_its_sql(
    tmp_path, caplog, table_key, expected_exc
):
    # The probe must never abort loading, and the warning must carry both
    # operator-facing details: which key was wrong and why it failed.
    cfg = _summary_cfg(tmp_path, [{
        "table": table_key,
        "sql": "CREATE TABLE summary_actual AS SELECT * FROM expenditures",
    }])
    con = _seeded_con()
    with caplog.at_level("WARNING"):
        _build_summaries(con, cfg)  # must not raise
    assert "cannot check whether summary table" in caplog.text, caplog.text
    # Assert the message's OWN rendering of the key: the interpolated
    # exception text happens to contain the bare name too, so a substring
    # check would still pass if the %r were deleted from the format string.
    assert f"summary table {table_key!r}" in caplog.text, (
        f"warning omits its own rendering of the offending key {table_key!r}"
    )
    assert expected_exc in caplog.text, f"warning omits the exception type for {table_key!r}"
    # the SQL still ran, so the real table exists
    assert con.execute("SELECT COUNT(*) FROM summary_actual").fetchone()[0] == 2


# ── Date sanity (louisville-open-data-ukl) ───────────────────────────────────
# The source extract carries invoice dates in years 2102, 2502, 7202. They sort
# to the newest end of every month series — the position a reader trusts most.

def test_date_sanity_nulls_impossible_dates_and_keeps_the_amount():
    from data_model import _apply_data_quality
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE expenditures AS SELECT * FROM (VALUES
            (2024, DATE '2023-09-01', DATE '2023-09-15', 'A', 'inv1', 100.0),
            (2024, DATE '2502-05-28', DATE '2023-07-10', 'A', 'inv2', 785.66),
            (2026, DATE '7202-07-14', DATE '2025-09-08', 'B', 'inv3', 1533.42),
            (2010, DATE '1999-12-31', DATE '2009-08-01', 'B', 'inv4', 5.0),
            (2026, DATE '2027-06-30', DATE '2026-01-01', 'B', 'inv5', 7.0)
        ) t(fiscal_year, invoice_date, payment_date, payee, invoice_number, extended_amount)
    """)
    cfg = CityConfig({"data_quality": {
        "table": "expenditures", "amount_column": "extended_amount",
        "date_sanity": {"columns": ["invoice_date", "payment_date"], "min": "2000-01-01",
                        "max_years_after_newest": 1},
    }}, "/nonexistent")
    _apply_data_quality(con, cfg)
    rows = con.execute("SELECT invoice_number, invoice_date, extended_amount FROM expenditures ORDER BY invoice_number").fetchall()
    dates = {r[0]: r[1] for r in rows}
    assert dates["inv1"] is not None and dates["inv5"] is not None   # in window (newest FY 2026 + 1)
    assert dates["inv2"] is None and dates["inv3"] is None and dates["inv4"] is None
    # The amounts are untouched: the row is still real spending.
    assert float(con.execute("SELECT ROUND(SUM(extended_amount), 2) FROM expenditures").fetchone()[0]) == 2431.08
    # payment_date was in range everywhere and stays.
    assert con.execute("SELECT COUNT(*) FROM expenditures WHERE payment_date IS NULL").fetchone()[0] == 0


def test_date_sanity_is_off_unless_configured():
    from data_model import _apply_data_quality
    con = duckdb.connect()
    con.execute("CREATE TABLE expenditures AS SELECT 2024 AS fiscal_year, DATE '7202-07-14' AS invoice_date, 1.0 AS extended_amount")
    _apply_data_quality(con, CityConfig({"data_quality": {"table": "expenditures", "amount_column": "extended_amount"}}, "/x"))
    assert con.execute("SELECT invoice_date FROM expenditures").fetchone()[0] is not None
