"""Year-coverage derivation and city-pack fact resolution.

Both were previously inline in the FastAPI startup handler / brand new and
untested, and the partial-vs-complete flip is the difference between the bot
saying "use 2025 for current spending" and presenting a half-loaded year as a
full one. Every branch is pinned here.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from city_config import CityConfig  # noqa: E402
from data_model import derive_year_facts, fiscal_year_end  # noqa: E402


# ── fiscal_year_end ──────────────────────────────────────────────────────────

def test_fiscal_year_end_july_start():
    assert fiscal_year_end(2026, 7).isoformat() == "2026-06-30"


def test_fiscal_year_end_calendar_year():
    assert fiscal_year_end(2026, 1).isoformat() == "2026-12-31"


# ── derive_year_facts ────────────────────────────────────────────────────────

def test_partial_when_coverage_stops_mid_year():
    yf = derive_year_facts(2026, 7, "2026-03-16")
    assert yf["is_partial"] is True
    assert yf["last_complete_year"] == 2025
    assert yf["in_progress_year"] == 2026
    assert "INCOMPLETE" in yf["rules"] and "2026-03-16" in yf["rules"]
    assert "partial" in yf["fact"] and "2025 is the most recent year with complete data" in yf["fact"]


def test_complete_when_coverage_reaches_year_end():
    yf = derive_year_facts(2026, 7, "2026-06-30")
    assert yf["is_partial"] is False
    assert yf["last_complete_year"] == 2026
    assert yf["in_progress_year"] is None
    assert "complete" in yf["rules"] and "INCOMPLETE" not in yf["rules"]


def test_complete_when_last_business_day_precedes_year_end():
    # FY2018 ends 2018-06-30, a Saturday; the last payment is 2018-06-29.
    # Requiring the exact last calendar day would misclassify such years.
    assert derive_year_facts(2018, 7, "2018-06-29")["is_partial"] is False
    # FY2019: June 30 was a Sunday, last payment 2019-06-28
    assert derive_year_facts(2019, 7, "2019-06-28")["is_partial"] is False


def test_still_partial_just_outside_the_grace_window():
    # ~5 weeks short of year end is genuinely incomplete
    assert derive_year_facts(2026, 7, "2026-05-20")["is_partial"] is True


def test_null_coverage_fails_safe_to_partial():
    # A NULL max date must NOT be stringified into "None" and compared
    # lexically (which read as complete and asserted a false claim).
    for bad in (None, "None", "", "not-a-date"):
        yf = derive_year_facts(2026, 7, bad)
        assert yf["is_partial"] is True, f"{bad!r} must fail safe to partial"
        assert yf["covered_through"] is None
        assert "loaded through" not in yf["fact"]  # no bogus date in the prose


def test_datetime_like_coverage_is_accepted():
    yf = derive_year_facts(2026, 7, "2026-06-30 00:00:00")
    assert yf["is_partial"] is False


# ── CityConfig.data_facts_for ────────────────────────────────────────────────

def _cfg(facts):
    return CityConfig({"data_facts": facts}, ".")


def test_data_facts_resolves_placeholders():
    out = _cfg(["Year {last_complete_year} is complete."]).data_facts_for({"last_complete_year": 2025})
    assert out == ["Year 2025 is complete."]


def test_data_facts_drops_unresolved_placeholders():
    out = _cfg(["Static fact.", "Year {last_complete_year} is complete."]).data_facts_for()
    assert out == ["Static fact."]


def test_data_facts_drops_when_value_is_none():
    out = _cfg(["Year {in_progress_year} is partial."]).data_facts_for({"in_progress_year": None})
    assert out == []


def test_data_facts_keeps_ordinary_prose_braces():
    # Only documented placeholder names can disqualify a fact; other braces
    # are prose and must survive (dropping load-bearing text is worse than
    # passing a literal brace through).
    text = "Amounts in {USD} and sets like {1, 2}."
    assert _cfg([text]).data_facts_for({}) == [text]


def test_data_facts_empty_config():
    assert _cfg([]).data_facts_for({"x": 1}) == []
