"""warm_cache.fetch_entries: it has been rewritten three times (unversioned ->
strip-prefix -> version-filter) and each rewrite shipped a reporting bug that
a later review caught. Both branches are pinned here."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warm_cache  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _patch(monkeypatch, payload):
    monkeypatch.setattr(warm_cache, "requests",
                        type("R", (), {"get": staticmethod(lambda *a, **k: _Resp(payload))}))


def test_only_current_version_entries_count(monkeypatch):
    _patch(monkeypatch, {"cache_version": "v2", "entries": {
        "v2:live question": {"has_interpretation": True},
        "v1:live question": {"has_interpretation": True},   # stale duplicate
        "v1:old question": {"has_interpretation": True},    # stale only
    }})
    entries = warm_cache.fetch_entries("http://x")
    assert entries == {"live question": {"has_interpretation": True}}
    # a stale-only question must look UNCACHED so the warm loop re-warms it
    assert "old question" not in entries


def test_stale_duplicate_does_not_shadow_current(monkeypatch):
    # the stale entry is listed last; version filtering must still pick v2's
    _patch(monkeypatch, {"cache_version": "v2", "entries": {
        "v2:q": {"events": 10},
        "v1:q": {"events": 999},
    }})
    assert warm_cache.fetch_entries("http://x")["q"]["events"] == 10


def test_falls_back_to_prefix_strip_for_old_servers(monkeypatch):
    _patch(monkeypatch, {"entries": {"abc123:q": {"events": 5}}})  # no cache_version
    assert warm_cache.fetch_entries("http://x") == {"q": {"events": 5}}


def test_empty_cache(monkeypatch):
    _patch(monkeypatch, {"cache_version": "v2", "entries": {}})
    assert warm_cache.fetch_entries("http://x") == {}
