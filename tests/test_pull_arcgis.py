"""Pagination + atomic-write tests for pull_arcgis (louisville-open-data-l9a).

The bug: offsets advanced by the REQUESTED batch_size with a precomputed page
count, so when a hosted layer caps its page size below --batch-size, records
past the first page were silently skipped and a partial CSV was written.
"""
import json

import pull_arcgis


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
