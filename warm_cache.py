#!/usr/bin/env python3
"""
Warm the response cache for all starter questions.
Run after a data refresh or container restart.

Usage:
    python warm_cache.py [--host http://localhost:8000] [--delay 10]
"""

import argparse
import os
import sys
import requests
import time

# Questions warmed BEYOND the active city pack's starter chips — asked often
# but not shown as chips. The chip questions themselves are read from the pack
# (below) so a chip edit in city.yaml auto-warms without touching this file;
# duplicating them here was the drift risk. Keep this list to whatever extra
# questions are worth pre-warming.
EXTRA_QUESTIONS = [
    "Which vendors receive payments from the most different agencies?",
    "Are there any patterns that suggest potential contract splitting?",
    "What does Louisville Metro spend on technology and cybersecurity?",
]


def _pack_chip_questions() -> list:
    """The active city pack's starter-chip questions, in pack order."""
    try:
        from city_config import load_city_config
        groups = (load_city_config().branding or {}).get("starter_groups", [])
        return [c[1] for g in groups for c in g.get("chips", []) if len(c) == 2]
    except Exception as e:
        print(f"Warning: could not read starter chips from the city pack: {e}")
        return []


def _starter_questions() -> list:
    seen, out = set(), []
    for q in _pack_chip_questions() + EXTRA_QUESTIONS:
        k = (q or "").strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(q)
    return out


STARTER_QUESTIONS = _starter_questions()


def fetch_entries(host: str) -> dict:
    """Current-version cache entries, keyed by question text.

    Server keys are "<prompt-version>:<question>" so a prompt change
    invalidates old answers. Entries from other versions are EXCLUDED — the
    app will never serve them, so treating them as cached would report a warm
    cache while the live one is cold. Falls back to prefix-stripping only for
    servers too old to report cache_version.
    """
    # /api/cache is admin-gated (it lists verbatim user questions); send the
    # operator token from the environment.
    headers = {}
    token = os.environ.get("ADMIN_TOKEN", "")
    if token:
        headers["X-Admin-Token"] = token
    resp = requests.get(f"{host}/api/cache", timeout=10, headers=headers)
    resp.raise_for_status()
    payload = resp.json()
    entries = payload.get("entries", {})
    version = payload.get("cache_version")
    if not version:
        return {k.split(":", 1)[-1]: v for k, v in entries.items()}
    prefix = f"{version}:"
    return {k[len(prefix):]: v for k, v in entries.items() if k.startswith(prefix)}


def main():
    parser = argparse.ArgumentParser(description="Warm response cache for starter questions")
    parser.add_argument("--host", default="http://localhost:8000")
    parser.add_argument("--delay", type=int, default=10, help="Seconds between questions")
    parser.add_argument("--check-only", action="store_true", help="Only check cache status")
    args = parser.parse_args()

    # Fail HARD if the pack yielded no starter chips: STARTER_QUESTIONS would
    # have silently collapsed to the few EXTRA_QUESTIONS, and warming/reporting
    # "all cached" over that short list leaves every real chip cold — the exact
    # cold-but-looks-warm state that masked the Cerebras model-404 outage. Refuse
    # rather than warm a degraded list.
    if not _pack_chip_questions():
        print("ERROR: no starter-chip questions could be read from the city pack — "
              "refusing to run (would warm only the extras and leave every chip cold). "
              "Check CITY_CONFIG and cities/*/city.yaml.")
        sys.exit(1)

    # Check current cache status
    try:
        entries = fetch_entries(args.host)
        print(f"Currently cached: {len(entries)} questions")
    except Exception as e:
        print(f"Could not reach bot at {args.host}: {e}")
        return

    if args.check_only:
        for q in STARTER_QUESTIONS:
            info = entries.get(q.lower().strip(), {})
            status = "CACHED" if info else "NOT CACHED"
            extra = ""
            if info:
                extra = f" ({info['events']} events, interp={info['has_interpretation']}, error={info['has_error']})"
            print(f"  {status}{extra} | {q[:60]}")
        return

    # Warm uncached questions
    need_warming = []
    for q in STARTER_QUESTIONS:
        info = entries.get(q.lower().strip(), {})
        if not info or info.get("has_error") or not info.get("has_interpretation"):
            need_warming.append(q)

    if not need_warming:
        print("All starter questions already cached with valid responses.")
        return

    print(f"\nWarming {len(need_warming)} questions ({args.delay}s delay between each):\n")
    for i, q in enumerate(need_warming):
        print(f"  [{i+1}/{len(need_warming)}] {q[:55]}...")
        try:
            resp = requests.post(
                f"{args.host}/api/ask",
                json={"question": q},
                timeout=120,
            )
            has_interp = resp.text.count('"type": "interpretation"')
            has_chart = resp.text.count('"type": "chart"')
            has_error = resp.text.count('"type": "error"')
            status = "OK" if has_interp and not has_error else "FAILED"
            print(f"    {status} | interp={has_interp} chart={has_chart} error={has_error}")
        except Exception as e:
            print(f"    ERROR: {e}")

        if i < len(need_warming) - 1:
            time.sleep(args.delay)

    # Verify final cache status
    print("\nFinal cache status:")
    try:
        entries = fetch_entries(args.host)
        for q in STARTER_QUESTIONS:
            info = entries.get(q.lower().strip(), {})
            if info:
                status = "GOOD" if info["has_interpretation"] and not info["has_error"] else "BAD"
            else:
                status = "MISSING"
            print(f"  {status:>7} | {q[:60]}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
