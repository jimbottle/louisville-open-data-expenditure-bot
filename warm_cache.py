#!/usr/bin/env python3
"""
Warm the response cache for all starter questions.
Run after a data refresh or container restart.

Usage:
    python warm_cache.py [--host http://localhost:8000] [--delay 10]
"""

import argparse
import os
import requests
import time

STARTER_QUESTIONS = [
    "Which agencies have spent the most money across all fiscal years?",
    "How has total annual spending changed from 2008 to 2026?",
    "Give me a year-over-year breakdown of LMPD spending",
    "How much does the mayor make? What about the police chief?",
    "What are the highest paid positions in Louisville Metro government?",
    "Who are the registered agents for the top 10 contractors by total spend?",
    "Which vendors receive payments from the most different agencies?",
    "Are there any patterns that suggest potential contract splitting?",
    "What does Louisville Metro spend on technology and cybersecurity?",
    "What are the largest capital projects and how much was allocated to each?",
    "How much grant funding has Louisville received and from which sources?",
]


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
