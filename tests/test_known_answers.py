"""
Known-answer test suite for Louisville expenditure bot.

These pin down values and invariants that should hold for the loaded dataset.
When the underlying data is refreshed, exact-value assertions (counts, named
top entries) may legitimately change and should be updated to match; the
invariant-style assertions (ranges, "no nulls", subset relationships) are meant
to survive routine refreshes.

Run: python -m pytest tests/test_known_answers.py -v
"""

import pandas as pd
import pytest
from data_model import get_compact_schema_description, infer_chart, load_all_data


@pytest.fixture(scope="module")
def con():
    return load_all_data("data")


# ── Agency spend ──────────────────────────────────────────────────────────────

def test_top_agency_is_public_works(con):
    r = con.execute("SELECT agency FROM summary_agency_spend LIMIT 1").fetchone()
    assert r[0] == "Public Works & Assets"


def test_top_agency_spend_over_1b(con):
    r = con.execute("SELECT total_spend FROM summary_agency_spend LIMIT 1").fetchone()
    assert r[0] > 1_000_000_000


# ── Annual spend ──────────────────────────────────────────────────────────────

def test_19_fiscal_years(con):
    r = con.execute("SELECT COUNT(*) FROM summary_annual_spend").fetchone()
    assert r[0] == 19  # FY2008 through FY2026 inclusive


def test_annual_spend_range(con):
    r = con.execute("SELECT MIN(total_spend), MAX(total_spend) FROM summary_annual_spend").fetchone()
    assert r[0] > 200_000_000  # lowest year > $200M
    assert r[1] < 700_000_000  # highest year < $700M


def test_peak_spending_year_is_2025(con):
    r = con.execute("SELECT fiscal_year FROM summary_annual_spend ORDER BY total_spend DESC LIMIT 1").fetchone()
    assert r[0] == 2025


def test_2026_is_partial_year(con):
    """FY2026 is still in progress, so its total must trail the last complete year."""
    rows = dict(con.execute(
        "SELECT fiscal_year, total_spend FROM summary_annual_spend WHERE fiscal_year IN (2025, 2026)"
    ).fetchall())
    assert rows[2026] < rows[2025]


# ── Largest payments / data artifacts ─────────────────────────────────────────

def test_largest_payment_not_susteen(con):
    """SUSTEEN's $224M entry is a data artifact and must not surface in largest payments."""
    rows = con.execute("SELECT payee FROM summary_largest_payments LIMIT 50").fetchall()
    assert all("SUSTEEN" not in (p[0] or "").upper() for p in rows)


def test_largest_payment_is_arena_authority(con):
    r = con.execute("SELECT payee, invoice_amount FROM summary_largest_payments LIMIT 1").fetchone()
    assert r[0] == "Louisville Arena Authority Inc"  # canonicalized (was uppercase pre-canonicalization)
    assert r[1] == 12_000_000.00


def test_data_artifacts_flagged(con):
    r = con.execute("SELECT COUNT(*) FROM expenditures WHERE is_data_artifact = TRUE").fetchone()
    assert r[0] == 2  # the SUSTEEN pair


def test_offsetting_rows_flagged(con):
    """Offsetting rows are flagged, and the artifact rows are a subset of them."""
    offsetting = con.execute("SELECT COUNT(*) FROM expenditures WHERE is_offsetting = TRUE").fetchone()[0]
    artifacts = con.execute("SELECT COUNT(*) FROM expenditures WHERE is_data_artifact = TRUE").fetchone()[0]
    assert offsetting > 0
    assert offsetting >= artifacts


# ── Salaries ──────────────────────────────────────────────────────────────────

def test_top_salary_is_police_chief(con):
    r = con.execute("SELECT job_title FROM summary_top_salaries LIMIT 1").fetchone()
    assert r[0] == "Police Chief"


# ── Expenditure types ─────────────────────────────────────────────────────────

def test_expenditure_types_exist(con):
    r = con.execute("SELECT DISTINCT expenditure_type FROM summary_expenditure_type").fetchall()
    types = {row[0] for row in r}
    assert "Operating" in types or "Metro Government Operations" in types


# ── Canonicalization ──────────────────────────────────────────────────────────

def test_canonical_columns_exist(con):
    cols = {c[0] for c in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'expenditures'"
    ).fetchall()}
    assert {"agency_canonical", "payee_canonical"}.issubset(cols)


def test_agency_normalization_no_duplicates(con):
    """Public Works should be one canonical entry, not two."""
    r = con.execute("SELECT COUNT(*) FROM summary_agency_spend WHERE agency LIKE '%Public Works%'").fetchone()
    assert r[0] == 1


def test_agency_canonicalization_collapses_variants(con):
    """Canonicalization must collapse the raw agency-name variants into fewer names."""
    r = con.execute(
        "SELECT COUNT(DISTINCT agency_canonical), COUNT(DISTINCT agency) FROM expenditures"
    ).fetchone()
    assert r[0] < r[1]


def test_payee_canonicalization_lge(con):
    """LG&E / Louisville Gas & Electric variants map to a single canonical name."""
    rows = con.execute(
        "SELECT DISTINCT payee_canonical FROM expenditures "
        "WHERE UPPER(payee) LIKE '%LG&E%' OR UPPER(payee) LIKE 'LOUISVILLE GAS%'"
    ).fetchall()
    assert [r[0] for r in rows] == ["Louisville Gas & Electric Company"]


