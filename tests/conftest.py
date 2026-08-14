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


@pytest.fixture(scope="session")
def require_data():
    """Skip the requesting test when the golden dataset isn't available."""
    if not expenditure_data_present():
        pytest.skip("real expenditure CSVs not present (gitignored) — data-bound test skipped")
