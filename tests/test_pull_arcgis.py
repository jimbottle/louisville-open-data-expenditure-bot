"""Pagination + atomic-write tests for pull_arcgis (louisville-open-data-l9a).

The bug: offsets advanced by the REQUESTED batch_size with a precomputed page
count, so when a hosted layer caps its page size below --batch-size, records
past the first page were silently skipped and a partial CSV was written.
"""
import json

import pytest

import pull_arcgis


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    """The pagination tests fetch dozens of fake pages; the real token bucket
    (100/min) would make them sleep. The limiter has its own tests below."""
    monkeypatch.setattr(pull_arcgis, "DEFAULT_PAGES_PER_MINUTE", 0)


def _make_capped_server(total, server_cap):
    """A fake ArcGIS query endpoint that never returns more than server_cap rows
    per page, regardless of the requested resultRecordCount, and sets
    exceededTransferLimit until the last row is served."""
    def fake_fetch_json(url, params, retries=3):
        offset = params["resultOffset"]
        want = params["resultRecordCount"]
        take = max(0, min(want, server_cap, total - offset))
        feats = [{"attributes": {"id": i}} for i in range(offset, offset + take)]
        return {"features": feats, "exceededTransferLimit": (offset + take) < total}
    return fake_fetch_json


def test_pull_records_fetches_all_when_server_caps_below_batch_size(monkeypatch):
    total, server_cap = 2500, 1000
    monkeypatch.setattr(pull_arcgis, "get_record_count", lambda *a, **k: total)
    monkeypatch.setattr(pull_arcgis, "fetch_json", _make_capped_server(total, server_cap))

    # batch_size deliberately far above the server's cap — the exact trigger.
    records = pull_arcgis.pull_records("http://x/FeatureServer/0", batch_size=5000)

    assert len(records) == total, "records were skipped when the server capped page size"
    # No gaps or dupes: exactly ids 0..total-1.
    assert sorted(r["id"] for r in records) == list(range(total))


def test_pull_records_stops_cleanly_on_exact_multiple(monkeypatch):
    total, server_cap = 2000, 1000  # total is an exact multiple of the cap
    monkeypatch.setattr(pull_arcgis, "get_record_count", lambda *a, **k: total)
    monkeypatch.setattr(pull_arcgis, "fetch_json", _make_capped_server(total, server_cap))
    records = pull_arcgis.pull_records("http://x/FeatureServer/0", batch_size=1000)
    assert len(records) == total
    assert sorted(r["id"] for r in records) == list(range(total))


def test_pull_records_handles_empty_result(monkeypatch):
    def _must_not_be_called(*a, **k):
        raise AssertionError("fetch_json should not be called when total is 0")
    monkeypatch.setattr(pull_arcgis, "get_record_count", lambda *a, **k: 0)
    monkeypatch.setattr(pull_arcgis, "fetch_json", _must_not_be_called)
    assert pull_arcgis.pull_records("http://x/FeatureServer/0") == []


def test_save_data_is_atomic_and_leaves_no_part_file(tmp_path):
    records = [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]
    out = pull_arcgis.save_data(records, str(tmp_path), "thing", "csv")
    assert out.endswith("thing.csv")
    assert (tmp_path / "thing.csv").exists()
    assert not (tmp_path / "thing.csv.part").exists(), "temp .part file was left behind"
    # Round-trips.
    import pandas as pd
    df = pd.read_csv(out)
    assert list(df["id"]) == [1, 2]


def test_save_data_json_is_atomic(tmp_path):
    records = [{"id": 1}, {"id": 2}]
    out = pull_arcgis.save_data(records, str(tmp_path), "thing", "json")
    assert out.endswith("thing.ndjson")
    assert not (tmp_path / "thing.ndjson.part").exists()
    lines = (tmp_path / "thing.ndjson").read_text().splitlines()
    assert [json.loads(x)["id"] for x in lines] == [1, 2]


