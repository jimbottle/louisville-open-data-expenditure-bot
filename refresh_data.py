#!/usr/bin/env python3
"""
Refresh all Louisville Open Data datasets, rebuild profiles, and reload graphs.

Usage:
    # Full refresh (pull + profiles + graph)
    python refresh_data.py

    # Pull data only (skip profiles and graph)
    python refresh_data.py --pull-only

    # Rebuild profiles and graph from existing CSVs (no re-pull)
    python refresh_data.py --skip-pull

    # Skip SOS lookups for faster profile rebuild
    python refresh_data.py --skip-pull --skip-sos

    # Target specific output directory
    python refresh_data.py -o ./data

    # Reload Neo4j graph only
    python refresh_data.py --graph-only --neo4j-uri bolt://localhost:7687
"""

import argparse
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR_DEFAULT = os.path.join(SCRIPT_DIR, "data")

ARCGIS_BASE = "https://services1.arcgis.com/79kfd2K6fskCAkyg/arcgis/rest/services"

# All known expenditure datasets
EXPENDITURE_DATASETS = {
    # New era (2018+) — same schema
    "eExpenditures_2018": f"{ARCGIS_BASE}/eExpenditures_2018/FeatureServer/0",
    "eExpenditures_2019": f"{ARCGIS_BASE}/eExpenditures_2019/FeatureServer/0",
    "eExpenditures_2020": f"{ARCGIS_BASE}/eExpenditures_2020/FeatureServer/0",
    "eExpenditures_2021": f"{ARCGIS_BASE}/eExpenditures_2021/FeatureServer/0",
    "eExpenditures_2022": f"{ARCGIS_BASE}/eExpenditures_2022/FeatureServer/0",
    "eExpenditures_2023": f"{ARCGIS_BASE}/eExpenditures_2023/FeatureServer/0",
    "eExpenditures_2024": f"{ARCGIS_BASE}/eExpenditures2024/FeatureServer/0",
    "eExpenditures_2025": f"{ARCGIS_BASE}/eExpenditures_2025/FeatureServer/0",
    "eExpenditures_2026": f"{ARCGIS_BASE}/eExpenditures_2026/FeatureServer/0",
    # Old era (2008-2017) — different schema, mapped at load time
    "eExpenditures_2008": f"{ARCGIS_BASE}/Louisville_Metro_KY_Expenditures_Data_For_Fiscal_Year_2008/FeatureServer/0",
    "eExpenditures_2009": f"{ARCGIS_BASE}/Louisville_Metro_KY_Expenditures_Data_For_Fiscal_Year_2009/FeatureServer/0",
    "eExpenditures_2010": f"{ARCGIS_BASE}/Louisville_Metro_KY_Expenditures_Data_For_Fiscal_Year_2010/FeatureServer/0",
    "eExpenditures_2011": f"{ARCGIS_BASE}/Louisville_Metro_KY_Expenditures_Data_For_Fiscal_Year_2011/FeatureServer/0",
    "eExpenditures_2012": f"{ARCGIS_BASE}/Louisville_Metro_KY_Expenditures_Data_For_Fiscal_Year_2012/FeatureServer/0",
    "eExpenditures_2013": f"{ARCGIS_BASE}/Louisville_Metro_KY_Expenditures_Data_For_Fiscal_Year_2013/FeatureServer/0",
    "eExpenditures_2014": f"{ARCGIS_BASE}/Louisville_Metro_KY_Expenditures_Data_For_Fiscal_Year_2014/FeatureServer/0",
    "eExpenditures_2015": f"{ARCGIS_BASE}/Louisville_Metro_KY_Expenditures_Data_For_Fiscal_Year_2015/FeatureServer/0",
    "eExpenditures_2016": f"{ARCGIS_BASE}/Louisville_Metro_KY_Expenditures_Data_For_Fiscal_Year_2016/FeatureServer/0",
    "eExpenditures_2017": f"{ARCGIS_BASE}/Louisville_Metro_KY_Expenditures_Data_For_Fiscal_Year_2017/FeatureServer/0",
}

# Enrichment datasets
ENRICHMENT_DATASETS = {
    "salary_data": f"{ARCGIS_BASE}/SalaryData/FeatureServer/0",
    "capital_projects": f"{ARCGIS_BASE}/Capital_Projects_/FeatureServer/0",
    "active_contractors": f"{ARCGIS_BASE}/Louisville_Metro_KY_Active_Contractors/FeatureServer/0",
    "staff_demographics": f"{ARCGIS_BASE}/Louisville_Metro_KY_Metro_Staff_Demographics/FeatureServer/0",
    "hr_requisitions": f"{ARCGIS_BASE}/Louisville_Metro_KY_HR_Requisition_Log/FeatureServer/0",
}


