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


def test_prompt_topic_mappings_name_values_that_exist(con):
    """Every agency_canonical the SQL prompt hardcodes must still exist in the
    data — a renamed agency turns a topical question into a silent zero.

    Distinct from the prompt-pinning tests above, which only prove app.py still
    contains the string: the prompt can name 'Metro Technology Services'
    forever while the canonical map renames it, and every source-text assertion
    stays green while the query returns nothing."""
    agencies = {r[0] for r in con.execute(
        "SELECT DISTINCT agency_canonical FROM expenditures WHERE agency_canonical IS NOT NULL"
    ).fetchall()}
    src = _app_source()
    for name in ("Louisville Metro Police Department", "Louisville Fire",
                 "Parks & Recreation", "Public Works & Assets",
                 "Metro Technology Services"):
        assert name in src, f"{name} no longer referenced in the prompt"
        assert name in agencies, f"prompt names agency '{name}' that is not in the data"


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


# ── chart inference must not refuse what the renderer would happily truncate ──

def test_a_ranking_longer_than_the_render_cap_still_charts():
    """"Which agencies have spent the most?" returns 61 rows and produced NO
    chart, while the same query capped at 50 charted fine — the inference
    ceiling sat below the renderer's own reach. A ranked result that merely
    needs truncating is the most chartable shape there is."""
    import pandas as pd
    from data_model import infer_chart, CHART_MAX_POINTS, CHART_MAX_ROWS
    df = pd.DataFrame({
        "agency_canonical": [f"Agency {i}" for i in range(61)],
        "total_spend": [float(1_000_000_000 - i * 10_000_000) for i in range(61)],
    })
    assert infer_chart(df)[0] == "bar"
    # the ceiling must stay clear of the render cap, or this regresses quietly
    assert CHART_MAX_ROWS > CHART_MAX_POINTS


def test_a_raw_dump_is_still_refused():
    """The ceiling exists so a multi-thousand-row result is not 'summarized'
    by whichever 30 rows happen to come first."""
    import pandas as pd
    from data_model import infer_chart, CHART_MAX_ROWS
    df = pd.DataFrame({"payee": [f"P{i}" for i in range(CHART_MAX_ROWS + 1)],
                       "amt": [float(i) for i in range(CHART_MAX_ROWS + 1)]})
    assert infer_chart(df)[0] is None


def test_a_truncated_ranking_keeps_the_top_and_says_so():
    """30 of 61 bars rendered as a bare 'Total Spend' reads as the whole
    ranking."""
    import pandas as pd
    from data_model import chart_window, CHART_MAX_POINTS
    df = pd.DataFrame({"agency": [f"A{i}" for i in range(61)],
                       "total_spend": [float(1000 - i) for i in range(61)]})
    out, note = chart_window(df, "bar", "agency", "total_spend")
    assert len(out) == CHART_MAX_POINTS
    assert note == f"top {CHART_MAX_POINTS} of 61"
    assert out["total_spend"].iloc[0] == 1000.0, "the heaviest row must survive"


def test_a_truncated_time_series_keeps_the_NEWEST_points():
    """The caller sorts a line frame oldest-first, so taking the head charts
    the 30 oldest months of a 61-month series and drops everything recent —
    while labelling it 'top 30', which is both untrue and points away from the
    data it cut. This is the regression that raising the ceiling introduced."""
    import pandas as pd
    from data_model import chart_window, CHART_MAX_POINTS
    months = pd.date_range("2021-01-01", periods=61, freq="MS").strftime("%Y-%m")
    df = pd.DataFrame({"month": months, "total_spend": [float(i) for i in range(61)]})
    out, note = chart_window(df, "line", "month", "total_spend")
    assert note == f"last {CHART_MAX_POINTS} of 61"
    assert out["month"].iloc[-1] == months[-1], "the newest point must be charted"
    assert months[0] not in set(out["month"]), "the oldest points are the ones dropped"


def test_an_unranked_bar_result_does_not_claim_to_be_a_top_n():
    """A result the SQL ordered by name is just the first N of M."""
    import pandas as pd
    from data_model import chart_window, CHART_MAX_POINTS
    df = pd.DataFrame({"payee": [f"P{i:03d}" for i in range(61)],
                       "amt": [float((i * 37) % 61) for i in range(61)]})
    _, note = chart_window(df, "bar", "payee", "amt")
    assert note == f"{CHART_MAX_POINTS} of 61"
    assert "top" not in note


