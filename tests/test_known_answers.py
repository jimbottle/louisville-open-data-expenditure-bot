"""
Known-answer test suite for Louisville expenditure bot.

These pin down values and invariants that should hold for the loaded dataset.
When the underlying data is refreshed, exact-value assertions (counts, named
top entries) may legitimately change and should be updated to match; the
invariant-style assertions (ranges, "no nulls", subset relationships) are meant
to survive routine refreshes.

Run: python -m pytest tests/test_known_answers.py -v
"""

import os

import pandas as pd
import pytest
from data_model import (
    drop_total_rows,
    get_compact_schema_description,
    infer_chart,
    load_all_data,
)


@pytest.fixture(scope="module")
def con():
    return load_all_data("data")


def _app_source() -> str:
    """app.py's text, read without leaking the handle.

    Several tests pin prompt fragments against the served source; importing
    app instead would create the log dir and mount StaticFiles at module
    scope, coupling those tests to the working directory for no benefit."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
    with open(path) as f:
        return f.read()


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


def test_summary_top_salaries_year_matches_the_prompt_rule(con):
    """The pack SQL and the prompt's CalYear rule must derive the same year.
    They once used different derivations (MAX-1 vs. highest loaded prior
    year), which agree only when CalYears are contiguous — a gap would leave
    the model querying a year the materialized table never covered."""
    from data_model import year_context
    yc = year_context(con, fy_start_month=7)
    assert yc["salary"] is not None, "Louisville data should yield salary guidance"
    table_year = con.execute(
        "SELECT DISTINCT calendar_year FROM summary_top_salaries"
    ).fetchall()
    assert len(table_year) == 1
    assert table_year[0][0] == yc["salary"]["last_complete_year"]


def test_top_salaries_magnitudes_and_scope(con):
    """Regression for the 'highest paid positions' answer (louisville-open-data-kxm):
    the summary must cover ONLY the latest complete year, count distinct people,
    and carry dollar magnitudes in the low hundreds of thousands (the LLM once
    rendered these as millions — the deterministic layer must not feed that)."""
    rows = con.execute("""
        SELECT calendar_year, avg_total_comp, max_total_comp, employee_count
        FROM summary_top_salaries ORDER BY avg_total_comp DESC LIMIT 10
    """).fetchall()
    # Derive the expected year the same way the pack SQL and year_context do
    # (highest CalYear actually LOADED below the newest) — a computed MAX-1
    # would re-pin the assumption those two deliberately dropped.
    expected_year = con.execute(
        "SELECT MAX(CalYear) FROM salary_data "
        "WHERE CalYear < (SELECT MAX(CalYear) FROM salary_data)"
    ).fetchone()[0]
    # Guard against a vacuous pass: with a single loaded CalYear the summary
    # materializes empty and every assertion below would silently skip.
    assert expected_year is not None, "no complete CalYear loaded to summarize"
    assert rows, "summary_top_salaries is empty"
    for year, avg_comp, max_comp, n in rows:
        assert year == expected_year, "must use the latest COMPLETE year, not the partial one"
        assert 100_000 < avg_comp < 500_000, f"top-10 avg comp {avg_comp} outside plausible $K range"
        assert avg_comp <= max_comp
        assert n >= 1
    # Police Chief is a single position: distinct-person count must be tiny,
    # not a person-year rollup across years. MAX across departments (the title
    # could theoretically appear in more than one) and assert presence first
    # so an absent row fails meaningfully instead of raising TypeError.
    row = con.execute(
        "SELECT MAX(employee_count) FROM summary_top_salaries WHERE job_title = 'Police Chief'"
    ).fetchone()
    assert row is not None and row[0] is not None, "Police Chief missing from summary_top_salaries"
    assert row[0] <= 3


# ── Chart total-row filtering ────────────────────────────────────────────────
# A grand-total bar equals every other bar combined, doubling the axis. Real
# payees are named "TOTAL ..." though, so both directions are pinned here.

def _chart_df(pairs):
    return pd.DataFrame({"label": [p[0] for p in pairs], "value": [p[1] for p in pairs]})


@pytest.mark.parametrize("label", [
    "TOTAL - ALL GRANT FUNDS",   # the ROLLUP label the grant prompt mandates
    "Total - Everything",        # lowercase / different suffix
    "TOTAL",                     # bare total
    "GRAND TOTAL",
    "TOTAL ALL YEARS",
])
def test_drop_total_rows_removes_total_labels(label):
    df = _chart_df([(label, 100.0), ("Alpha", 60.0), ("Beta", 40.0)])
    out = drop_total_rows(df, "label", "value")
    assert label not in out["label"].tolist()
    assert out["label"].tolist() == ["Alpha", "Beta"]


@pytest.mark.parametrize("label", ["TOTALS", "SUBTOTAL", "TOTAL_SPEND"])
def test_drop_total_rows_catches_non_word_total_labels_by_value(label):
    # \bTOTAL\b wouldn't match these; the value test carries them.
    df = _chart_df([(label, 100.0), ("Alpha", 60.0), ("Beta", 40.0)])
    assert drop_total_rows(df, "label", "value")["label"].tolist() == ["Alpha", "Beta"]


def test_drop_total_rows_handles_negative_result_sets():
    # Credit/offset charts are all-negative; magnitude comparison must still work.
    df = _chart_df([("Total credits", -100.0), ("Alpha", -60.0), ("Beta", -40.0)])
    assert drop_total_rows(df, "label", "value")["label"].tolist() == ["Alpha", "Beta"]


def test_drop_total_rows_shape_match_does_not_inflate_baseline():
    # A shape-matched total is dropped first; the value check for a second
    # candidate must measure against the REMAINING rows. With the baseline
    # bug (grand computed over all rows) SUBTOTAL's `others` would be 150 and
    # it would survive; measured correctly it is 50 and gets dropped.
    df = _chart_df([
        ("TOTAL - ALL GRANT FUNDS", 100.0),
        ("SUBTOTAL", 50.0),
        ("Alpha", 30.0),
        ("Beta", 20.0),
    ])
    out = drop_total_rows(df, "label", "value")
    assert out["label"].tolist() == ["Alpha", "Beta"]


def test_drop_total_rows_keeps_real_payee_in_mixed_sign_chart():
    # Credits make `others` negative; a positive payee matching its magnitude
    # is not a total, so sign agreement must be required.
    df = _chart_df([("TOTAL TOOL SUPPLY INC", 50.0), ("Alpha", -30.0), ("Beta", -20.0)])
    assert "TOTAL TOOL SUPPLY INC" in drop_total_rows(df, "label", "value")["label"].tolist()


@pytest.mark.parametrize("payee", [
    "TOTAL TOOL SUPPLY INC",
    "TOTAL ACCESS GROUP INC",
    "TOTAL RENOVATIONS",
    "TOTAL TRUCK PARTS INC",
])
def test_drop_total_rows_keeps_real_vendors_named_total(payee):
    df = _chart_df([(payee, 90.0), ("Alpha", 60.0), ("Beta", 40.0)])
    out = drop_total_rows(df, "label", "value")
    assert payee in out["label"].tolist()
    assert len(out) == 3


def test_drop_total_rows_catches_novel_total_label_by_value():
    # An unanticipated label shape is still caught when its value equals the
    # sum of the others (the property that makes a total bar harmful).
    df = _chart_df([("Overall total spending", 100.0), ("Alpha", 60.0), ("Beta", 40.0)])
    out = drop_total_rows(df, "label", "value")
    assert out["label"].tolist() == ["Alpha", "Beta"]


def test_drop_total_rows_is_a_noop_without_totals():
    df = _chart_df([("Alpha", 60.0), ("Beta", 40.0), ("Gamma", 10.0)])
    assert len(drop_total_rows(df, "label", "value")) == 3
    # missing/blank columns must not raise
    assert len(drop_total_rows(df, "nope", "value")) == 3


# ── Grants ────────────────────────────────────────────────────────────────────

GRANT_ROLLUP_QUERY = (
    "SELECT COALESCE(fund, 'TOTAL - ALL GRANT FUNDS') AS fund, "
    "ROUND(SUM(total_amount), 2) AS total_amount FROM summary_grant_funding "
    "GROUP BY ROLLUP(fund) ORDER BY total_amount DESC NULLS LAST"
)


def test_grant_rollup_prompt_query_stays_valid(con):
    """The SQL prompt tells the model to use EXACTLY this query for grant
    totals — if it ever breaks against the schema, the model will faithfully
    reproduce broken SQL. Pin it two ways: the string tested here must still
    appear verbatim in app.py's prompt (no silent drift between prompt and
    test), and executing it must yield a TOTAL row equal to the sum of the
    per-fund rows with a plausible source count and magnitude."""
    app_src = _app_source()
    assert GRANT_ROLLUP_QUERY in app_src, (
        "the prompt's copy-exact grant query no longer matches the tested one — "
        "update GRANT_ROLLUP_QUERY and this assertion together"
    )
    df = con.execute(GRANT_ROLLUP_QUERY).fetchdf()
    totals = df[df["fund"] == "TOTAL - ALL GRANT FUNDS"]
    assert len(totals) == 1, "exactly one grand-total row"
    funds = df[df["fund"] != "TOTAL - ALL GRANT FUNDS"]
    assert abs(totals["total_amount"].iloc[0] - funds["total_amount"].sum()) < 1.0
    assert len(funds) >= 50
    assert totals["total_amount"].iloc[0] > 1e9  # ~$1.17B as of 2026-07


# ── Technology topic vocabulary ──────────────────────────────────────────────

TECH_DEPT_SQL = (
    "SELECT 'Metro Technology Services department' AS spend_view, "
    "ROUND(SUM(extended_amount), 2) AS total_spend FROM expenditures "
    "WHERE fiscal_year = {yr} AND agency_canonical = 'Metro Technology Services' "
    "AND is_data_artifact = FALSE"
)
TECH_CATEGORY_SQL = (
    "SELECT 'Computer, software and cloud purchases (all departments)', "
    "ROUND(SUM(extended_amount), 2) FROM expenditures "
    "WHERE fiscal_year = {yr} AND (spend_category LIKE 'Computer%' "
    "OR spend_category ILIKE '%Software%' OR spend_category = 'Cloud Computing Services') "
    "AND is_data_artifact = FALSE"
)


def test_tech_topic_query_matches_prompt_and_returns_both_views(con):
    """The tech bullet prescribes a copy-exact UNION; pin it like the grant
    query. Both legs must appear verbatim in app.py, both must return money,
    and the category leg must include the non-Computer-prefixed software
    categories that were once missing (a ~43% undercount)."""
    app_src = _app_source()
    for frag in (TECH_DEPT_SQL, TECH_CATEGORY_SQL):
        assert frag.replace("{yr}", "{last_complete_year}") in app_src, (
            "the prompt's copy-exact tech query no longer matches this test"
        )
    year = con.execute("SELECT MAX(fiscal_year) - 1 FROM expenditures").fetchone()[0]
    dept = con.execute(TECH_DEPT_SQL.format(yr=year)).fetchone()[1]
    cat = con.execute(TECH_CATEGORY_SQL.format(yr=year)).fetchone()[1]
    assert dept and dept > 1e6, f"department view should be millions, got {dept}"
    assert cat and cat > 1e6, f"category view should be millions, got {cat}"
    # the broadened pattern must beat the old Computer-only one
    # same artifact filter as the prescribed query, so the comparison isolates
    # the category pattern rather than mixing in an unrelated filter difference
    narrow = con.execute(
        "SELECT SUM(extended_amount) FROM expenditures WHERE fiscal_year = ? "
        "AND (spend_category LIKE 'Computer%' OR spend_category = 'Cloud Computing Services') "
        "AND is_data_artifact = FALSE",
        [year],
    ).fetchone()[0]
    assert cat > narrow * 1.2, "broadened category filter must capture the software categories"
    # the two views overlap, so their intersection is smaller than either
    both = con.execute(
        "SELECT SUM(extended_amount) FROM expenditures WHERE fiscal_year = ? "
        "AND agency_canonical = 'Metro Technology Services' "
        "AND (spend_category LIKE 'Computer%' OR spend_category ILIKE '%Software%' "
        "OR spend_category = 'Cloud Computing Services') AND is_data_artifact = FALSE",
        [year],
    ).fetchone()[0]
    assert both < dept and both < cat


def test_prompt_topic_values_exist_in_the_data(con):
    """Agency/category literals named in the Topic Vocabulary must be real."""
    for agency in ["Metro Technology Services", "Louisville Metro Police Department",
                   "Louisville Fire", "Parks & Recreation", "Public Works & Assets"]:
        n = con.execute(
            "SELECT COUNT(*) FROM expenditures WHERE agency_canonical = ?", [agency]
        ).fetchone()[0]
        assert n > 0, f"prompt names agency {agency!r} which has no rows"
    for cat in ["Computer Software", "Cloud Computing Services", "Software Maintenance"]:
        n = con.execute(
            "SELECT COUNT(*) FROM expenditures WHERE spend_category = ?", [cat]
        ).fetchone()[0]
        assert n > 0, f"prompt names spend_category {cat!r} which has no rows"


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


def test_arp_relief_money_is_labelled_the_way_the_prompt_says(con):
    """The SQL prompt tells the model ARPA money lives under fund = 'ARP'.
    That is a claim about the data: if a refresh renames the fund, the prompt
    starts producing confident $0 answers about a real $37M program."""
    funds = {r[0] for r in con.execute(
        "SELECT DISTINCT fund FROM expenditures WHERE fund IS NOT NULL"
    ).fetchall()}
    assert "ARP" in funds, "fund 'ARP' vanished — the prompt's ARPA mapping is now wrong"
    # matching the filter the prompt prescribes, artifact exclusion included
    total = con.execute(
        "SELECT SUM(extended_amount) FROM expenditures "
        "WHERE fund = 'ARP' AND is_data_artifact = FALSE"
    ).fetchone()[0]
    assert total and total > 1_000_000, f"fund 'ARP' holds only {total}"
    # the spelling the model reaches for on its own must still match nothing,
    # or the mapping would be unnecessary and possibly double-counting
    assert not [f for f in funds if "american rescue" in f.lower()]
    # the prompt names this fund with an equality filter, so a prefix check
    # would pass while the prescribed query returns $0
    assert "CARES Coronavirus Relief Fund (CRF)" in funds


def test_the_arp_mapping_is_pinned_in_the_prompt_itself():
    """The data claim above is only useful if the prompt still makes it."""
    src = _app_source()
    assert "the fund value is the bare string 'ARP'" in src
    assert "fund = 'CARES Coronavirus Relief Fund (CRF)'" in src
    # the prescribed filter must exclude artifacts, like the adjacent bullets
    bullet = [ln for ln in src.splitlines() if "ARPA / ARP / American Rescue Plan" in ln]
    assert bullet and "is_data_artifact = FALSE" in bullet[0]


# ── humanize_text: labels belong in tables, not in the middle of sentences ────

def test_prose_mode_leaves_ordinary_english_words_alone():
    """`Other` is a salary column labelled "Other Pay", so the unrestricted
    mapping rewrote "Other notable spends" into "Other Pay notable spends".
    Every word here is a real column name in some shipped table."""
    from data_model import humanize_text
    sentence = ("Other notable spends include the fund for a program in that "
                "region; the Chief approved the Allocation, and the "
                "Description of the project names the department and payee.")
    assert humanize_text(sentence, prose=True) == sentence


def test_prose_mode_still_removes_camel_and_shouted_jargon():
    """The earlier "has no underscore" proxy also spared jobTitle, CalYear and
    LICENSENO, which then streamed into answers raw — the exact jargon this
    function exists to remove."""
    from data_model import humanize_text
    out = humanize_text("Pay for the jobTitle listed in CalYear, plus the "
                        "LICENSENO on file.", prose=True)
    for jargon in ("jobTitle", "CalYear", "LICENSENO"):
        assert jargon not in out, f"{jargon} survived prose humanization"


def test_prose_mode_still_humanizes_real_identifiers():
    from data_model import humanize_text
    out = humanize_text("Totals come from extended_amount by agency_canonical.",
                        prose=True)
    assert "extended_amount" not in out and "agency_canonical" not in out
    assert "Extended Amount" in out


def test_table_mode_still_maps_bare_column_names():
    """In a results table a bare `Other` really is the column — prose mode
    must not have weakened the table path."""
    from data_model import humanize_text
    assert humanize_text("Other") != "Other"
    assert humanize_text("extended_amount") == humanize_text("extended_amount", prose=True)
