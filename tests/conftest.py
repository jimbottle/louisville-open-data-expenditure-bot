"""Shared fixtures. Lets the suite run from a clean clone / in CI where the
golden expenditure CSVs (gitignored) are absent: data-bound tests skip with a
clear reason instead of erroring at collection, while every pure-function and
in-memory-DuckDB test still runs."""
import glob
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def expenditure_data_present(data_dir: str | None = None) -> bool:
    """True when the real per-year expenditure CSVs are on disk."""
    d = data_dir or os.environ.get("DATA_DIR", "data")
    if not os.path.isabs(d):
        d = os.path.join(REPO, d)
    return bool(glob.glob(os.path.join(d, "eExpenditures_*.csv")))


# Which provider is primary — and therefore which model ids a deprecation
# resolves against — is decided by ambient env vars. A developer with real keys
# exported (or a run on the deploy host, which has them) would otherwise get
# different results from the same code: the model-fallback tests resolve
# OpenRouter slugs against a Cerebras-shaped fake catalogue and fail. Tests that
# want a provider set it explicitly with monkeypatch.
PROVIDER_ENV = (
    "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "OPENROUTER_MODEL",
    "CEREBRAS_API_KEY", "CEREBRAS_PAID_API_KEY", "GEMINI_API_KEY",
    "MODEL", "MODEL_FALLBACKS", "LLM_BASE_URL",
)


@pytest.fixture(autouse=True)
def _neutral_provider_env(monkeypatch):
    for var in PROVIDER_ENV:
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture(scope="session")
def require_data():
    """For tests that go through the app, which loads DATA_DIR (default 'data').
    Skips when that directory has no expenditure CSVs."""
    if not expenditure_data_present(os.environ.get("DATA_DIR", "data")):
        pytest.skip("real expenditure CSVs not present (gitignored) — data-bound test skipped")


@pytest.fixture(scope="session")
def require_louisville_data():
    """For the known-answer suite, whose `con` hardcodes load_all_data('data')
    and whose golden answers are Louisville-specific. Gates on 'data'
    EXPLICITLY (not DATA_DIR), so a lingering DATA_DIR=data_cincinnati can
    neither skip these tests while Louisville data sits in ./data, nor let them
    run and then error inside load_all_data('data')."""
    if not expenditure_data_present("data"):
        pytest.skip("Louisville expenditure CSVs not present in ./data (gitignored) — skipped")
