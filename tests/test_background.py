"""General background for explanatory questions (louisville-open-data-03r):
which questions get one, and the mechanical checks that withhold a note."""
import pytest

from analytics_agent import is_explanatory, validate_background


@pytest.mark.parametrize("q,expected", [
    ("why are there so many zero value roles?", True),
    ("Why did LMPD spending jump in 2024?", True),
    ("How come parks spending fell?", True),
    ("Explain the Fire Settlement job code", True),
    ("What does 'is_data_artifact' mean?", True),
    ("What is the reason for the FY2023 dip?", True),
    ("How does the capital budget work?", True),
    ("What are the highest paid positions in Louisville Metro government?", False),
    ("How much does the mayor make?", False),
    ("Give me a year-over-year breakdown of LMPD spending", False),
    ("", False),
])
def test_is_explanatory(q, expected):
    assert is_explanatory(q) is expected


GOOD = ("Public payroll extracts often keep records for people who left during the "
        "year or who hold unpaid roles such as board seats, so a job title can appear "
        "with no pay attached.")


def test_a_general_note_passes():
    assert validate_background(GOOD, ["Louisville"]) == (GOOD, None)


@pytest.mark.parametrize("text,reason", [
    ("NONE", "none"),
    ("none.", "none"),
    ("", "none"),
    ("Settlements in 2024 are common.", "contained figures"),
    ("Such roles often pay $0.", "contained figures"),
    ("About 5 percent of rows are adjustments.", "contained figures"),
    ("Roughly half is typical, 50% or so.", "contained figures"),
    ("In Louisville these are settlement codes.", "named the city"),
    ("x" * 1201, "too long"),
])
def test_notes_that_break_a_rule_are_withheld(text, reason):
    assert validate_background(text, ["Louisville"]) == (None, reason)