def test_payee_canonicalization_cdw(con):
    """All CDW GOVT variants map to CDW LLC."""
    rows = con.execute(
        "SELECT DISTINCT payee_canonical FROM expenditures WHERE UPPER(payee) LIKE 'CDW%'"
    ).fetchall()
    assert [r[0] for r in rows] == ["CDW LLC"]


# ── Contractors ───────────────────────────────────────────────────────────────

def test_top_contractors_have_agents_and_spend(con):
    """summary_top_contractors is pre-filtered to rows with a registered agent and a spend total."""
    r = con.execute(
        "SELECT COUNT(*) FROM summary_top_contractors "
        "WHERE sos_registered_agent IS NULL OR total_spend IS NULL"
    ).fetchone()
    assert r[0] == 0


def test_top_contractor_is_lge(con):
    r = con.execute(
        "SELECT payee FROM summary_top_contractors ORDER BY total_spend DESC LIMIT 1"
    ).fetchone()
    assert r[0] == "Louisville Gas & Electric Company"


# ── Compact schema (feeds the LLM system prompt) ──────────────────────────────

def test_compact_schema_excludes_internal_tables(con):
    schema = get_compact_schema_description(con)
    # Internal helper tables are present in the DB but must not reach the prompt.
    assert "## _payee_to_canonical" not in schema
    assert "## _payee_canonical_totals" not in schema
    # Real tables are present.
    assert "## expenditures" in schema
    assert "## summary_agency_spend" in schema


def test_compact_schema_enumerates_only_low_cardinality(con):
    schema = get_compact_schema_description(con)
    # Low-cardinality categorical: values listed inline as an enum {...}.
    # (Trailing space avoids matching the separate `expenditure_types` column.)
    line = next((ln for ln in schema.splitlines() if ln.startswith("- expenditure_type ")), "")
    assert "{" in line and "Operating" in line
    # High-cardinality entity columns must NOT be enumerated (would bloat + leak).
    payee_line = next((ln for ln in schema.splitlines() if ln.startswith("- payee_canonical ")), "")
    assert "{" not in payee_line


def test_compact_schema_smaller_than_full(con):
    from data_model import get_full_schema_description
    assert len(get_compact_schema_description(con)) < len(get_full_schema_description(con))


# ── Chart inference (infer_chart) ─────────────────────────────────────────────
# Pure-function tests pinning the axis/type selection against regression.

def test_chart_constant_year_topn_is_not_a_line():
    """The bug: a 'top 5 in 2025' result carries a constant fiscal_year column.
    It must chart the entity that varies, not draw a line across identical years."""
    df = pd.DataFrame({
        "fiscal_year": [2025] * 5,
        "agency": list("ABCDE"),
        "total_extended_spend": [8.3, 4.5, 2.9, 1.9, 1.4],
    })
    chart_type, label_col, value_col = infer_chart(df)
    assert label_col == "agency"                 # varying dimension, not constant year
    assert value_col == "total_extended_spend"
    assert chart_type == "pie"                   # 5 slices, not a line


def test_chart_annual_trend_is_a_line():
    df = pd.DataFrame({"fiscal_year": list(range(2008, 2027)),
                       "total_spend": [float(i) for i in range(19)]})
    chart_type, label_col, value_col = infer_chart(df)
    assert (chart_type, label_col, value_col) == ("line", "fiscal_year", "total_spend")


def test_chart_numeric_month_is_a_line_but_month_names_are_not():
    nums = pd.DataFrame({"month": list(range(1, 13)), "total": [float(i) for i in range(12)]})
    assert infer_chart(nums)[0] == "line"
    names = pd.DataFrame({"month": ["April", "August", "December", "February"],
                          "total": [4.0, 3.0, 2.0, 1.0]})
    assert infer_chart(names)[0] != "line"       # lexicographic sort would mislead


def test_chart_prefers_dollar_measure_over_count():
    """SELECT payee, SUM(amount) AS total, COUNT(*) AS num_invoices -> chart dollars."""
    df = pd.DataFrame({
        "payee": list("ABCDE"),
        "total": [9.0, 7.0, 5.0, 3.0, 1.0],   # float measure
        "num_invoices": [50, 40, 30, 20, 10],  # int count
    })
    assert infer_chart(df)[2] == "total"


def test_chart_handles_nullable_and_string_dtypes():
    """Int32 measure and pandas string-dtype dimension should still chart."""
    df = pd.DataFrame({
        "category": pd.array(["A", "B", "C", "D", "E", "F"], dtype="string"),
        "spend": pd.array([6, 5, 4, 3, 2, 1], dtype="Int32"),
    })
    chart_type, label_col, value_col = infer_chart(df)
    assert label_col == "category" and value_col == "spend" and chart_type == "bar"


# ── Volume ────────────────────────────────────────────────────────────────────

def test_total_expenditure_rows(con):
    r = con.execute("SELECT COUNT(*) FROM expenditures").fetchone()
    assert r[0] > 2_200_000