def test_a_frame_that_fits_is_left_alone():
    import pandas as pd
    from data_model import chart_window
    df = pd.DataFrame({"a": ["x", "y"], "v": [2.0, 1.0]})
    out, note = chart_window(df, "bar", "a", "v")
    assert note is None and len(out) == 2


# ── a cached answer can never replay a dead citation link ────────────────────

def test_the_citation_format_participates_in_the_cache_version():
    """A cached answer replays its stored SSE frames verbatim and never
    re-runs retrieval, so rag's read-time healing cannot reach one. The cache
    version was a hash of the PROMPTS only, and fixing a URL changes no
    prompt — so the Gateway fix would have shipped while warm_cache.py's
    pre-warmed starter answers kept serving 'Invalid parameters!' forever."""
    src = _app_source()
    assert "CITATION_FORMAT" in src
    version_call = src[src.index("CACHE_VERSION = hashlib.sha1("):]
    version_call = version_call[:version_call.index(").hexdigest()")]
    assert "CITATION_FORMAT" in version_call, \
        "a citation-format change must orphan every cached answer"


def test_loading_the_cache_drops_entries_with_dead_links(tmp_path, monkeypatch):
    """States the invariant directly rather than relying on the version bump,
    which only helps the one time someone remembers to change it."""
    import json as _json
    import app
    good_key, dead_key = "v1:good question", "v1:dead question"
    cache = {
        good_key: ['data: {"type": "sources", "content": "Gateway.aspx?M=L&ID=1"}\n\n'],
        dead_key: ['data: {"type": "sources", "content": "LegislationDetail.aspx?ID=1&GUID=x"}\n\n'],
    }
    path = tmp_path / ".response_cache.json"
    with open(path, "w") as f:
        _json.dump(cache, f)
    monkeypatch.setattr(app, "CACHE_FILE", str(path))
    loaded = app._load_cache()
    assert good_key in loaded
    assert dead_key not in loaded, "a cached dead link is served to a reader as-is"


@pytest.mark.parametrize("order", ["ascending", "descending"])
def test_a_month_series_keeps_its_newest_end_end_to_end(order):
    """The bug the hand-written "line" test could not see. A month axis is
    SUBSTR(invoice_date, 1, 7) -> '2021-01' strings, which failed the old
    \\d{4}-only sortability check and were classified "bar" — so a 61-month
    series went down the categorical path and lost everything recent, exactly
    the truncation the tail branch was added to prevent. Runs the real
    infer_chart -> chart_window path rather than asserting a chart type."""
    from data_model import infer_chart, chart_window, CHART_MAX_POINTS
    months = pd.date_range("2021-01-01", periods=61, freq="MS").strftime("%Y-%m")
    spend = [float(i) for i in range(61)]
    df = pd.DataFrame({"month": months, "total_spend": spend})
    if order == "descending":            # SQL that ordered newest-first
        df = df.iloc[::-1].reset_index(drop=True)

    chart_type, label_col, value_col = infer_chart(df)
    assert chart_type == "line", "a zero-padded YYYY-MM axis sorts chronologically"
    out, note = chart_window(df, chart_type, label_col, value_col)
    assert note == f"last {CHART_MAX_POINTS} of 61"
    charted = set(out["month"])
    assert months[-1] in charted, "the newest month must survive truncation"
    assert months[0] not in charted, "the oldest months are what gets dropped"


def test_a_month_axis_truncates_from_the_right_end_even_as_a_bar():
    """Belt and braces: the decision is made from the frame, so a time axis
    that lands on the categorical path for any other reason still keeps its
    recent end instead of its oldest."""
    from data_model import chart_window, CHART_MAX_POINTS
    months = pd.date_range("2021-01-01", periods=61, freq="MS").strftime("%Y-%m")
    df = pd.DataFrame({"month": months, "amt": [float(i) for i in range(61)]})
    out, note = chart_window(df, "bar", "month", "amt")
    assert note == f"last {CHART_MAX_POINTS} of 61"
    assert months[-1] in set(out["month"])


