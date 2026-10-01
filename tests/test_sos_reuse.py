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


def _git(cwd, *args, env=None):
    import subprocess
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env)


def _commit_dated(repo, path, when):
    """Commit `path` with both author and committer dates set to `when`."""
    env = dict(os.environ, GIT_AUTHOR_DATE=f"{when.isoformat()}T12:00:00",
               GIT_COMMITTER_DATE=f"{when.isoformat()}T12:00:00",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
    _git(repo, "add", os.path.basename(path))
    _git(repo, "commit", "-q", "-m", "profiles", env=env)


def test_undated_rows_age_by_git_commit_date_not_mtime(tmp_path):
    """A fresh clone gives every file today's mtime, so mtime would make an
    undated row look fresh forever (roborev 5148). The last commit touching
    the file is the latest the data can have been fetched."""
    repo = tmp_path
    _git(repo, "init", "-q")
    p = repo / "profiles.csv"
    _write(p, [{"payee": "Old Corp", "sos_registered_agent": "x", "sos_org_number": "9"}])
    recent = dt.date.today() - dt.timedelta(days=30)
    _commit_dated(repo, p, recent)
    # mtime is "now" (as after a clone) but the commit is 30 days old: reused,
    # and stamped with the commit date, not today.
    prior = bcp.load_prior_sos(str(p), max_age_days=120)
    assert prior["Old Corp"]["sos_looked_up"] == recent.isoformat()

    # Re-commit the same content with an old date: stale, not reused.
    _write(p, [{"payee": "Old Corp", "sos_registered_agent": "x", "sos_org_number": "9", "note": "v2"}])
    _commit_dated(repo, p, dt.date.today() - dt.timedelta(days=200))
    assert bcp.load_prior_sos(str(p), max_age_days=120) == {}


def test_undated_rows_without_git_history_are_stale(tmp_path):
    p = tmp_path / "profiles.csv"   # tmp_path is not a git checkout
    _write(p, [{"payee": "Old Corp", "sos_registered_agent": "x", "sos_org_number": "9"}])
    assert bcp.load_prior_sos(str(p), max_age_days=120) == {}, "mtime must never date an undated row"


def test_failed_lookup_is_distinguished_from_no_match(monkeypatch):
    """A timeout during a rebuild must not be recorded as a looked-up miss
    that --reuse-sos then skips for 120 days (roborev 5148)."""
    def boom(name, session):
        raise TimeoutError("sos down")
    monkeypatch.setattr(bcp, "sos_search", boom)
    assert bcp.lookup_entity("Any LLC", None) is None
    monkeypatch.setattr(bcp, "sos_search", lambda name, session: [])
    assert bcp.lookup_entity("Any LLC", None) == {}


def test_reuse_disabled_or_missing_file():
    assert bcp.load_prior_sos("", 120) == {}
    assert bcp.load_prior_sos("/nonexistent/profiles.csv", 120) == {}


def test_reuse_requires_sos_columns(tmp_path):
    p = tmp_path / "profiles.csv"
    _write(p, [{"payee": "No SOS Inc", "total_spend": 1}])
    assert bcp.load_prior_sos(str(p), 120) == {}


def test_shallow_clone_never_dates_an_undated_row(tmp_path):
    """The refresh build used `git clone --depth 1`; in a shallow clone
    `git log -1 -- file` reports HEAD's date for every file, so an undated
    row looked fresh on every run (roborev 5149). A shallow checkout must
    treat undated rows as stale."""
    src = tmp_path / "src"; src.mkdir()
    _git(src, "init", "-q")
    p = src / "profiles.csv"
    _write(p, [{"payee": "Old Corp", "sos_registered_agent": "x", "sos_org_number": "9"}])
    _commit_dated(src, p, dt.date.today() - dt.timedelta(days=200))
    (src / "other.txt").write_text("later work\n")
    _commit_dated(src, src / "other.txt", dt.date.today() - dt.timedelta(days=1))
    # Full clone: the file's real commit is 200 days old -> stale.
    full = tmp_path / "full"
    _git(tmp_path, "clone", "-q", f"file://{src}", str(full))
    assert bcp.load_prior_sos(str(full / "profiles.csv"), 120) == {}
    # Shallow clone: HEAD is 1 day old and git log would say so for the CSV.
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{src}", str(shallow))
    assert _git(shallow, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
    assert bcp.load_prior_sos(str(shallow / "profiles.csv"), 120) == {}, \
        "undated row dated by the shallow clone's HEAD"


def test_blank_stamp_in_a_stamped_file_is_refetched(tmp_path):
    """A failed lookup leaves sos_looked_up blank. When that file is reused,
    the blank row must be re-fetched, not dated by the commit and reused as a
    miss for 120 days (roborev 5149)."""
    repo = tmp_path
    _git(repo, "init", "-q")
    p = repo / "profiles.csv"
    today = dt.date.today()
    _write(p, [
        {"payee": "Fresh LLC", "sos_registered_agent": "a", "sos_org_number": "1", "sos_looked_up": today.isoformat()},
        {"payee": "Failed Inc", "sos_registered_agent": "", "sos_org_number": "", "sos_looked_up": ""},
        {"payee": "Garbage Co", "sos_registered_agent": "", "sos_org_number": "", "sos_looked_up": "not-a-date"},
    ])
    _commit_dated(repo, p, today)   # a commit date that WOULD be fresh if used
    prior = bcp.load_prior_sos(str(p), 120)
    assert set(prior) == {"Fresh LLC"}
