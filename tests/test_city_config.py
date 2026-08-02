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


def test_frontend_city_literals_live_only_in_the_fallback_defaults():
    """A second city's deployment must not show Louisville text. Every
    Louisville mention left in the page has to sit inside the clearly-marked
    DEFAULT_STARTER_GROUPS fallback (which /api/config overrides)."""
    html = open(os.path.join(REPO, "static", "index.html")).read()
    start = html.index("const DEFAULT_STARTER_GROUPS")
    end = html.index("let STARTER_GROUPS = DEFAULT_STARTER_GROUPS;")
    outside = html[:start] + html[end:]
    # Case-insensitive: lowercase leaks (data.louisvilleky.gov,
    # louisville.raylytics.io) are exactly the class of literal that just
    # moved out of the About block.
    stray = [ln.strip() for ln in outside.splitlines()
             if "louisville" in ln.lower() or "lmpd" in ln.lower()]
    # Only nodes applyBranding() actually rewrites may keep Louisville text as
    # the pre-fetch first paint. The About block used to be whitelisted here
    # without being overridden anywhere — six lines of Louisville attribution,
    # license and disclaimer that a Cincinnati deploy would have rendered.
    # Every token here must correspond to a node applyBranding() rewrites AND
    # actually appear on a line carrying a city literal — "<h1>Lou<" did not
    # (the h1 is just "Lou"), so it read as covering the header while covering
    # nothing.
    overridden = ("<title>Lou", 'class="subtitle"', "<h2>Ask me about",
                  "<p>Natural language", 'id="question"')
    unexpected = [ln for ln in stray if not any(tok in ln for tok in overridden)]
    assert not unexpected, f"city literals outside branding control: {unexpected}"


def test_branding_covers_every_overridable_frontend_string():
    cfg = load_city_config(LOUISVILLE)
    for key in ("bot_name", "tab_title", "subtitle", "hero_heading", "hero_blurb",
                "input_placeholder", "input_aria_label", "about_html", "starter_groups"):
        assert cfg.branding.get(key), f"branding missing {key}"


def test_default_starter_groups_match_the_pack():
    """The chips exist twice — in the pack and as the JS fallback. Labels,
    questions AND order must agree, or a failed /api/config would render
    different buttons (and possibly un-warmed questions)."""
    import re
    cfg = load_city_config(LOUISVILLE)
    html = open(os.path.join(REPO, "static", "index.html")).read()
    block = html[html.index("const DEFAULT_STARTER_GROUPS"):
                 html.index("let STARTER_GROUPS = DEFAULT_STARTER_GROUPS;")]
    js_pairs = re.findall(r"\['([^']*)',\s*'([^']*)'\]", block)
    pack_pairs = [tuple(c) for g in cfg.branding["starter_groups"] for c in g["chips"]]
    # Count first, so a regex that failed to match reads as a regex problem
    # rather than as chip drift.
    assert len(js_pairs) == len(pack_pairs), (
        f"extracted {len(js_pairs)} JS chips vs {len(pack_pairs)} in the pack — "
        "if the counts differ the pair regex may simply have missed a chip "
        "(e.g. an apostrophe in the text)"
    )
    assert js_pairs == pack_pairs, "fallback chips drift from the pack (label, question or order)"


# ── GET /api/config (the contract applyBranding() depends on verbatim) ───────

FRONTEND_KEYS = ("bot_name", "tab_title", "subtitle", "hero_heading", "hero_blurb",
                 "input_placeholder", "input_aria_label", "about_html", "starter_groups")


def _get_config():
    """GET /api/config through the real router.

    Calling the handler directly left the route path, its registration and
    JSON serialization untested — renaming the decorator's path would break
    the frontend with the suite green."""
    from fastapi.testclient import TestClient
    import app
    resp = TestClient(app.app).get("/api/config")
    assert resp.status_code == 200
    return resp.json()


def test_api_config_returns_every_key_the_frontend_reads():
    cfg = _get_config()
    for key in FRONTEND_KEYS:
        assert key in cfg, f"/api/config omits {key}, which applyBranding() reads"
    for group in cfg["starter_groups"]:
        assert isinstance(group.get("chips"), list)
        for chip in group["chips"]:
            assert len(chip) == 2, "each chip must stay [label, question]"


def test_a_packs_own_branding_survives_the_defaulting():
    """The endpoint's whole contract is "pack wins, default fills". Only the
    fallback branch was covered, so changing any setdefault to a plain
    assignment would overwrite Louisville's About text, hero and subtitle with
    the generic copy and the suite would stay green."""
    pack = load_city_config(LOUISVILLE).branding
    cfg = _get_config()
    declared = [k for k in FRONTEND_KEYS if k in pack]
    assert len(declared) == len(FRONTEND_KEYS), "fixture pack no longer declares every key"
    for key in declared:
        assert cfg[key] == pack[key], f"/api/config overrode the pack's {key}"


def test_api_config_defaults_use_the_packs_own_city(monkeypatch):
    """A pack with no branding section must render ITS OWN neutral copy —
    never another city's identity or an empty About box."""
    import app
    from city_config import CityConfig
    monkeypatch.setattr(
        app, "CONFIG", CityConfig({"city": {"name": "Cincinnati"}}, "."),
    )
    cfg = _get_config()
    for key in FRONTEND_KEYS:
        assert key in cfg
    assert cfg["bot_name"] == "Cincinnati"
    assert "Cincinnati" in cfg["hero_heading"] and "Louisville" not in cfg["hero_heading"]
    assert "Cincinnati" in cfg["input_placeholder"]
    # the About box must never come back empty — it carries the disclaimer
    assert "not" in cfg["about_html"] and "affiliated" in cfg["about_html"]
    assert "Louisville" not in cfg["about_html"]
    assert cfg["starter_groups"] == []


def test_api_config_handles_a_pack_with_no_city_name(monkeypatch):
    import app
    from city_config import CityConfig
    monkeypatch.setattr(app, "CONFIG", CityConfig({}, "."))
    cfg = _get_config()
    assert cfg["bot_name"] == "Open Data Bot"
    assert cfg["subtitle"] == ""          # suppressed, not a dangling sentence
    assert cfg["about_html"]              # disclaimer still present
    # one name for one unknown city: the tab must not read "City Open Data"
    # while the header reads "Open Data Bot"
    assert cfg["tab_title"] == cfg["bot_name"]
