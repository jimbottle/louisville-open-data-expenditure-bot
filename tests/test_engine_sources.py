"""Unit tests for the engine's source-resolution and loading paths that the
big-city datasets exercise but CI can't (city data dirs are gitignored):
year-less glob/literal sources and column_map renames on the duckdb_union
reader — verified here with tiny fixtures."""

import os
import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from city_config import CityConfig  # noqa: E402
from data_model import _load_expenditures, _source_files  # noqa: E402


def _touch(d, name, content="a,b\n1,2\n"):
    path = os.path.join(d, name)
    with open(path, "w") as f:
        f.write(content)
    return path


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
