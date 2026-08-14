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
