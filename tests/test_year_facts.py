"""Year-coverage derivation and city-pack fact resolution.

Both were previously inline in the FastAPI startup handler / brand new and
untested, and the partial-vs-complete flip is the difference between the bot
saying "use 2025 for current spending" and presenting a half-loaded year as a
full one. Every branch is pinned here.
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from city_config import CityConfig  # noqa: E402
from data_model import (  # noqa: E402
    derive_salary_year_facts,
    derive_year_facts,
    fiscal_year_end,
    year_context,
)


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
    assert "partial" in yf["fact"]
    assert "FY2025 is the most recent fiscal year with complete expenditure data" in yf["fact"]


def test_complete_when_coverage_reaches_year_end():
    yf = derive_year_facts(2026, 7, "2026-06-30", today=date(2026, 8, 1))
    assert yf["is_partial"] is False
    assert yf["last_complete_year"] == 2026
    assert yf["in_progress_year"] is None
    assert "complete" in yf["rules"] and "INCOMPLETE" not in yf["rules"]


def test_complete_when_last_business_day_precedes_year_end():
    # FY2018 ends 2018-06-30, a Saturday; the last payment is 2018-06-29.
    # Requiring the exact last calendar day would misclassify such years.
    assert derive_year_facts(2018, 7, "2018-06-29", today=date(2018, 9, 1))["is_partial"] is False
    # FY2019: June 30 was a Sunday, last payment 2019-06-28
    assert derive_year_facts(2019, 7, "2019-06-28", today=date(2019, 9, 1))["is_partial"] is False


def test_expenditure_rule_and_fact_claim_nothing_about_salaries():
    # Coverage is measured on payments only, so BOTH the rule (SQL prompt) and
    # the fact (interpret prompt) must scope themselves to spending — a
    # complete fiscal year must never vouch for salary_data.
    for yf in (derive_year_facts(2026, 7, "2026-03-16", today=date(2026, 8, 1)),
               derive_year_facts(2026, 7, "2026-06-30", today=date(2026, 8, 1))):
        for text in (yf["rules"], yf["fact"]):
            assert "CalYear" not in text
            assert "salar" not in text.lower()
            assert "calendar" not in text.lower()


# ── derive_salary_year_facts ─────────────────────────────────────────────────

def test_salary_newest_year_is_always_treated_as_partial():
    # The newest CalYear is a YTD snapshot; the calendar rolling over does not
    # make a stale snapshot complete. The complete year cited must be one that
    # is actually loaded (passed in), never an assumed MAX-1.
    s = derive_salary_year_facts(2026, prior_cal_year=2025)
    assert s["is_partial"] is True
    assert s["last_complete_year"] == 2025
    assert "CalYear = 2025" in s["rules"] and "YEAR-TO-DATE" in s["rules"]
    assert "year-to-date" in s["fact"].lower()


def test_salary_cites_the_loaded_prior_year_not_a_computed_one():
    # A gap in the data (no 2029) must cite 2028, not 2029.
    s = derive_salary_year_facts(2030, prior_cal_year=2028)
    assert s["last_complete_year"] == 2028
    assert "CalYear = 2028" in s["rules"]


def test_salary_facts_omitted_when_no_prior_year_is_loaded():
    # Pointing at a year the table lacks would zero-row every salary question.
    assert derive_salary_year_facts(2026, prior_cal_year=None) is None


def test_grace_window_boundary_is_pinned():
    # Exactly at the 7-day cutoff -> complete; one day earlier -> partial.
    # (A vaguer fixture would pass for any window from 1 to 40 days.)
    after = date(2026, 8, 1)
    assert derive_year_facts(2026, 7, "2026-06-23", today=after)["is_partial"] is False
    assert derive_year_facts(2026, 7, "2026-06-22", today=after)["is_partial"] is True


def test_running_fiscal_year_is_never_promoted_by_the_grace_window():
    # Late in a RUNNING fiscal year, coverage within the grace window must not
    # declare the year complete — five days of spending are still to come.
    during = date(2026, 6, 25)
    assert derive_year_facts(2026, 7, "2026-06-24", today=during)["is_partial"] is True


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


# ── year_context (the shared derivation both entry points call) ──────────────

def _con(fiscal_rows=(("2026-03-16", 2026), ("2025-06-30", 2025)), salary_years=(2026, 2025)):
    import duckdb
    con = duckdb.connect()
    con.execute("CREATE TABLE expenditures (payment_date VARCHAR, fiscal_year INTEGER)")
    if fiscal_rows:
        con.executemany("INSERT INTO expenditures VALUES (?, ?)", list(fiscal_rows))
    if salary_years is not None:
        con.execute("CREATE TABLE salary_data (CalYear INTEGER)")
        con.executemany("INSERT INTO salary_data VALUES (?)", [(y,) for y in salary_years])
    return con


def test_year_context_builds_values_rules_and_facts():
    yc = year_context(_con(), fy_start_month=7, today=date(2026, 8, 1))
    assert yc["values"] == {
        "first_year": 2025, "newest_year": 2026,
        "in_progress_year": 2026, "last_complete_year": 2025,
    }
    # both a spending rule and a salary rule, derived independently
    assert "FY2026 data is INCOMPLETE" in yc["rules"]
    assert "CalYear 2026 in salary_data" in yc["rules"]
    # both facts reach the interpret prompt
    assert len(yc["facts"]) == 2
    assert any("year-to-date" in f.lower() for f in yc["facts"])


def test_year_context_without_salary_table_is_quiet():
    yc = year_context(_con(salary_years=None), fy_start_month=7, today=date(2026, 8, 1))
    assert yc["salary"] is None
    # no table at all is a distinct state from "table with too few years"
    assert yc["newest_cal_year"] is None
    assert "CalYear" not in yc["rules"]
    assert len(yc["facts"]) == 1  # expenditure fact only


def test_year_context_omits_salary_guidance_for_a_single_calyear():
    yc = year_context(_con(salary_years=(2026,)), fy_start_month=7, today=date(2026, 8, 1))
    assert yc["salary"] is None
    # ...but the table exists, which the log line must distinguish
    assert yc["newest_cal_year"] == 2026
    assert "CalYear" not in yc["rules"]
    assert len(yc["facts"]) == 1  # expenditure fact only


def test_year_context_handles_no_usable_fiscal_years():
    # Empty table must not TypeError on newest_year - 1; callers get nothing.
    yc = year_context(_con(fiscal_rows=()), fy_start_month=7)
    assert yc["values"] == {} and yc["rules"] == "" and yc["facts"] == []


def test_year_context_reports_a_failed_salary_derivation_distinctly():
    # A salary_data table that exists but whose CalYear can't be used must be
    # reported as its own state — not as "no salary table" (it exists) and not
    # as the single-year case (the derivation never got that far).
    import duckdb
    con = duckdb.connect()
    con.execute("CREATE TABLE expenditures (payment_date VARCHAR, fiscal_year INTEGER)")
    con.execute("INSERT INTO expenditures VALUES ('2026-03-16', 2026), ('2025-06-30', 2025)")
    # renamed column: the table exists but CalYear does not (BinderException)
    con.execute("CREATE TABLE salary_data (calendar_year INTEGER)")
    con.execute("INSERT INTO salary_data VALUES (2026)")
    yc = year_context(con, fy_start_month=7, today=date(2026, 8, 1))
    assert yc["salary"] is None
    assert yc["salary_error"] is True
    assert yc["newest_cal_year"] is None
    assert "CalYear" not in yc["rules"]      # no salary guidance emitted
    assert len(yc["facts"]) == 1             # expenditure fact only


def test_year_context_healthy_paths_are_not_flagged_as_errors():
    assert year_context(_con(), fy_start_month=7, today=date(2026, 8, 1))["salary_error"] is False
    assert year_context(_con(salary_years=None), fy_start_month=7)["salary_error"] is False
    assert year_context(_con(salary_years=(2026,)), fy_start_month=7)["salary_error"] is False


def test_year_context_error_after_first_query_clears_everything():
    # A failure in the SECOND query (newest year read fine, prior-year query
    # blows up) must still land cleanly in the error state — no populated
    # salary rule, no lingering newest_cal presented as the single-year case.
    import duckdb

    class _FailsOnPriorYear:
        """Delegates to a real connection but fails the prior-year query."""
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            if "CalYear <" in sql:
                raise duckdb.BinderException("simulated failure after first read")
            return self._inner.execute(sql, *args, **kwargs)

    yc = year_context(_FailsOnPriorYear(_con()), fy_start_month=7, today=date(2026, 8, 1))
    assert yc["salary_error"] is True
    assert yc["salary"] is None
    assert yc["newest_cal_year"] is None
    assert "CalYear" not in yc["rules"]   # no half-applied salary rule
    assert len(yc["facts"]) == 1