def test_a_non_chronological_label_is_not_mistaken_for_a_time_axis():
    """Month NAMES mis-sort lexicographically, which is why infer_chart
    downgrades them. chart_window must not then treat them as ordered time."""
    from data_model import chart_window, CHART_MAX_POINTS
    names = [f"{m} 2021" for m in
             ("Apr", "Aug", "Dec", "Feb", "Jan", "Jul", "Jun", "Mar", "May", "Nov", "Oct", "Sep")] * 6
    df = pd.DataFrame({"month_name": names[:61], "amt": [float(61 - i) for i in range(61)]})
    _, note = chart_window(df, "bar", "month_name", "amt")
    assert note == f"top {CHART_MAX_POINTS} of 61", "values descend, so this is a ranking"


@pytest.mark.parametrize("dtype", ["datetime", "string"])
def test_one_null_time_bucket_does_not_flip_the_truncation_end(dtype):
    """is_monotonic_increasing is False whenever a series holds NaN/NaT, and
    only the string branch of is_chronological dropped nulls. So a single null
    month made a genuinely chronological axis answer False in BOTH directions,
    and chart_window fell through to head() — charting 2021-01..2023-07 and
    dropping everything to 2026-01, the same wrong-end truncation by a third
    route. Nulls are real here: the Louisville pack filters `fiscal_year IS NOT
    NULL` because the raw rows carry them, and generated SQL may not."""
    from data_model import infer_chart, chart_window, CHART_MAX_POINTS
    stamps = pd.date_range("2021-01-01", periods=61, freq="MS")
    if dtype == "datetime":
        axis = pd.Series(list(stamps))
        axis.iloc[7] = pd.NaT
        newest = stamps[-1]
    else:
        axis = pd.Series(list(stamps.strftime("%Y-%m")))
        axis.iloc[7] = None
        newest = stamps.strftime("%Y-%m")[-1]

    df = pd.DataFrame({"month": axis, "total_spend": [float(i) for i in range(61)]})
    df = df.sort_values("month")
    chart_type, label_col, value_col = infer_chart(df)
    out, note = chart_window(df, chart_type, label_col, value_col)
    # 60 real buckets, not 61: the null one is not chartable, and the note must
    # count what is actually rendered.
    assert note == f"last {CHART_MAX_POINTS} of 60"
    assert len(out) == CHART_MAX_POINTS
    assert newest in set(out["month"]), "the newest month must survive"
    assert stamps[0] not in set(out["month"]), "the oldest end is what gets dropped"
    # The null bucket sorts LAST, so before it was dropped it rendered as a bar
    # labelled "NaT"/"None" at the newest position, carrying a real total.
    rendered = out["month"].astype(str).tolist()
    assert not [lbl for lbl in rendered if lbl in ("NaT", "None", "nan")], \
        f"a null bucket was charted as a real point: {rendered[-3:]}"
    assert out["month"].notna().all()


def test_an_all_null_axis_is_not_treated_as_ordered_time():
    """Nothing to order by, so it must not claim a chronological window."""
    from data_model import is_chronological
    assert not is_chronological(pd.Series([None, None, None], dtype="object"))
    assert not is_chronological(pd.Series([pd.NaT, pd.NaT]))


def test_a_null_label_is_never_charted_as_a_point():
    """Nulls sort LAST in both pandas and DuckDB, so on a time axis the null
    bucket lands at the newest position — a bar labelled "NaT" carrying a real
    SUM, which a reader takes for the current month. It is not a category
    either, so it is dropped for every chart type, not only time axes."""
    from data_model import chart_window, CHART_MAX_POINTS
    labels = [f"Agency {i}" for i in range(60)] + [None]
    df = pd.DataFrame({"agency": labels, "amt": [float(100 - i) for i in range(61)]})
    out, note = chart_window(df, "bar", "agency", "amt")
    assert note == f"top {CHART_MAX_POINTS} of 60", "the count must exclude the unchartable row"
    assert out["agency"].notna().all()


def test_a_mostly_null_axis_leaves_nothing_to_chart():
    """The caller re-checks after chart_window for exactly this."""
    from data_model import chart_window
    df = pd.DataFrame({"month": [None] * 60 + ["2026-01"],
                       "amt": [float(i) for i in range(61)]})
    out, _ = chart_window(df, "line", "month", "amt")
    assert len(out) < 2, "app.py raises on this and skips the chart"


# ── an unordered result is not a random one ──────────────────────────────────