def test_pull_records_aborts_on_non_paginating_server(monkeypatch):
    """A layer that ignores resultOffset (supportsPagination=false) returns the
    same page forever with exceededTransferLimit=true. The loop must abort
    instead of spinning and appending duplicates without bound (louisville-open-data l9a/3681)."""
    import pytest
    total = 2000
    def ignores_offset(url, params, retries=3):
        # Always returns the SAME first 1000 rows, always claims there's more.
        feats = [{"attributes": {"id": i}} for i in range(1000)]
        return {"features": feats, "exceededTransferLimit": True}
    monkeypatch.setattr(pull_arcgis, "get_record_count", lambda *a, **k: total)
    monkeypatch.setattr(pull_arcgis, "fetch_json", ignores_offset)
    with pytest.raises(RuntimeError, match="pagination not honored"):
        pull_arcgis.pull_records("http://x/FeatureServer/0", batch_size=1000)


# ── concurrent paging (louisville-open-data-rm4) ─────────────────────────────
# The 2026-10-01 lou-refresh timed out mid-pull when the source served pages
# ~6x slower than usual; pages are now fetched in a parallel window but must be
# committed in offset order so the output is identical to the serial pull.

def test_parallel_pull_matches_serial_and_keeps_order(monkeypatch):
    total, server_cap = 10_500, 1000
    monkeypatch.setattr(pull_arcgis, "get_record_count", lambda *a, **k: total)
    monkeypatch.setattr(pull_arcgis, "fetch_json", _make_capped_server(total, server_cap))
    serial = pull_arcgis.pull_records("http://x/FeatureServer/0", batch_size=1000, workers=1)
    parallel = pull_arcgis.pull_records("http://x/FeatureServer/0", batch_size=1000, workers=4)
    assert [r["id"] for r in parallel] == [r["id"] for r in serial] == list(range(total))


def test_parallel_pull_replans_after_a_short_page(monkeypatch):
    """If a page inside the window comes back short (server shrank its cap
    mid-pull), the later pages of that window were laid out on stale offsets
    and must be dropped, not committed with a gap."""
    total = 7_000
    calls = []

    def shrinking_server(url, params, retries=3):
        offset = params["resultOffset"]
        calls.append(offset)
        cap = 1000 if offset < 2000 else 500      # cap drops after the 2nd page
        take = max(0, min(params["resultRecordCount"], cap, total - offset))
        feats = [{"attributes": {"id": i}} for i in range(offset, offset + take)]
        return {"features": feats, "exceededTransferLimit": (offset + take) < total}

    monkeypatch.setattr(pull_arcgis, "get_record_count", lambda *a, **k: total)
    monkeypatch.setattr(pull_arcgis, "fetch_json", shrinking_server)
    records = pull_arcgis.pull_records("http://x/FeatureServer/0", batch_size=1000, workers=4)
    assert [r["id"] for r in records] == list(range(total)), "gap or duplicate after the cap shrank"
    # The window re-plans on the NEW page size: 2 pages of 1000 + 10 of 500
    # is 12 useful requests; one wasted window at most, not workers-1 wasted
    # requests per window for the rest of the pull (roborev 5148).
    assert len(calls) <= 12 + 4 + 4, f"{len(calls)} requests for 12 pages: window not re-planned on the shrunk cap"


def test_parallel_pull_aborts_on_non_paginating_server(monkeypatch):
    import pytest
    def ignores_offset(url, params, retries=3):
        return {"features": [{"attributes": {"id": i}} for i in range(1000)],
                "exceededTransferLimit": True}
    monkeypatch.setattr(pull_arcgis, "get_record_count", lambda *a, **k: 2000)
    monkeypatch.setattr(pull_arcgis, "fetch_json", ignores_offset)
    with pytest.raises(RuntimeError, match="pagination not honored"):
        pull_arcgis.pull_records("http://x/FeatureServer/0", batch_size=1000, workers=4)


def test_workers_flag_defaults_from_env(monkeypatch):
    import importlib
    monkeypatch.setenv("ARCGIS_WORKERS", "3")
    mod = importlib.reload(pull_arcgis)
    try:
        assert mod.DEFAULT_WORKERS == 3
    finally:
        monkeypatch.delenv("ARCGIS_WORKERS")
        importlib.reload(pull_arcgis)


# ── ArcGIS Online request-unit quota (lou-refresh #5, 2026-10-01) ────────────

