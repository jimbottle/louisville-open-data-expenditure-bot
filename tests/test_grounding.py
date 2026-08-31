"""Vocabulary grounding (grounding.py): the value index, question-term lookup,
and the filter diagnosis behind the verify-and-repair step.

All in-memory: a small synthetic ledger shaped like the real one (padded
categories, a canonical column, an amount column, an enrichment table), so
these run in CI without the gitignored data. The production failure that
motivated the module — '%Vehicle%' matching nothing for a department whose
vehicle spend sits under 'Automotive ...' categories — is reproduced here.
"""
import duckdb
import pytest

import grounding as g
from city_config import CityConfig


def _cfg(extra=None):
    raw = {
        "expenditures": {"table": "expenditures"},
        "data_quality": {"table": "expenditures", "amount_column": "extended_amount"},
        "canonicalization": [{"table": "expenditures", "source_column": "payee",
                              "target_column": "payee_canonical"}],
        "grounding": extra or {},
    }
    return CityConfig(raw, "/nonexistent")


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("""
        CREATE TABLE expenditures AS SELECT * FROM (VALUES
          ('Louisville Fire', 2024, 'Automotive Parts & Accessories', 'NAPA AUTO PARTS', 'NAPA AUTO PARTS', 'General Fund', 900000.0, FALSE),
          ('Louisville Fire', 2024, 'Automotive Fuel', 'FLEETONE', 'FLEETONE', 'General Fund', 500000.0, FALSE),
          ('Louisville Fire', 2024, 'Automotive Fuel   ', 'FLEETONE', 'FLEETONE', 'General Fund', 4000.0, FALSE),
          ('Louisville Fire', 2023, 'Vehicles Fire Apparatus Trucks', 'SEAGRAVE FIRE APPARATUS LLC', 'SEAGRAVE FIRE APPARATUS LLC', 'Fire Equipment Replacement Fund', 1200000.0, FALSE),
          ('Public Works & Assets', 2024, 'Street Cleaning Vehicle', 'ELGIN SWEEPER', 'ELGIN SWEEPER', 'General Fund', 300000.0, FALSE),
          ('Public Works & Assets', 2024, 'Paving Expense', 'LOUISVILLE PAVING COMPANY INC', 'Louisville Paving Company Inc', 'Municipal Aid', 2500000.0, FALSE),
          ('Office of Housing', 2023, 'Rental Assistance Tenant', 'ST JOHN CENTER INC', 'ST JOHN CENTER INC', 'ARP', 800000.0, FALSE),
          ('Office of Housing', 2023, 'Emergency Shelter', 'WAYSIDE MISSION', 'WAYSIDE MISSION', 'ARP', 200000.0, FALSE),
          ('Parks & Recreation', 2024, 'Carpet Replacement', 'SHARP FLOORING', 'SHARP FLOORING', 'General Fund', 50000.0, FALSE),
          ('Parks & Recreation', 2024, 'Carpet Replacement', 'SUSTEEN', 'SUSTEEN', 'General Fund', 224000000.0, TRUE)
        ) t(agency_canonical, fiscal_year, spend_category, payee, payee_canonical, fund, extended_amount, is_data_artifact)
    """)
    c.execute("""
        CREATE TABLE salary_data AS SELECT * FROM (VALUES
          ('Greenberg, Craig', 'Mayor', 'Mayor Office', 2024, 140000.0),
          ('Smith, Pat', 'Deputy Mayor', 'Mayor Office', 2024, 120000.0),
          ('Jones, Sam', 'Police Officer', 'Louisville Metro Police Department', 2024, 90000.0)
        ) t(Employee_Name, jobTitle, Department, CalYear, YTD_Total)
    """)
    g.build_value_index(c, _cfg())
    return c


# ── Index construction ───────────────────────────────────────────────────────

def test_index_covers_categoricals_and_skips_the_raw_canonical_source(con):
    cols = {(t, c) for t, c in con.execute(
        f"SELECT DISTINCT tbl, col FROM {g.VALUE_INDEX_TABLE}").fetchall()}
    assert ("expenditures", "spend_category") in cols
    assert ("expenditures", "fund") in cols
    assert ("expenditures", "payee_canonical") in cols
    assert ("salary_data", "jobTitle") in cols
    # The raw side of the canonical pair is noise the canonical column removes.
    assert ("expenditures", "payee") not in cols
    # Numeric columns are not vocabulary.
    assert ("expenditures", "fiscal_year") not in cols