def test_an_unordered_result_leads_with_the_largest_value():
    """Generated SQL often omits ORDER BY entirely — the reported case was a
    UNION ALL of a Mayor filter and a Police Chief filter — and DuckDB then
    returns storage order. The reader gets a table in no discernible order and
    a bar chart whose bars jump around, for a question that plainly meant
    "who earns the most"."""
    from data_model import order_for_display
    df = pd.DataFrame({
        "role": ["Mayor", "Mayor", "Police Chief", "Police Chief"],
        "employee": ["James", "Greenberg", "Bates", "Humphrey"],
        "ytd_total": [146035.41, 158115.58, 201104.74, 267811.04],
    })
    out = order_for_display(df, "SELECT ... UNION ALL SELECT ...;")
    assert out["employee"].tolist() == ["Humphrey", "Bates", "Greenberg", "James"]
    assert out["ytd_total"].is_monotonic_decreasing


def test_an_explicit_order_by_is_never_second_guessed():
    """An ORDER BY is an expressed intent, even when it disagrees with the
    heuristic — including one that sorts ascending on purpose."""
    from data_model import order_for_display
    df = pd.DataFrame({"payee": ["A", "B", "C"], "amt": [3.0, 1.0, 2.0]})
    for sql in ("SELECT payee, amt FROM t ORDER BY payee",
                "select * from t order by amt asc",
                "SELECT * FROM t\nORDER   BY  amt DESC"):
        assert order_for_display(df, sql)["amt"].tolist() == [3.0, 1.0, 2.0], sql


def test_a_time_keyed_result_keeps_its_chronology():
    """Chronology is the order of a trend, so reordering by value would make
    the table nonsense and fight the chart layer, which sorts by axis."""
    from data_model import order_for_display
    df = pd.DataFrame({"fiscal_year": [2024, 2025, 2026],
                       "total_spend": [5.0, 9.0, 1.0]})
    assert order_for_display(df, "SELECT ... GROUP BY 1")["fiscal_year"].tolist() == [2024, 2025, 2026]


def test_reordering_is_stable_and_safe_on_odd_input():
    """Ties keep the order the query produced; nothing here may raise."""
    from data_model import order_for_display
    tied = pd.DataFrame({"name": ["first", "second"], "amt": [7.0, 7.0]})
    assert order_for_display(tied, "SELECT 1")["name"].tolist() == ["first", "second"]
    # no measure to sort by, single row, and a missing sql are all no-ops
    assert len(order_for_display(pd.DataFrame({"a": ["x", "y"]}), "SELECT 1")) == 2
    assert len(order_for_display(pd.DataFrame({"a": [1.0]}), "SELECT 1")) == 1
    assert len(order_for_display(pd.DataFrame({"a": [1.0, 2.0]}), None)) == 2


def test_the_prompt_asks_for_an_explicit_order_by():
    """The deterministic sort is a safety net; the SQL should say what it
    means, and a UNION ALL needs its ORDER BY after the final SELECT."""
    src = _app_source()
    assert "ALWAYS give a multi-row result an explicit ORDER BY" in src
    assert "UNION ALL queries too" in src



# ── which results may be reordered at all ────────────────────────────────────
# The rule is deliberately blunt: a result is reorderable only when its order
# is definitionally arbitrary — every ORDER BY it contains, if any, lives in an
# OVER (...) spec, which cannot order a result set. Deciding which nested
# clauses "really" survive means deciding whether every enclosing body is a
# plain scan, through CTE chains, derived tables, comma joins and set
# operations; five attempts at that judgement each shipped a REVERSED order.

_WINDOW_FN_SQL = (
    "SELECT agency_canonical, payee_canonical, total FROM ("
    "SELECT agency_canonical, payee_canonical, SUM(extended_amount) AS total, "
    "ROW_NUMBER() OVER (PARTITION BY agency_canonical ORDER BY SUM(extended_amount) DESC) AS rn "
    "FROM expenditures GROUP BY 1,2) WHERE rn <= 3"
)
_CTE_BOTTOM_N_SQL = (
    "WITH lowest AS (SELECT payee_canonical, SUM(extended_amount) AS total "
    "FROM expenditures GROUP BY 1 ORDER BY total ASC LIMIT 10) SELECT * FROM lowest"
)
_CTE_OUTER_LIMIT_SQL = (
    "WITH lowest AS (SELECT payee_canonical, SUM(extended_amount) AS total "
    "FROM expenditures GROUP BY 1 ORDER BY total ASC) SELECT * FROM lowest LIMIT 10"
)


