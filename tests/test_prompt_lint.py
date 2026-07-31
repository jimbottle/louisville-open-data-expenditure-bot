"""Prompt lint: system prompts must never QUOTE model-voice failure text.

Twice in production the model reproduced the exact phrase a prompt quoted as
forbidden (a hallucinated SQL filter, then a compensation claim) — negative
examples read as templates. Prohibitions must be stated positively. This
scans the prompt-bearing sources for the known past offenders so the bug
class can't be silently reintroduced by a future prompt edit.
"""

import glob
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Exact strings that previously leaked from prompts into answers.
PAST_OFFENDERS = [
    '"plus benefits"',
    "agency_canonical = 'Louisville Metro'",
]

# Everything whose text reaches a system prompt: the two prompt-building
# modules plus every city pack (data_facts is injected verbatim).
PROMPT_SOURCES = ["app.py", "analytics_agent.py"] + sorted(
    os.path.relpath(p, REPO) for p in glob.glob(os.path.join(REPO, "cities", "*", "city.yaml"))
)


def test_no_quoted_model_voice_anti_phrases_in_prompt_sources():
    assert any("city.yaml" in s for s in PROMPT_SOURCES)
    for fname in PROMPT_SOURCES:
        src = open(os.path.join(REPO, fname)).read()
        for offender in PAST_OFFENDERS:
            assert offender not in src, (
                f"{fname} contains the quoted anti-phrase {offender!r} — state the "
                "prohibition positively instead; models imitate quoted failure text"
            )