def test_index_trims_and_merges_padded_variants_and_excludes_artifacts(con):
    rows = con.execute(
        f"SELECT val, weight, n FROM {g.VALUE_INDEX_TABLE} "
        "WHERE col = 'spend_category' AND val LIKE 'Automotive Fuel%'").fetchall()
    assert rows == [("Automotive Fuel", 504000.0, 2)]
    carpet = con.execute(
        f"SELECT weight FROM {g.VALUE_INDEX_TABLE} WHERE val = 'Carpet Replacement'").fetchone()[0]
    assert carpet == 50000.0  # the $224M artifact row does not weight the vocabulary


def test_pack_can_exclude_and_include_columns():
    c = duckdb.connect()
    c.execute("CREATE TABLE expenditures AS SELECT 'x' AS spend_category, 'GR001' AS grant_, 1.0 AS extended_amount, "
              "'y' AS fund UNION ALL SELECT 'z', 'GR002', 2.0, 'w'")
    g.build_value_index(c, _cfg({"exclude_columns": ["expenditures.grant_"]}))
    cols = {r[0] for r in c.execute(f"SELECT DISTINCT col FROM {g.VALUE_INDEX_TABLE}").fetchall()}
    assert "grant_" not in cols and "spend_category" in cols


# ── Question terms ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("word,stem", [
    ("vehicles", "vehicle"), ("salaries", "salary"), ("buses", "bus"),
    ("business", "business"), ("status", "status"), ("fire", "fire"), ("parks", "park"),
])
def test_singularize(word, stem):
    assert g.singularize(word) == stem


def test_question_terms_keep_content_words_and_drop_filler():
    q = "How much did the Louisville Fire department spend on vehicles in fiscal year 2024?"
    assert g.question_terms(q, _cfg({"stopwords": ["louisville"]})) == ["fire", "vehicle"]


def test_question_terms_keep_acronyms_and_quoted_phrases():
    assert "arp" in g.question_terms("How much ARP money was spent?")
    assert "yum center" in g.question_terms('Spending on the "Yum Center" arena')
    # A 3-letter lowercase word that is not an acronym is dropped ...
    assert "did" not in g.question_terms("what did the zoo buy")
    # ... but a 3-letter word with a synonym entry survives.
    assert "bus" in g.question_terms("how much on bus service")


# ── Lookup / block ───────────────────────────────────────────────────────────

def test_grounding_block_surfaces_the_real_category_family(con, monkeypatch):
    monkeypatch.setattr(g, "VALUES_PER_GROUP", 2)  # force the "to cover all of them" tail
    block = g.grounding_block(con, "How much did Louisville Fire spend on vehicles in FY2024?", _cfg())
    assert "Automotive Parts & Accessories" in block
    assert "also matched: " in block and "automotive" in block
    assert "spend_category ILIKE '%automotive%'" in block
    assert "'Louisville Fire'" in block  # the agency, from "fire"
    # Dollar tables show dollar weights; the salary table shows row counts.
    assert "$900.0K" in block


def test_short_tokens_match_on_word_boundaries(con):
    # 'arp' must find the ARP fund but not Carpet or SHARP.
    groups = g.lookup_terms(con, ["arp"], _cfg())
    vals = {v for grp in groups for v, _, _ in grp["values"]}
    assert "ARP" in vals
    assert not any("Carpet" in v or "SHARP" in v for v in vals)


def test_entity_columns_only_contribute_specific_matches(con, monkeypatch):
    monkeypatch.setattr(g, "ENTITY_COLUMN_DISTINCT", 5)      # payee_canonical has 9 values here
    monkeypatch.setattr(g, "ENTITY_COLUMN_MAX_MATCHES", 1)
    # "fire" matches one payee (SEAGRAVE FIRE ...): specific, so it is kept.
    groups = g.lookup_terms(con, ["fire"], _cfg())
    assert any(grp["column"] == "payee_canonical" for grp in groups)
    # A term hitting several payees is dropped from that column as noise.
    monkeypatch.setattr(g, "ENTITY_COLUMN_MAX_MATCHES", 0)
    groups = g.lookup_terms(con, ["fire"], _cfg())
    assert not any(grp["column"] == "payee_canonical" for grp in groups)


def test_block_is_empty_for_a_question_with_no_vocabulary(con):
    assert g.grounding_block(con, "Which agencies spend the most?", _cfg()) == ""