@pytest.mark.parametrize("sql,ordered,why", [
    # Reorderable: nothing in the statement speaks to row order.
    ("SELECT a, b FROM t", False, "no ORDER BY at all"),
    ("SELECT ... UNION ALL SELECT ...", False, "the reported salary query"),
    ("SELECT p, SUM(a) AS total FROM e GROUP BY 1", False, "a plain aggregate"),
    ("SELECT * FROM t WHERE payee = 'ORDER BY INC'", False, "string literal"),
    ("SELECT * FROM t -- ORDER BY amt", False, "line comment"),
    ("SELECT * FROM t /* ORDER BY amt */", False, "block comment"),
    ("SELECT reorder_by FROM t", False, "word boundary"),
    # Not reorderable: the query said something about order, so it is left alone.
    ("SELECT a FROM t ORDER BY a", True, "plain top-level clause"),
    ("select * from t order   by  amt desc", True, "case and whitespace"),
    (_CTE_BOTTOM_N_SQL, True, "bottom-N, LIMIT inside the CTE"),
    (_CTE_OUTER_LIMIT_SQL, True, "bottom-N, LIMIT on the enclosing SELECT"),
    (_WINDOW_FN_SQL, True, "a window ORDER BY counts too — it may be a rank filter"),
    (_WINDOW_FN_SQL.replace("DESC", "ASC"), True, "the bottom-N sibling likewise"),
    ("SELECT p, SUM(a) AS total FROM e GROUP BY 1 "
     "QUALIFY ROW_NUMBER() OVER (ORDER BY SUM(a) ASC) <= 10", True,
     "QUALIFY filters a rank with no subquery at all"),
    ("SELECT p, total, SUM(total) OVER (ORDER BY d) AS running FROM e", True,
     "ACCEPTED COST: a value window decorates rather than selects, but "
     "telling the two apart is what leaked seven times"),
    ("SELECT p, d, amt FROM (SELECT *, ROW_NUMBER() OVER "
     "(PARTITION BY p ORDER BY d DESC) rn FROM e) WHERE rn = 1", True,
     "ACCEPTED COST: the de-duplication idiom is genuinely arbitrary across "
     "entities, but its rank key is indistinguishable from a bottom-N's"),
])
def test_only_an_arbitrary_order_may_be_reordered(sql, ordered, why):
    from data_model import sql_orders_result
    assert sql_orders_result(sql) is ordered, why


@pytest.mark.parametrize("sql,why", [
    (_CTE_BOTTOM_N_SQL, "LIMIT inside the CTE"),
    (_CTE_OUTER_LIMIT_SQL, "LIMIT on the enclosing SELECT"),
    (_CTE_BOTTOM_N_SQL.replace("ORDER BY total ASC", "ORDER BY total"),
     "SQL's default direction is ASC"),
    ("WITH r AS (SELECT p, SUM(a) AS total FROM e GROUP BY 1 ORDER BY total ASC), "
     "j AS (SELECT r.p, r.total FROM r JOIN x ON 1=1) SELECT * FROM j LIMIT 20",
     "the join lives in an intermediate CTE"),
    ("WITH r AS (SELECT p, SUM(a) AS total FROM e GROUP BY 1 ORDER BY total ASC) "
     "SELECT * FROM (SELECT r.p, r.total FROM r JOIN x ON 1=1) LIMIT 20",
     "derived table"),
    ("WITH r AS (SELECT p, SUM(a) AS total FROM e GROUP BY 1 ORDER BY total ASC) "
     "SELECT r.p, r.total FROM r, x WHERE r.p = x.p LIMIT 20", "implicit comma join"),
    ("WITH r AS (SELECT p, SUM(a) AS total FROM e GROUP BY 1 ORDER BY total ASC) "
     "SELECT * FROM r UNION SELECT * FROM o LIMIT 20", "set operation"),
    ("WITH recent AS (SELECT * FROM e ORDER BY check_date ASC LIMIT 100) "
     "SELECT p, SUM(a) AS total FROM recent GROUP BY 1", "a slice on a non-measure column"),
])
def test_a_bottom_n_or_sliced_result_is_never_flipped(sql, why):
    """Every one of these once rendered smallest-first as largest-first, or the
    reverse, because the direction was inferred from the query text. None of
    them is reordered now — the frame is served exactly as the query built it,
    which is at worst unhelpful rather than confidently wrong."""
    from data_model import order_for_display
    df = pd.DataFrame({"payee": ["cheap", "mid", "big"], "total": [1.0, 2.0, 3.0]})
    assert order_for_display(df, sql)["total"].tolist() == [1.0, 2.0, 3.0], why