def run(cmd, description=""):
    """Run a subprocess and print status."""
    print(f"\n{'─' * 60}")
    print(f"  {description}" if description else f"  {' '.join(cmd[:3])}...")
    print(f"{'─' * 60}")
    result = subprocess.run(cmd, cwd=SCRIPT_DIR)
    if result.returncode != 0:
        print(f"  FAILED (exit code {result.returncode})")
        return False
    return True


def pull_datasets(data_dir):
    """Pull all expenditure and enrichment datasets."""
    pull_script = os.path.join(SCRIPT_DIR, "pull_arcgis.py")
    failed = []

    print("\n" + "=" * 60)
    print("  PULLING EXPENDITURE DATASETS")
    print("=" * 60)

    for name, url in sorted(EXPENDITURE_DATASETS.items()):
        ok = run(
            [sys.executable, pull_script, url, "--name", name, "-o", data_dir],
            f"Pulling {name}",
        )
        if not ok:
            failed.append(name)

    print("\n" + "=" * 60)
    print("  PULLING ENRICHMENT DATASETS")
    print("=" * 60)

    for name, url in sorted(ENRICHMENT_DATASETS.items()):
        ok = run(
            [sys.executable, pull_script, url, "--name", name, "-o", data_dir],
            f"Pulling {name}",
        )
        if not ok:
            failed.append(name)

    if failed:
        print(f"\nWARNING: {len(failed)} datasets failed to pull: {', '.join(failed)}")
    else:
        print(f"\nAll {len(EXPENDITURE_DATASETS) + len(ENRICHMENT_DATASETS)} datasets pulled successfully.")

    return len(failed) == 0


def build_profiles(data_dir, skip_sos=False, top=200):
    """Rebuild contractor profiles."""
    print("\n" + "=" * 60)
    print("  BUILDING CONTRACTOR PROFILES")
    print("=" * 60)

    cmd = [
        sys.executable, os.path.join(SCRIPT_DIR, "graph", "build_contractor_profiles.py"),
        "--top", str(top),
        "--data-dir", data_dir,
        "--output", os.path.join(data_dir, "contractor_profiles.csv"),
    ]
    if skip_sos:
        cmd.append("--skip-sos")

    return run(cmd, f"Building top {top} contractor profiles" + (" (skip SOS)" if skip_sos else ""))


def reload_graph(neo4j_uri, neo4j_password, data_dir):
    """Reload the Neo4j context graph."""
    print("\n" + "=" * 60)
    print("  RELOADING NEO4J GRAPH")
    print("=" * 60)

    return run(
        [
            sys.executable, os.path.join(SCRIPT_DIR, "graph", "load_graph.py"),
            "--uri", neo4j_uri,
            "--password", neo4j_password,
            "--data-dir", data_dir,
        ],
        f"Loading graph into {neo4j_uri}",
    )


def main():
    parser = argparse.ArgumentParser(
        description="Refresh all Louisville Open Data datasets and rebuild derived data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-o", "--data-dir", default=DATA_DIR_DEFAULT, help="Data directory")
    parser.add_argument("--pull-only", action="store_true", help="Only pull datasets, skip profiles and graph")
    parser.add_argument("--skip-pull", action="store_true", help="Skip pulling, rebuild from existing CSVs")
    parser.add_argument("--skip-sos", action="store_true", help="Skip KY SOS lookups in profile builder")
    parser.add_argument("--skip-graph", action="store_true", help="Skip Neo4j graph reload")
    parser.add_argument("--graph-only", action="store_true", help="Only reload Neo4j graph")
    parser.add_argument("--profile-top", type=int, default=200, help="Number of top payees to profile")
    parser.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", "context123"))

    args = parser.parse_args()
    os.makedirs(args.data_dir, exist_ok=True)

    start = time.time()
    success = True

    if args.graph_only:
        success = reload_graph(args.neo4j_uri, args.neo4j_password, args.data_dir)
    else:
        # Step 1: Pull data
        if not args.skip_pull:
            success = pull_datasets(args.data_dir) and success

        if args.pull_only:
            elapsed = time.time() - start
            print(f"\nData pull complete in {elapsed / 60:.1f} minutes.")
            return

        # Step 2: Build contractor profiles
        success = build_profiles(args.data_dir, args.skip_sos, args.profile_top) and success

        # Step 3: Reload graph
        if not args.skip_graph:
            success = reload_graph(args.neo4j_uri, args.neo4j_password, args.data_dir) and success

    elapsed = time.time() - start
    print(f"\n{'=' * 60}")
    if success:
        print(f"  REFRESH COMPLETE in {elapsed / 60:.1f} minutes")
    else:
        print(f"  REFRESH COMPLETED WITH ERRORS in {elapsed / 60:.1f} minutes")
    print(f"{'=' * 60}")

    # Print summary of data files
    csv_count = len([f for f in os.listdir(args.data_dir) if f.endswith(".csv")])
    print(f"\n  Data directory: {args.data_dir}")
    print(f"  CSV files: {csv_count}")


if __name__ == "__main__":
    main()