def test_block_respects_the_character_budget(con, monkeypatch):
    full = g.grounding_block(con, "fire vehicles housing paving mayor", _cfg()).split("\n", 2)[-1]
    monkeypatch.setattr(g, "BLOCK_CHAR_BUDGET", 200)
    body = g.grounding_block(con, "fire vehicles housing paving mayor", _cfg()).split("\n", 2)[-1]
    assert body.count("\n- ") < full.count("\n- ")
    # One line is always served; the budget bounds everything after it.
    first = body.split("\n")[0]
    assert len(body) <= max(200, len(first)) + 200


# ── Filter diagnosis ─────────────────────────────────────────────────────────

def test_sql_literal_filters_extracts_positive_string_predicates():
    sql = ("SELECT 1 FROM expenditures e WHERE e.agency_canonical = 'Louisville Fire' "
           "AND LOWER(fund) = 'arp' AND spend_category ILIKE '%Vehicle%' "
           "AND payee_canonical NOT LIKE '%INC%' AND region IN ('District 1', 'District 2') "
           "AND program <> 'x' AND payee_canonical = 'O''REILLY'")
    assert g.sql_literal_filters(sql) == [
        ("agency_canonical", "=", "Louisville Fire"),
        ("fund", "=", "arp"),
        ("spend_category", "ILIKE", "%Vehicle%"),
        ("region", "=", "District 1"),
        ("region", "=", "District 2"),
        ("payee_canonical", "=", "O'REILLY"),
    ]


def test_diagnosis_flags_a_literal_that_matches_nothing_with_close_values(con):
    d = g.diagnose_filters(con, "SELECT 1 FROM expenditures WHERE fund = 'ARPA'", _cfg())
    assert len(d) == 1 and d[0]["literal"] == "ARPA" and d[0]["matched"] == 0
    assert [v for v, _ in d[0]["suggestions"]] == ["ARP"]
    hint = g.format_repair_hint(d, _cfg())
    assert "fund = 'ARPA' matches 0 of" in hint and "'ARP'" in hint
    assert hint.rstrip().endswith("Return ONLY the SQL.")
    assert "Rewrite" not in g.format_repair_hint(d, _cfg(), instruct=False)


def test_diagnosis_flags_a_narrow_literal_beside_a_wider_family(con):
    """The production case: '%Vehicle%' IS a real substring, but the query was
    empty because this department's vehicle spend sits under 'Automotive ...'."""
    sql = ("SELECT SUM(extended_amount) FROM expenditures WHERE agency_canonical = 'Louisville Fire' "
           "AND fiscal_year = 2024 AND spend_category ILIKE '%Vehicle%'")
    d = g.diagnose_filters(con, sql, _cfg())
    assert [x["literal"] for x in d] == ["%Vehicle%"]
    assert d[0]["narrow"] and d[0]["matched"] >= 1
    vals = [v for v, _ in d[0]["suggestions"]]
    assert "Automotive Parts & Accessories" in vals and "Automotive Fuel" in vals
    hint = g.format_repair_hint(d, _cfg())
    assert "spend_category ILIKE '%automotive%'" in hint
    assert "OR spend_category ILIKE '%Vehicle%'" in hint


def test_diagnosis_is_silent_for_a_genuinely_empty_result(con):
    sql = "SELECT 1 FROM expenditures WHERE agency_canonical = 'Louisville Fire' AND fiscal_year = 2031"
    # The question named the year: the reader must be told the data stops
    # short, never handed an all-years figure with the year quietly dropped.
    assert g.diagnose_filters(con, sql, _cfg(), question="Fire spending in fiscal year 2031?") == []
    assert g.diagnose_filters(con, sql, _cfg(), question="Fire spending in FY31?") == []
    assert g.format_repair_hint([], _cfg()) == ""


def test_diagnosis_reports_a_case_only_mismatch(con):
    d = g.diagnose_filters(con, "SELECT 1 FROM expenditures WHERE agency_canonical = 'louisville fire'", _cfg())
    assert d[0]["case_only"] and [v for v, _ in d[0]["suggestions"]] == ["Louisville Fire"]
    assert "capitalization" in g.format_repair_hint(d, _cfg())


def test_diagnosis_finds_a_value_that_lives_in_another_column(con):
    d = g.diagnose_filters(con, "SELECT 1 FROM expenditures WHERE agency_canonical = 'Mayor'", _cfg())
    assert d and not d[0]["suggestions"]
    assert any(col == "jobTitle" and val == "Mayor" for _, col, val in d[0]["elsewhere"])
    assert "salary_data.jobTitle = 'Mayor'" in g.format_repair_hint(d, _cfg())


def test_diagnosis_ignores_unindexed_columns(con):
    assert g.diagnose_filters(con, "SELECT 1 FROM expenditures WHERE invoice_number = 'INV-1'", _cfg()) == []