def test_a_rank_filtered_window_is_left_alone_whichever_way_it_ranks():
    """A ranking window's ORDER BY decides which rows survive the rank filter
    that follows, exactly as an inner LIMIT does — so "bottom 3 per agency"
    (ASC) is a smallest-first question and sorting it descending renders
    largest-of-the-cheapest first. The DESC sibling is indistinguishable
    without reading the direction out of the SQL, which is what repeatedly
    produced reversed results, so both are served as the query built them."""
    from data_model import order_for_display
    df = pd.DataFrame({"payee": ["cheap", "mid", "big"], "total": [1.0, 2.0, 3.0]})
    for sql in (_WINDOW_FN_SQL, _WINDOW_FN_SQL.replace("DESC", "ASC")):
        assert order_for_display(df, sql)["total"].tolist() == [1.0, 2.0, 3.0]


def test_a_query_carrying_any_order_by_is_served_as_built():
    """The accepted cost of the blunt rule, pinned rather than latent. Both of
    these ARE genuinely arbitrary results that would benefit from sorting —
    but a value window's ORDER BY and a rank filter's ORDER BY are the same
    tokens, and a bottom-N can rank a plain column just as a de-dupe ranks a
    date. Seven attempts to separate them each shipped a reversed result."""
    from data_model import order_for_display
    df = pd.DataFrame({"payee": ["a", "b", "c"], "total": [1.0, 3.0, 2.0]})
    for sql in (
        "SELECT payee, total, SUM(total) OVER (ORDER BY d) AS running FROM e",
        "SELECT p, d, amt FROM (SELECT *, ROW_NUMBER() OVER "
        "(PARTITION BY p ORDER BY d DESC) rn FROM e) WHERE rn = 1",
    ):
        assert order_for_display(df, sql)["total"].tolist() == [1.0, 3.0, 2.0]


def test_no_sql_shape_can_invert_a_result():
    """The invariant the whole rule exists for: every query that selected rows
    by rank or limit — in any spelling found across eight review rounds — is
    served exactly as built, so none of them can come back reversed."""
    from data_model import order_for_display
    df = pd.DataFrame({"payee": ["cheap", "mid", "big"], "total": [1.0, 2.0, 3.0]})
    selective = [
        _CTE_BOTTOM_N_SQL,
        _CTE_OUTER_LIMIT_SQL,
        _WINDOW_FN_SQL.replace("DESC", "ASC"),
        "SELECT a,p,total FROM (SELECT a,p,total, ROW_NUMBER() OVER "
        "(PARTITION BY a ORDER BY total ASC) rn FROM s) WHERE rn <= 3",
        "SELECT p, SUM(a) AS total FROM e GROUP BY 1 "
        "QUALIFY ROW_NUMBER() OVER (ORDER BY SUM(a) ASC) <= 10",
        "WITH recent AS (SELECT * FROM e ORDER BY d ASC LIMIT 100) "
        "SELECT p, SUM(a) AS total FROM recent GROUP BY 1",
    ]
    for sql in selective:
        assert order_for_display(df, sql)["total"].tolist() == [1.0, 2.0, 3.0], sql


