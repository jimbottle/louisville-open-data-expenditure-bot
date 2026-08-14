#!/usr/bin/env python3
"""
Pull data from ArcGIS FeatureServer endpoints.

Handles pagination, metadata/data-dictionary extraction, and outputs to CSV or JSON.

Usage:
    # Pull full dataset to CSV (default)
    python pull_arcgis.py "https://services1.arcgis.com/.../FeatureServer/0"

    # Pull with a where clause filter
    python pull_arcgis.py "https://services1.arcgis.com/.../FeatureServer/0" --where "agency='OMB Finance'"

    # Output as JSON (newline-delimited)
    python pull_arcgis.py "https://services1.arcgis.com/.../FeatureServer/0" --format json

    # Save metadata/data dictionary
    python pull_arcgis.py "https://services1.arcgis.com/.../FeatureServer/0" --metadata

    # Metadata only, no data pull
    python pull_arcgis.py "https://services1.arcgis.com/.../FeatureServer/0" --metadata --no-data

    # Custom output directory
    python pull_arcgis.py "https://services1.arcgis.com/.../FeatureServer/0" -o ./data/
"""

import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urlencode, urlparse

import pandas as pd
import requests

DEFAULT_BATCH_SIZE = 1000
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds


def normalize_url(url: str) -> str:
    """Strip query params and trailing /query from a FeatureServer layer URL."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/query"):
        path = path[: -len("/query")]
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def derive_layer_name(base_url: str) -> str:
    """Extract a filesystem-safe name from the service URL."""
    match = re.search(r"/services/([^/]+)/FeatureServer/(\d+)", base_url)
    if match:
        return f"{match.group(1)}_layer{match.group(2)}"
    return "arcgis_export"


def fetch_json(url: str, params: dict, retries: int = MAX_RETRIES) -> dict:
    """GET request with retry logic. Returns parsed JSON."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            return data
        except (requests.RequestException, json.JSONDecodeError) as e:
            if attempt == retries:
                raise
            wait = RETRY_DELAY * attempt
            print(f"  Retry {attempt}/{retries} after error: {e} (waiting {wait}s)")
            time.sleep(wait)


def get_metadata(base_url: str) -> dict:
    """Fetch full layer metadata."""
    return fetch_json(base_url, {"f": "json"})


def get_record_count(base_url: str, where: str = "1=1") -> int:
    """Get total record count for a query."""
    data = fetch_json(
        f"{base_url}/query",
        {"where": where, "returnCountOnly": "true", "f": "json"},
    )
    return data["count"]


