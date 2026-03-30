"""
Known-answer test suite for Louisville expenditure bot.

Run: python -m pytest tests/test_known_answers.py -v
"""

import pytest
from data_model import load_all_data

@pytest.fixture(scope="module")
def con():
    return load_all_data("data")


def test_top_agency_is_public_works(con):
    r = con.execute("SELECT agency FROM summary_agency_spend LIMIT 1").fetchone()
    assert r[0] == "Public Works & Assets"


def test_top_agency_spend_over_1b(con):
    r = con.execute("SELECT total_spend FROM summary_agency_spend LIMIT 1").fetchone()
    assert r[0] > 1_000_000_000


def test_19_fiscal_years(con):
    r = con.execute("SELECT COUNT(*) FROM summary_annual_spend").fetchone()
    assert r[0] == 19


def test_annual_spend_range(con):
    r = con.execute("SELECT MIN(total_spend), MAX(total_spend) FROM summary_annual_spend").fetchone()
    assert r[0] > 200_000_000  # lowest year > $200M
    assert r[1] < 700_000_000  # highest year < $700M


def test_peak_spending_year_is_2025(con):
    r = con.execute("SELECT fiscal_year FROM summary_annual_spend ORDER BY total_spend DESC LIMIT 1").fetchone()
    assert r[0] == 2025


def test_largest_payment_not_susteen(con):
    """SUSTEEN $224M was a data artifact — it should not appear in largest payments."""
    r = con.execute("SELECT payee FROM summary_largest_payments LIMIT 1").fetchone()
    assert r[0] != "SUSTEEN INC"


def test_largest_payment_is_arena_authority(con):
    r = con.execute("SELECT payee, invoice_amount FROM summary_largest_payments LIMIT 1").fetchone()
    assert r[0] == "LOUISVILLE ARENA AUTHORITY INC"
    assert r[1] == 12_000_000.00


def test_top_salary_is_police_chief(con):
    r = con.execute("SELECT job_title FROM summary_top_salaries LIMIT 1").fetchone()
    assert r[0] == "Police Chief"


def test_expenditure_types_exist(con):
    r = con.execute("SELECT DISTINCT expenditure_type FROM summary_expenditure_type").fetchall()
    types = {row[0] for row in r}
    assert "Operating" in types or "Metro Government Operations" in types


def test_data_artifacts_flagged(con):
    r = con.execute("SELECT COUNT(*) FROM expenditures WHERE is_data_artifact = TRUE").fetchone()
    assert r[0] == 2  # the SUSTEEN pair


def test_agency_normalization_no_duplicates(con):
    """Public Works should be one canonical entry, not two."""
    r = con.execute("SELECT COUNT(*) FROM summary_agency_spend WHERE agency LIKE '%Public Works%'").fetchone()
    assert r[0] == 1


def test_total_expenditure_rows(con):
    r = con.execute("SELECT COUNT(*) FROM expenditures").fetchone()
    assert r[0] > 2_200_000