def test_the_reorder_is_only_ever_descending():
    """The direction is never taken from the query. Borrowing it is how this
    function turned "which ten payees received the least" into
    largest-of-the-ten-first, five separate ways."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data_model.py")).read()
    body = src[src.index("def order_for_display("):src.index("def chart_window(")]
    assert "ascending=False" in body
    assert "ascending=True" not in body


# ── a truncated result must not read as a complete one ───────────────────────

def test_a_long_result_says_what_was_cut():
    """"How much grant funding, and from which sources?" returns 103 rows.
    pandas renders 25 head + "..." + 25 tail, and the model enumerated the 24
    visible funds (the head minus the ROLLUP total) as though that were every
    source — while the chart beside it said "top 30 of 102". The gap has to be
    stated, not implied by an ellipsis."""
    import duckdb
    from analytics_agent import execute_sql_safe, MAX_DISPLAY_ROWS
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT i AS fund, i * 1.0 AS amt "
                f"FROM range({MAX_DISPLAY_ROWS + 53}) t(i)")
    _, s = execute_sql_safe(con, "SELECT fund, amt FROM t ORDER BY amt DESC")
    con.close()
    note = [ln for ln in s.splitlines() if ln.startswith("[")]
    assert note, "a truncated table shipped with no note"
    assert f"has {MAX_DISPLAY_ROWS + 53} rows" in note[0]
    assert "53 rows in between are NOT shown" in note[0]
    assert "partial" in note[0]
    # the grant result's 103rd row is a ROLLUP total, so the row count is not
    # a count of funding sources — the chart beside it correctly says 102
    assert "quote the DATA row count" in note[0]


def test_the_note_separates_data_rows_from_a_rollup_total():
    """The grant query returns 103 rows for 102 funds — the extra is a ROLLUP
    grand total. Quoting 103 beside a chart titled "top 30 of 102" is the
    mismatch that prompted this, so the note makes the same subtraction the
    chart does."""
    import duckdb
    from analytics_agent import execute_sql_safe, MAX_DISPLAY_ROWS
    n = MAX_DISPLAY_ROWS + 20
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT 'Fund ' || i AS fund, i * 1.0 AS amt "
                f"FROM range(1, {n + 1}) t(i)")
    _, s = execute_sql_safe(
        con,
        "SELECT COALESCE(fund, 'TOTAL - ALL FUNDS') AS fund, SUM(amt) AS amt "
        "FROM t GROUP BY ROLLUP(fund)")
    con.close()
    note = [ln for ln in s.splitlines() if ln.startswith("[")][0]
    assert f"has {n + 1:,} rows" in note, note
    assert f"{n:,} are data rows" in note, note


def test_a_short_result_gets_no_note():
    """Nothing was cut, so nothing to disclose."""
    import duckdb
    from analytics_agent import execute_sql_safe, MAX_DISPLAY_ROWS
    con = duckdb.connect()
    con.execute(f"CREATE TABLE t AS SELECT i AS fund, i * 1.0 AS amt "
                f"FROM range({MAX_DISPLAY_ROWS}) t(i)")
    _, s = execute_sql_safe(con, "SELECT fund, amt FROM t ORDER BY amt DESC")
    con.close()
    assert not [ln for ln in s.splitlines() if ln.startswith("[")]


def test_the_prompt_forbids_passing_off_a_truncated_list_as_complete():
    src = _app_source()
    assert "A long result is TRUNCATED" in src
    assert "IN WHATEVER ORDER THE QUERY PRODUCED" in src


def test_neither_the_note_nor_the_prompt_claims_the_head_is_the_largest():
    """order_for_display sorts by the measure ONLY when the SQL has no ORDER BY;
    anything carrying one is served as built, including ASC and keys like a
    month or a payee name. So "which funds received the least" puts the
    SMALLEST rows in the visible head — calling them "the largest few" is the
    same reversal the ordering rules were stripped back to prevent, committed
    in prose instead of in the frame."""
    import duckdb
    from analytics_agent import execute_sql_safe, TRUNCATION_NOTE, MAX_DISPLAY_ROWS
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT 'Fund ' || i AS fund, i * 1.0 AS amt "
                f"FROM range(1, {MAX_DISPLAY_ROWS + 40}) t(i)")
    _, s = execute_sql_safe(con, "SELECT fund, amt FROM t ORDER BY amt ASC")
    con.close()
    note = [ln for ln in s.splitlines() if ln.startswith("[")][0]
    # the ascending query really does show the smallest rows first
    assert " Fund 1 " in s.splitlines()[1] or "Fund 1" in s.splitlines()[1]
    claim = note.split("Do not describe")[0]
    assert "largest" not in claim and "smallest" not in claim, claim
    assert "in the order the query produced them" in note
    assert "largest" not in TRUNCATION_NOTE.split("Do not describe")[0]


def test_the_truncation_note_is_part_of_the_cache_key():
    """The note is model-visible input. When only the prompts were hashed, a
    note-only edit changed what the model read while cached answers stayed
    valid — observed live, where two verification runs replayed a pre-fix
    answer and looked like the fix had failed."""
    src = _app_source()
    call = src[src.index("CACHE_VERSION = hashlib.sha1("):]
    call = call[:call.index(").hexdigest()")]
    assert "TRUNCATION_NOTE" in call
    assert "MAX_DISPLAY_ROWS" in call