def pull_records(
    base_url: str,
    where: str = "1=1",
    out_fields: str = "*",
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[dict]:
    """Paginate through all records and return as a list of attribute dicts."""
    total = get_record_count(base_url, where)
    print(f"Total records matching query: {total:,}")

    if total == 0:
        return []

    query_url = f"{base_url}/query"
    all_records = []
    offset = 0
    page = 0

    # Advance the offset by the number of records ACTUALLY returned, not by the
    # requested batch_size. Hosted layers cap page size at their own
    # maxRecordCount (often 1000-2000), so a --batch-size larger than that gets
    # a short page every time; the old `offset = page * batch_size` with a
    # precomputed page count then stepped over the un-returned rows and stopped
    # early, silently writing a partial CSV. Loop until the server says there is
    # no more (exceededTransferLimit) or hands back an empty page.
    while True:
        params = {
            "where": where,
            "outFields": out_fields,
            "outSR": "4326",
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": batch_size,
        }
        data = fetch_json(query_url, params)
        features = data.get("features", [])
        if not features:
            break
        batch = [f["attributes"] for f in features]
        all_records.extend(batch)
        offset += len(features)
        page += 1
        print(f"  Page {page} — fetched {len(batch)} records ({len(all_records):,} of {total:,})")

        # Safety bound against a layer that does NOT paginate
        # (supportsPagination=false — common on older on-prem ArcGIS Server):
        # it ignores resultOffset, returns the SAME page every time with
        # exceededTransferLimit=true, so neither the empty-page nor the flag
        # break ever fires and the loop would spin forever appending duplicates.
        # A correct pull collects exactly `total`; overshooting it by more than a
        # page means the server is not honoring the offset — stop loudly.
        if total and len(all_records) > total + batch_size:
            print(f"  ⚠️  Stopping at {len(all_records):,} records, past the expected "
                  f"{total:,}: the layer appears to ignore pagination (resultOffset) "
                  "and is returning duplicates. Data was NOT saved reliably — verify "
                  "the layer supports paging.")
            raise RuntimeError(
                f"pagination not honored by {base_url}: fetched {len(all_records):,} "
                f"rows for an expected {total:,}; aborting to avoid a duplicate-laden dump."
            )

        # The transfer-limit flag is the authoritative "more to come" signal and
        # is the one that survives a server whose page cap is below batch_size.
        if not data.get("exceededTransferLimit", False):
            break

    if total and len(all_records) < total:
        # total is a pre-count snapshot; a smaller final tally usually means the
        # source changed mid-pull, but it can also mean a truncated pull — say so
        # loudly rather than writing a short file that reads as complete.
        print(f"  ⚠️  Pulled {len(all_records):,} of {total:,} expected records — "
              "source may have changed, or the pull was truncated.")
    print(f"Done. {len(all_records):,} records pulled.")
    return all_records


def save_metadata(meta: dict, output_dir: str, name: str) -> str:
    """Save layer metadata and field dictionary to JSON and a readable summary."""
    os.makedirs(output_dir, exist_ok=True)

    # Full metadata JSON
    meta_path = os.path.join(output_dir, f"{name}_metadata.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    # Human-readable data dictionary
    dict_path = os.path.join(output_dir, f"{name}_data_dictionary.csv")
    fields = meta.get("fields", [])
    rows = []
    for field in fields:
        domain = field.get("domain")
        domain_values = ""
        if domain and domain.get("type") == "codedValue":
            domain_values = "; ".join(
                f"{cv['code']}={cv['name']}" for cv in domain.get("codedValues", [])
            )
        rows.append(
            {
                "field_name": field.get("name"),
                "alias": field.get("alias"),
                "type": field.get("type"),
                "length": field.get("length", ""),
                "nullable": field.get("nullable"),
                "default_value": field.get("defaultValue"),
                "domain_type": domain.get("type") if domain else "",
                "domain_values": domain_values,
            }
        )
    pd.DataFrame(rows).to_csv(dict_path, index=False)

    print(f"Metadata saved to:        {meta_path}")
    print(f"Data dictionary saved to: {dict_path}")
    return meta_path


def save_data(records: list[dict], output_dir: str, name: str, fmt: str) -> str:
    """Save records to CSV or newline-delimited JSON.

    Written atomically (to a .part file, then os.replace) like pull_socrata: a
    kill mid-write must not leave a truncated file that read_csv_auto later
    ingests as a complete year with no error.
    """
    os.makedirs(output_dir, exist_ok=True)

    if fmt == "json":
        out_path = os.path.join(output_dir, f"{name}.ndjson")
        tmp_path = out_path + ".part"
        with open(tmp_path, "w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
    else:
        out_path = os.path.join(output_dir, f"{name}.csv")
        tmp_path = out_path + ".part"
        pd.DataFrame(records).to_csv(tmp_path, index=False)

    os.replace(tmp_path, out_path)
    print(f"Data saved to: {out_path} ({len(records):,} records)")
    return out_path


def main():
    parser = argparse.ArgumentParser(
        description="Pull data from ArcGIS FeatureServer endpoints.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "url",
        help="FeatureServer layer URL (e.g. .../FeatureServer/0)",
    )
    parser.add_argument(
        "--where",
        default="1=1",
        help="SQL where clause filter (default: all records)",
    )
    parser.add_argument(
        "--fields",
        default="*",
        help="Comma-separated field list or * for all (default: *)",
    )
    parser.add_argument(
        "--format",
        choices=["csv", "json"],
        default="csv",
        help="Output format (default: csv)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Records per request (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--metadata",
        action="store_true",
        help="Also save layer metadata and data dictionary",
    )
    parser.add_argument(
        "--no-data",
        action="store_true",
        help="Skip data pull (use with --metadata for metadata only)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default="./data",
        help="Output directory (default: ./data)",
    )
    parser.add_argument(
        "--name",
        help="Override output file name (default: derived from URL)",
    )

    args = parser.parse_args()
    base_url = normalize_url(args.url)
    name = args.name or derive_layer_name(base_url)

    print(f"Service: {base_url}")
    print(f"Name:    {name}")
    print()

    if args.metadata or args.no_data:
        meta = get_metadata(base_url)
        save_metadata(meta, args.output_dir, name)
        print()

    if not args.no_data:
        records = pull_records(base_url, args.where, args.fields, args.batch_size)
        if records:
            save_data(records, args.output_dir, name, args.format)


if __name__ == "__main__":
    main()
