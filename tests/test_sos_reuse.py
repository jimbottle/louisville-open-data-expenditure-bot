"""SOS result reuse in graph/build_contractor_profiles.py (louisville-open-data-rm4).

The monthly lou-refresh re-ran ~150 polite 1.5 s KY SOS lookups every time;
recent results from the previous profiles CSV are now reused."""
import datetime as dt
import importlib.util
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "build_contractor_profiles", os.path.join(ROOT, "graph", "build_contractor_profiles.py"))
bcp = importlib.util.module_from_spec(_spec)
sys.path.insert(0, ROOT)
_spec.loader.exec_module(bcp)


def _write(path, rows):
    pd.DataFrame(rows).to_csv(path, index=False)


def test_recent_rows_are_reused_and_stale_rows_are_not(tmp_path):
    today = dt.date.today()
    p = tmp_path / "profiles.csv"
    _write(p, [
        {"payee": "Fresh LLC", "sos_registered_agent": "CT Corp", "sos_org_number": "1",
         "sos_officers": "A. Person (President)", "sos_looked_up": (today - dt.timedelta(days=10)).isoformat()},
        {"payee": "Stale Inc", "sos_registered_agent": "x", "sos_org_number": "2",
         "sos_officers": "", "sos_looked_up": (today - dt.timedelta(days=400)).isoformat()},
        {"payee": "Miss Co", "sos_registered_agent": "", "sos_org_number": "",
         "sos_officers": "", "sos_looked_up": (today - dt.timedelta(days=3)).isoformat()},
    ])
    prior = bcp.load_prior_sos(str(p), max_age_days=120)
    assert set(prior) == {"Fresh LLC", "Miss Co"}, "stale row must be re-fetched; a recent miss is still a lookup"
    assert prior["Fresh LLC"]["sos_officers"] == "A. Person (President)"
    assert prior["Fresh LLC"]["sos_org_number"] == "1"


def test_legacy_file_without_date_column_is_dated_by_mtime(tmp_path):
    p = tmp_path / "profiles.csv"
    _write(p, [{"payee": "Old Corp", "sos_registered_agent": "x", "sos_org_number": "9"}])
    assert "Old Corp" in bcp.load_prior_sos(str(p), max_age_days=120)
    # Age the file past the window.
    old = (dt.datetime.now() - dt.timedelta(days=200)).timestamp()
    os.utime(p, (old, old))
    assert bcp.load_prior_sos(str(p), max_age_days=120) == {}


def test_reuse_disabled_or_missing_file():
    assert bcp.load_prior_sos("", 120) == {}
    assert bcp.load_prior_sos("/nonexistent/profiles.csv", 120) == {}


def test_reuse_requires_sos_columns(tmp_path):
    p = tmp_path / "profiles.csv"
    _write(p, [{"payee": "No SOS Inc", "total_spend": 1}])
    assert bcp.load_prior_sos(str(p), 120) == {}