def test_everything_degrades_to_nothing_without_an_index():
    c = duckdb.connect()
    c.execute("CREATE TABLE expenditures AS SELECT 'a' AS spend_category, 1.0 AS extended_amount")
    assert g.grounding_block(c, "vehicles", _cfg()) == ""
    assert g.diagnose_filters(c, "SELECT 1 FROM expenditures WHERE spend_category = 'x'", _cfg()) == []


def test_elsewhere_returns_table_column_value(con):
    """Regression guard for a DuckDB 1.5.1 planner bug: a `col <> ?` filter in
    the same SELECT as a window partitioned by (tbl, col) came back with the
    tbl and col VALUES swapped while the result description still said
    (tbl, col, val). The CTE shape in _elsewhere avoids it; this pins that."""
    rows = g._elsewhere(con, "agency_canonical", "Mayor", _cfg())
    assert rows, "expected the job title 'Mayor' to be found in another column"
    tables = {r[0] for r in rows}
    assert tables <= {"salary_data", "expenditures"}, rows
    assert ("salary_data", "jobTitle", "Mayor") in rows


def test_acronyms_and_capitalized_phrases_are_terms_in_their_own_right():
    cfg = _cfg({"synonyms": {"american rescue plan": ["arp"], "cares act": ["cares"]}})
    # A known phrase replaces its words: "plan" must not match 'Retirement Plan'.
    assert g.question_terms("How much has the city spent from the American Rescue Plan?", cfg) == ["american rescue plan"]
    terms = g.question_terms("How much CARES Act coronavirus relief money did the city spend?", cfg)
    assert terms[0] == "cares act" and "care" not in terms and "cares" not in terms  # phrase covers it
    # An unknown capitalized phrase is kept alongside its words.
    terms = g.question_terms("Spending on the Yum Center arena", _cfg())
    assert terms[0] == "yum center" and "center" in terms


def test_sql_tables_reads_from_and_join_targets():
    assert g.sql_tables("SELECT 1 FROM summary_grant_funding s JOIN expenditures e ON 1=1") == \
        ["summary_grant_funding", "expenditures"]
    assert g.sql_tables("WITH x AS (SELECT * FROM salary_data) SELECT * FROM x") == ["salary_data", "x"]


def test_diagnosis_is_scoped_to_the_tables_the_sql_reads(con):
    """'ARP' is a real fund in expenditures; a query that looks for it in a
    summary table that does not carry it must be told so — and pointed at
    the table that does."""
    con.execute("CREATE TABLE summary_grant_funding AS SELECT 'CDBG' AS fund, 1.0 AS total_amount "
                "UNION ALL SELECT 'HOME', 2.0")
    g.build_value_index(con, _cfg())
    d = g.diagnose_filters(con, "SELECT SUM(total_amount) FROM summary_grant_funding WHERE fund = 'ARP'", _cfg())
    assert d and d[0]["matched"] == 0 and d[0]["total"] == 2
    assert ("expenditures", "fund", "ARP") in d[0]["elsewhere"]
    assert "expenditures.fund = 'ARP'" in g.format_repair_hint(d, _cfg())
    # The same literal against the table that has it is not a diagnosis.
    assert g.diagnose_filters(con, "SELECT 1 FROM expenditures WHERE fund = 'ARP'", _cfg()) == []


def test_diagnosis_reports_a_year_the_value_has_no_rows_in(con):
    """fund = 'ARP' is real; FY2024 has no ARP rows in this fixture (ARP ran
    FY2023 only). The SQL pinned the year; the question may not have."""
    sql = "SELECT SUM(extended_amount) FROM expenditures WHERE fund = 'ARP' AND fiscal_year = 2024"
    # The model invented the year: the question never said 2024.
    d = g.diagnose_filters(con, sql, _cfg(), question="How much ARP money has the city spent?")
    assert len(d) == 1 and d[0]["year_gap"]
    # The reader asked for 2024: no repair, the emptiness is the answer.
    assert g.diagnose_filters(con, sql, _cfg(), question="How much ARP money was spent in 2024?") == []
    assert (d[0]["first"], d[0]["last"], d[0]["year"]) == (2023, 2023, 2024)
    hint = g.format_repair_hint(d, _cfg())
    assert "no rows in fiscal_year = 2024" in hint and "2023-2023" in hint
    # A year the value DOES have rows in is not a gap (the emptiness is real).
    assert g.diagnose_filters(
        con, "SELECT 1 FROM expenditures WHERE fund = 'ARP' AND fiscal_year = 2023 AND 1 = 0", _cfg()) == []
