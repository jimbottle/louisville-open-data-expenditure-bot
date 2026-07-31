#!/usr/bin/env python3
"""
Pull datasets from a Socrata (Tyler Data & Insights) portal as CSV.

Uses the bulk export endpoint (/api/views/<id>/rows.csv) — one call, no
pagination, no auth for public data. Companion to pull_arcgis.py for cities
on the Socrata stack (Cincinnati, Kansas City, Chicago).

Usage:
    python pull_socrata.py data.cincinnati-oh.gov qrj9-83t8 -n vendor_payments -o data_cincinnati/
    python pull_socrata.py data.cincinnati-oh.gov qrj9-83t8 --metadata   # column metadata JSON too
"""

import argparse
import json
import os
import sys
import time

import requests

MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds


def pull_csv(domain: str, dataset_id: str, out_path: str) -> int:
    """Stream the bulk CSV export to out_path. Returns bytes written."""
    url = f"https://{domain}/api/views/{dataset_id}/rows.csv?accessType=DOWNLOAD"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with requests.get(url, stream=True, timeout=600) as r:
                r.raise_for_status()
                written = 0
                with open(out_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
                        written += len(chunk)
                return written
        except requests.RequestException as e:
            if attempt == MAX_RETRIES:
                raise
            print(f"  attempt {attempt} failed ({e}); retrying in {RETRY_DELAY}s")
            time.sleep(RETRY_DELAY)


def pull_metadata(domain: str, dataset_id: str, out_path: str) -> None:
    """Save the dataset's api/views metadata (name, description, columns)."""
    url = f"https://{domain}/api/views/{dataset_id}.json"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    meta = r.json()
    keep = {
        "id": meta.get("id"),
        "name": meta.get("name"),
        "description": meta.get("description"),
        "rowsUpdatedAt": meta.get("rowsUpdatedAt"),
        "columns": [
            {
                "fieldName": c.get("fieldName"),
                "name": c.get("name"),
                "dataTypeName": c.get("dataTypeName"),
                "description": c.get("description"),
            }
            for c in meta.get("columns", [])
        ],
    }
    with open(out_path, "w") as f:
        json.dump(keep, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Pull a Socrata dataset as CSV")
    parser.add_argument("domain", help="Portal domain, e.g. data.cincinnati-oh.gov")
    parser.add_argument("dataset_id", help="Socrata 4x4 dataset id, e.g. qrj9-83t8")
    parser.add_argument("-n", "--name", help="Output basename (default: the dataset id)")
    parser.add_argument("-o", "--out-dir", default="data", help="Output directory")
    parser.add_argument("--metadata", action="store_true", help="Also save column metadata JSON")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    name = args.name or args.dataset_id.replace("-", "_")
    csv_path = os.path.join(args.out_dir, f"{name}.csv")

    print(f"Pulling {args.domain}/{args.dataset_id} -> {csv_path}")
    t0 = time.time()
    written = pull_csv(args.domain, args.dataset_id, csv_path)
    print(f"  {written / 1e6:,.1f} MB in {time.time() - t0:,.0f}s")

    if args.metadata:
        meta_path = os.path.join(args.out_dir, f"{name}_metadata.json")
        pull_metadata(args.domain, args.dataset_id, meta_path)
        print(f"  metadata -> {meta_path}")


if __name__ == "__main__":
    main()