def test_quota_429_waits_for_retry_after_and_retries(monkeypatch):
    """ArcGIS answers HTTP 200 with a JSON-level 429 when the per-minute
    request-unit budget is spent. That is a stall, not a failure: sleep for
    the server's Retry-after and try again."""
    sleeps = []
    monkeypatch.setattr(pull_arcgis.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    class Resp:
        def __init__(self, payload): self._p = payload
        def raise_for_status(self): pass
        def json(self): return self._p

    quota = {"error": {"code": 429, "message": "Unable to perform query. Too many requests.",
                       "details": ["API calls quota exceeded (6044 request units)! maximum allowed "
                                   "request units (6000) per Minute. Retry after 60 sec."]}}
    ok = {"features": [{"attributes": {"id": 1}}], "exceededTransferLimit": False}

    class Sess:
        def get(self, url, params=None, timeout=None):
            calls["n"] += 1
            return Resp(quota if calls["n"] <= 2 else ok)
    monkeypatch.setattr(pull_arcgis, "_session", lambda: Sess())
    assert pull_arcgis.fetch_json("http://x/query", {}) == ok
    assert calls["n"] == 3
    assert sleeps == [60, 60], "must honour the server's Retry-after, not the 2 s network backoff"


def test_quota_429_gives_up_after_quota_retries(monkeypatch):
    import pytest
    monkeypatch.setattr(pull_arcgis.time, "sleep", lambda s: None)
    monkeypatch.setattr(pull_arcgis, "QUOTA_RETRIES", 3)

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"error": {"code": 429, "message": "Too many requests.", "details": []}}

    class Sess:
        def get(self, url, params=None, timeout=None): return Resp()
    monkeypatch.setattr(pull_arcgis, "_session", lambda: Sess())
    with pytest.raises(RuntimeError, match="ArcGIS error"):
        pull_arcgis.fetch_json("http://x/query", {})


def test_other_arcgis_errors_still_raise_immediately(monkeypatch):
    import pytest
    sleeps = []
    monkeypatch.setattr(pull_arcgis.time, "sleep", lambda s: sleeps.append(s))

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"error": {"code": 400, "message": "Invalid query parameters", "details": []}}

    class Sess:
        def get(self, url, params=None, timeout=None): return Resp()
    monkeypatch.setattr(pull_arcgis, "_session", lambda: Sess())
    with pytest.raises(RuntimeError, match="Invalid query"):
        pull_arcgis.fetch_json("http://x/query", {})
    assert sleeps == []


def test_rate_limiter_caps_pages_per_minute(monkeypatch):
    """100 pages/min with a burst of 10: the 11th acquisition must wait ~0.6 s
    and 60 acquisitions must spread over ~30 s of (simulated) time."""
    clock = {"t": 0.0}
    slept = []
    monkeypatch.setattr(pull_arcgis.time, "monotonic", lambda: clock["t"])
    def fake_sleep(s):
        slept.append(s); clock["t"] += s
    monkeypatch.setattr(pull_arcgis.time, "sleep", fake_sleep)
    lim = pull_arcgis.RateLimiter(100)
    for _ in range(10):
        assert lim.acquire() == 0.0
    assert abs(lim.acquire() - 0.6) < 1e-6
    for _ in range(49):
        lim.acquire()
    assert abs(clock["t"] - 30.0) < 1e-6, f"60 pages took {clock['t']:.1f}s of simulated time, expected 30"


def test_rate_limiter_disabled_at_zero():
    lim = pull_arcgis.RateLimiter(0)
    assert all(lim.acquire() == 0.0 for _ in range(100))


def test_pull_records_passes_through_the_limiter(monkeypatch):
    total, cap = 3000, 1000
    monkeypatch.setattr(pull_arcgis, "get_record_count", lambda *a, **k: total)
    monkeypatch.setattr(pull_arcgis, "fetch_json", _make_capped_server(total, cap))
    acquired = []
    class Spy(pull_arcgis.RateLimiter):
        def acquire(self):
            acquired.append(1); return 0.0
    monkeypatch.setattr(pull_arcgis, "RateLimiter", Spy)
    records = pull_arcgis.pull_records("http://x/FeatureServer/0", batch_size=1000, workers=2)
    assert len(records) == total
    assert len(acquired) >= 3, "every page request must take a token"
