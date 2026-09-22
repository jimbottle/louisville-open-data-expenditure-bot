"""Tests for refresh_data cache invalidation (louisville-open-data-hc5)."""
import refresh_data


def test_clear_response_cache_removes_the_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STATS_DIR", str(tmp_path))
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    cache = tmp_path / ".response_cache.json"
    cache.write_text('{"v:q": []}')
    refresh_data.clear_response_cache(str(tmp_path))
    assert not cache.exists(), "refresh must delete the stale response cache"


def test_pull_only_path_invalidates_the_cache(monkeypatch):
    """--pull-only must call clear_response_cache before returning: a pull-only
    refresh changes the data, so leaving the cache intact serves stale answers.
    Drive main() with every step stubbed and assert the cache clear ran."""
    called = {"cleared": False}
    monkeypatch.setattr(refresh_data, "pull_datasets", lambda *a, **k: True)
    monkeypatch.setattr(refresh_data, "clear_response_cache",
                        lambda *a, **k: called.__setitem__("cleared", True))
    monkeypatch.setattr("sys.argv", ["refresh_data.py", "--pull-only", "-o", "/tmp"])
    refresh_data.main()
    assert called["cleared"], "--pull-only returned without invalidating the cache"


def test_cache_clear_targets_lou_api_base_without_a_body(tmp_path, monkeypatch):
    """The scheduled AWS build clears the CloudFront deployment's cache; a
    DELETE with a body would need the OAC body-hash header, so none is sent
    and the endpoint's no-body branch (clear all) is what runs."""
    import refresh_data
    import requests
    seen = {}

    def fake_delete(url, **kw):
        seen.update(url=url, kw=kw)
        class R: status_code = 200
        return R()

    monkeypatch.setattr(requests, "delete", fake_delete)
    monkeypatch.setenv("ADMIN_TOKEN", "t0k")
    monkeypatch.setenv("LOU_API_BASE", "https://example.cloudfront.net/")
    refresh_data.clear_response_cache(str(tmp_path))
    assert seen["url"] == "https://example.cloudfront.net/api/cache"
    assert "json" not in seen["kw"] and "data" not in seen["kw"]
    assert seen["kw"]["headers"] == {"X-Admin-Token": "t0k"}


def test_main_exit_code_reflects_failed_steps(monkeypatch):
    """The unattended build must not deploy a partial refresh: a failed pull
    (or profiles/ingest) makes main() return 1, which sys.exit turns into a
    failed CodeBuild phase and a lou-alerts notification."""
    import refresh_data
    monkeypatch.setattr(refresh_data, "clear_response_cache", lambda *a, **k: None)
    monkeypatch.setattr(refresh_data, "pull_datasets", lambda *a, **k: False)
    monkeypatch.setattr("sys.argv", ["refresh_data.py", "--pull-only", "-o", "/tmp"])
    assert refresh_data.main() == 1
    monkeypatch.setattr(refresh_data, "pull_datasets", lambda *a, **k: True)
    assert refresh_data.main() == 0
    # Full path: a failing ingest after a good pull is still a failure.
    monkeypatch.setattr(refresh_data, "build_profiles", lambda *a, **k: True)
    monkeypatch.setattr(refresh_data, "scrape_officers", lambda *a, **k: True)
    monkeypatch.setattr(refresh_data, "ingest_documents", lambda *a, **k: False)
    monkeypatch.setattr("sys.argv", ["refresh_data.py", "--skip-graph", "-o", "/tmp"])
    assert refresh_data.main() == 1
