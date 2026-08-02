"""Canonical-map seeder tests.

The seeder writes files a curator then trusts, and a wrong merge silently
folds one organization's spend into another's — so these tests care much more
about what it REFUSES to merge than about how much it merges.
"""

import csv
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import canonical_seed as cs  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── normalization ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("LOUISVILLE GAS & ELECTRIC COMPANY", "Louisville Gas and Electric Co."),
    ("ACME SERVICES, INC.", "Acme Services Inc"),
    ("The Kroger Co", "KROGER"),
    ("A.B.C. Supply L.L.C.", "ABC SUPPLY LLC"),
    ("WALGREENS #4821", "Walgreens"),
    ("Dugan & Meyers LLC", "DUGAN AND MEYERS"),
])
def test_orthographic_variants_share_a_key(a, b):
    assert cs.normalize(a) == cs.normalize(b) != ""


@pytest.mark.parametrize("a,b", [
    # Different organizations that share a lot of surface text.
    ("BETHEL #2 APOSTOLIC CHURCH", "BETHEL #3 APOSTOLIC CHURCH"),
    ("LOUISVILLE METRO POLICE", "LOUISVILLE METRO PARKS"),
    ("FIRST NATIONAL BANK", "FIRST NATIONAL BANCORP"),
    # Truncation is NOT a safe automatic merge — it goes to the review report.
    ("LOUISVILLE GAS AND ELE", "LOUISVILLE GAS AND ELECTRIC"),
])
def test_distinct_entities_keep_distinct_keys(a, b):
    assert cs.normalize(a) != cs.normalize(b)


def test_a_bare_suffix_is_not_stripped_to_nothing():
    """Stripping is guarded so a payee literally named "CO" survives."""
    assert cs.normalize("CO") == "CO"
    assert cs.normalize("The Company") == "COMPANY"


def test_normalize_tolerates_junk():
    for junk in (None, "", "   ", "###", "!!!"):
        assert cs.normalize(junk) == ""


# ── canonical labels ─────────────────────────────────────────────────────────

def test_label_is_the_heaviest_spelling_not_the_first_seen():
    c = cs.Cluster("ACME", {"ACME INC": 10.0, "Acme Services": 500.0})
    assert c.canonical == "Acme Services"


def test_label_choice_is_deterministic_under_ties():
    """These files are committed and diffed; equal weights must not shuffle."""
    members = {"B SPELLING": 5.0, "A SPELLING": 5.0, "C SPELLING": 5.0}
    labels = {cs.Cluster("K", dict(members)).canonical for _ in range(20)}
    assert labels == {"C Spelling"}


@pytest.mark.parametrize("raw,expected", [
    ("DEPARTMENT OF PUBLIC WORKS", "Department of Public Works"),
    ("ACME SERVICES LLC", "Acme Services LLC"),   # acronym stays upper
    ("HUMANA INC", "Humana Inc"),                 # suffix reads as a word
    ("APCD", "APCD"),                             # unknown acronym preserved
    ("Already Cased Inc.", "Already Cased Inc."),  # mixed case left alone
])
def test_smart_title(raw, expected):
    assert cs.smart_title(raw) == expected


# ── clustering and draft rows ────────────────────────────────────────────────

def _clusters(weights):
    return cs.cluster_values(weights)


def test_clusters_are_ordered_by_weight():
    cl = _clusters({"SMALL CO": 1.0, "BIG CO": 100.0, "MID CO": 50.0})
    # the label is the raw winning spelling, not the stripped clustering key
    assert [c.canonical for c in cl] == ["Big Co", "Mid Co", "Small Co"]


def test_draft_rows_include_the_winners_own_identity_row():
    """The engine's exact map is a literal lookup, so the heaviest spelling
    needs its own row whenever the canonical label differs in case."""
    cl = _clusters({"ACME INC": 100.0, "ACME, INC.": 5.0})
    rows = cs.draft_rows(cl)
    assert ("ACME INC", "Acme Inc") in rows
    assert ("ACME, INC.", "Acme Inc") in rows


def test_singletons_are_not_written_by_default():
    """A lone spelling needs no map row; writing one buries the real merges."""
    rows = cs.draft_rows(_clusters({"SOLO VENDOR LLC": 10.0}))
    assert rows == []


def test_min_cluster_one_writes_recasings_but_never_no_op_rows():
    rows = cs.draft_rows(_clusters({"SOLO VENDOR LLC": 10.0}), min_cluster=1)
    assert rows == [("SOLO VENDOR LLC", "Solo Vendor LLC")]
    # a value already equal to its canonical form is still skipped
    assert cs.draft_rows(_clusters({"Solo Vendor LLC": 10.0}), min_cluster=1) == []


def test_draft_rows_are_a_function_of_the_map_the_engine_will_load():
    """Every source in the draft must be unique — the engine builds a CASE
    per row, and a duplicated source would produce an unreachable branch."""
    cl = _clusters({"ACME INC": 3.0, "acme inc": 2.0, "Acme, Inc.": 1.0})
    sources = [r[0] for r in cs.draft_rows(cl)]
    assert len(sources) == len(set(sources))


# ── suggestions (never auto-merged) ──────────────────────────────────────────

