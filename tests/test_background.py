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


def test_state_code_matches_whole_words_only():
    assert validate_background("Rules differ in KY.", ["KY"]) == (None, "named the city")
    ok = "Budgets often look to the sky for rain."
    assert validate_background(ok, ["KY"]) == (ok, None)


def test_pack_blocked_names_reach_the_validator():
    import app
    names = app._city_names()
    for n in ("Louisville", "KY", "Kentucky", "Jefferson County", "LMPD"):
        assert n in names
    assert validate_background("Jefferson County pensions are common.", names)[1] == "named the city"


class _FakeCompletions:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    def create(self, **kw):
        b = self.behaviour
        if isinstance(b, Exception):
            raise b
        class _Msg: content = b
        class _Choice: message = _Msg(); finish_reason = "stop"
        class _Resp: choices = [_Choice()]; usage = None
        return _Resp()


class _FakeClient:
    def __init__(self, behaviour):
        self.seen = []
        self.chat = type("C", (), {"completions": _FakeCompletions(behaviour)})()

    def with_options(self, **kw):
        self.seen.append(kw)
        return self


def test_generate_background_is_one_try_per_provider_paid_first():
    from analytics_agent import generate_background
    paid = _FakeClient(RuntimeError("402"))
    free = _FakeClient("Payroll systems often keep unpaid roles.")
    text, usage, tier = generate_background([(paid, "m1", "paid"), (free, "m2", "openrouter")], "why?", "a")
    assert tier == "openrouter" and text.startswith("Payroll")
    # No retry ladder: every call is made with retries off and a bounded timeout.
    for kw in paid.seen + free.seen:
        assert kw["max_retries"] == 0 and 0 < kw["timeout"] <= 15


def test_generate_background_gives_up_when_the_budget_is_spent():
    from analytics_agent import generate_background
    with pytest.raises(Exception):
        generate_background([(_FakeClient("x"), "m", "paid")], "why?", "a", budget=0.5)
