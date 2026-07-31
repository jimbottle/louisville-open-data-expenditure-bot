"""Config-pack integrity tests: every shipped cities/*/city.yaml must load
through CityConfig with the shapes the engine expects, and the Louisville
pack's canonical maps must keep their expected entry counts (a malformed CSV
row silently shrinks the map — these counts are the tripwire)."""

import glob
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from city_config import load_city_config  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALL_PACKS = sorted(glob.glob(os.path.join(REPO, "cities", "*", "city.yaml")))
LOUISVILLE = os.path.join(REPO, "cities", "louisville", "city.yaml")


def test_packs_exist():
    assert LOUISVILLE in ALL_PACKS
    assert len(ALL_PACKS) >= 3  # louisville + cincinnati + kansas_city paper packs


@pytest.mark.parametrize("path", ALL_PACKS, ids=lambda p: p.split(os.sep)[-2])
def test_pack_loads_with_expected_shapes(path):
    cfg = load_city_config(path)
    assert cfg.city.get("name")
    assert cfg.city.get("fiscal_year_start_month") in range(1, 13)
    assert cfg.title

    sources = cfg.expenditures.get("sources", [])
    assert sources, "every pack must declare at least one expenditure source"
    for src in sources:
        assert src.get("id")
        assert src.get("reader")

    for spec in cfg.canonicalization:
        assert spec.get("source_column") and spec.get("target_column")
    for spec in cfg.summaries:
        assert spec.get("table") and spec.get("sql")

    # legacy-shape accessors used by the engine and app
    assert isinstance(cfg.labels, dict)
    dd = cfg.data_dictionary
    assert isinstance(dd, dict)
    for entry in dd.values():
        assert "columns" in entry


def test_louisville_canonical_map_counts():
    cfg = load_city_config(LOUISVILLE)
    assert len(cfg.load_map("agency_map.csv")) == 97
    assert len(cfg.load_map("payee_map.csv")) == 30
    assert len(cfg.load_map("payee_prefix_map.csv")) == 4


def test_louisville_pack_matches_engine_expectations():
    cfg = load_city_config(LOUISVILLE)
    # the two Louisville eras with the schema break
    readers = [s["reader"] for s in cfg.expenditures["sources"]]
    assert readers == ["duckdb_union", "pandas_mapped"]
    # dictionary covers all seven curated tables
    assert len(cfg.dictionary) == 7
    assert "expenditures" in cfg.dictionary
    # all eight summary tables present
    assert len(cfg.summaries) == 8
    # app.py depends on these legacy shapes being non-empty for expenditures
    assert cfg.labels["expenditures"]
    assert cfg.data_dictionary["expenditures"]["columns"]


def test_city_config_env_override(monkeypatch):
    monkeypatch.setenv("CITY_CONFIG", LOUISVILLE)
    cfg = load_city_config()
    assert cfg.city["name"] == "Louisville"