def test_truncations_surface_as_near_duplicates_but_stay_out_of_the_map():
    weights = {"LOUISVILLE GAS AND ELECTRIC": 100.0, "LOUISVILLE GAS AND ELE": 20.0}
    cl = _clusters(weights)
    assert len(cl) == 2, "a truncation must not be merged automatically"
    assert cs.draft_rows(cl) == []
    pairs = cs.near_duplicates(cl, threshold=0.86)
    assert len(pairs) == 1 and pairs[0][0] >= 0.86


def test_initialism_candidates_find_the_semantic_merges():
    """The half of curation normalization cannot reach (APCD/Air Pollution
    Control District) — the half that dominated Louisville's hand-built map."""
    cl = _clusters({"APCD": 100.0, "AIR POLLUTION CONTROL DISTRICT": 80.0})
    found = cs.initialism_candidates(cl)
    assert [(s.canonical, l.canonical) for s, l in found] == [
        ("APCD", "Air Pollution Control District")
    ]
    assert cs.draft_rows(cl) == [], "suggestions must not enter the draft map"


def test_initialism_skips_joiners_the_way_writers_do():
    cl = _clusters({"LGE": 10.0, "LOUISVILLE GAS AND ELECTRIC": 10.0})
    assert len(cs.initialism_candidates(cl)) == 1


def test_prefix_candidates_only_fire_on_trailing_store_codes():
    """A mid-name '#' is part of the identity; a prefix rule keyed on it would
    swallow every other congregation on the street."""
    cl = _clusters({
        "WALGREENS #4821": 10.0, "WALGREENS #17": 5.0,
        "BETHEL #2 APOSTOLIC CHURCH": 3.0,
    })
    prefixes = [p for p, _, _, _ in cs.prefix_candidates(cl)]
    assert prefixes == ["WALGREENS #"]


def test_near_duplicate_cap_is_reported_not_silent():
    """The comparison bound must be visible in the report, or a capped run
    reads as 'nothing left to merge'."""
    import io
    cl = _clusters({f"VENDOR NUMBER {i}": float(i) for i in range(50)})
    buf = io.StringIO()
    cs.write_report(buf, cl, [], [], [], [], cl[:5], "dollars", limit=10,
                    threshold=0.9)
    text = buf.getvalue()
    assert "Compared the 10 heaviest of 50 clusters" in text
    assert "Lighter clusters were not compared" in text


# ── the map the engine actually loads ────────────────────────────────────────

def test_written_map_round_trips_through_the_engines_loader(tmp_path):
    """The draft has to be loadable by CityConfig.load_map verbatim, including
    values holding commas and quotes."""
    from city_config import CityConfig
    cl = _clusters({'ACME, INC.': 10.0, '"ACME" INC': 4.0})
    assert len(cl) == 1, "fixture must actually co-cluster for this to test CSV"
    path = tmp_path / "payee_map.csv"
    cs.write_map(str(path), cs.draft_rows(cl))
    # the raw file really does carry the delimiter and quote characters
    raw = path.read_text()
    assert '"' in raw and "," in raw
    loaded = CityConfig({}, str(tmp_path)).load_map("payee_map.csv")
    assert loaded['ACME, INC.'] == "Acme, Inc."
    assert loaded['"ACME" INC'] == "Acme, Inc."


def test_seeder_output_survives_the_engines_sql_quoting(tmp_path):
    """Canonical labels reach DuckDB inside single-quoted SQL literals."""
    import data_model
    cl = _clusters({"O'BRIEN & SONS": 10.0, "O'Brien and Sons Inc": 4.0})
    rows = cs.draft_rows(cl)
    assert rows, "apostrophe names must still cluster"
    for src, canon in rows:
        assert "''" in data_model._sql_quote(src) or "'" not in src
        assert "''" in data_model._sql_quote(canon) or "'" not in canon


# ── CLI safety ───────────────────────────────────────────────────────────────

def test_cli_writes_a_draft_and_never_touches_a_curated_map(tmp_path, monkeypatch):
    """Re-running the seeder on an onboarded city must not clobber the map a
    human spent hours on."""
    import shutil
    pack = tmp_path / "pack"
    pack.mkdir()
    shutil.copy(os.path.join(REPO, "cities", "louisville", "city.yaml"), pack)
    curated = pack / "agency_map.csv"
    curated.write_text("source,canonical\nHAND,Hand Curated\n")
    for name in ("payee_map.csv", "payee_prefix_map.csv"):
        shutil.copy(os.path.join(REPO, "cities", "louisville", name), pack)

    monkeypatch.setattr(cs, "load_weights",
                        lambda *a, **k: ({"ACME INC": 9.0, "Acme, Inc.": 1.0}, "dollars"))
    cs.main(["--city", str(pack / "city.yaml"), "--dimension", "agency"])

    assert curated.read_text() == "source,canonical\nHAND,Hand Curated\n"
    draft = pack / "agency_map.csv.draft.csv"
    with open(draft) as f:
        assert next(csv.reader(f)) == ["source", "canonical"]


def test_cli_rejects_a_dimension_the_pack_does_not_declare(tmp_path):
    from city_config import load_city_config
    cfg = load_city_config(os.path.join(REPO, "cities", "louisville", "city.yaml"))
    with pytest.raises(SystemExit):
        cs.spec_for(cfg, "not_a_dimension")
    assert cs.spec_for(cfg, "payee")["source_column"] == "payee"
