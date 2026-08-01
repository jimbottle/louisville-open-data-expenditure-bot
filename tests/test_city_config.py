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
    # dictionary covers all seven curated source tables (summary-table entries
    # may be added on top, so no exact-count pin)
    source_tables = {
        "expenditures", "salary_data", "capital_projects", "active_contractors",
        "staff_demographics", "hr_requisitions", "contractor_profiles",
    }
    assert source_tables <= set(cfg.dictionary)
    # all eight summary tables present
    assert len(cfg.summaries) == 8
    # app.py depends on these legacy shapes being non-empty for expenditures
    assert cfg.labels["expenditures"]
    assert cfg.data_dictionary["expenditures"]["columns"]


def test_city_config_env_override(monkeypatch):
    # Must point at a NON-default pack, or a broken env lookup would fall
    # through to the Louisville default and pass anyway. Loading a paper pack
    # through CityConfig is safe — only load_all_data needs a real reader.
    monkeypatch.setenv(
        "CITY_CONFIG", os.path.join(REPO, "cities", "cincinnati", "city.yaml")
    )
    cfg = load_city_config()
    assert cfg.city["name"] == "Cincinnati"


def test_branding_is_exposed_and_complete():
    """The frontend gets its identity from the pack, so a second city's
    deployment never says Louisville."""
    cfg = load_city_config(LOUISVILLE)
    b = cfg.branding
    assert b.get("bot_name") and b.get("tab_title") and b.get("subtitle")
    groups = b.get("starter_groups") or []
    assert len(groups) >= 3
    for g in groups:
        assert g.get("label")
        assert g.get("chips")
        for chip in g["chips"]:
            assert len(chip) == 2, "each chip is [button label, question]"
            assert all(isinstance(x, str) and x.strip() for x in chip)


def test_branding_starter_questions_match_the_warm_cache_list():
    """warm_cache pre-answers the starter questions; if the chips drift from
    that list the UI offers questions that were never warmed."""
    import warm_cache
    cfg = load_city_config(LOUISVILLE)
    chip_qs = {c[1] for g in cfg.branding["starter_groups"] for c in g["chips"]}
    assert chip_qs <= set(warm_cache.STARTER_QUESTIONS), (
        f"chips not in warm list: {chip_qs - set(warm_cache.STARTER_QUESTIONS)}"
    )
