"""
Build contractor profiles by enriching expenditure payees with:
1. Active contractor license data (from Louisville open data)
2. Expenditure history (aggregated from expenditures table)
3. Kentucky SOS business entity data (scraped from public records)

Outputs: data/contractor_profiles.csv

Usage:
    python graph/build_contractor_profiles.py [--skip-sos] [--top N]
"""

import argparse
import csv
import datetime as _dt
import os
import re
import subprocess
import sys
import time

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_model import load_all_data


# ── KY SOS Scraper ───────────────────────────────────────────────────────────

SOS_SEARCH_URL = "https://sosbes.sos.ky.gov/BusSearchNProfile/search.aspx"
SOS_PROFILE_URL = "https://sosbes.sos.ky.gov/BusSearchNProfile/Profile.aspx"


def sos_search(name: str, session: requests.Session) -> list[dict]:
    """Search KY SOS for a business entity by name. Returns list of matches."""
    # Get the search page to extract ViewState
    resp = session.get(SOS_SEARCH_URL, timeout=15)
    soup = BeautifulSoup(resp.text, "html.parser")

    viewstate = soup.find("input", {"name": "__VIEWSTATE"})
    if not viewstate:
        return []

    data = {
        "__VIEWSTATE": viewstate["value"],
        "ctl00$MainContent$txtSearch": name,
        "ctl00$MainContent$ddlSearchBy": "Business Name or Organization Number",
        "ctl00$MainContent$BSearch": "Search",
    }
    # Include optional ASP.NET fields if present
    for field in ["__EVENTVALIDATION", "__VIEWSTATEGENERATOR"]:
        el = soup.find("input", {"name": field})
        if el:
            data[field] = el["value"]

    resp = session.post(SOS_SEARCH_URL, data=data, timeout=15)
    soup = BeautifulSoup(resp.text, "html.parser")

    results = []
    table = soup.find("table", {"id": "MainContent_gvSearchResults"})
    if not table:
        return []

    rows = table.find_all("tr")[1:]  # skip header
    for row in rows:
        cells = row.find_all("td")
        if len(cells) >= 4:
            link = cells[0].find("a")
            href = link["href"] if link else ""
            ctr = ""
            if "ctr=" in href:
                ctr = href.split("ctr=")[1].split("&")[0]
            results.append({
                "name": cells[0].get_text(strip=True),
                "org_number": cells[1].get_text(strip=True),
                "status": cells[2].get_text(strip=True),
                "type": cells[3].get_text(strip=True),
                "ctr": ctr,
            })
    return results


def sos_profile(ctr: str, session: requests.Session) -> dict:
    """Get detailed profile for a KY SOS entity by its internal ID."""
    resp = session.get(f"{SOS_PROFILE_URL}?ctr={ctr}", timeout=15)
    soup = BeautifulSoup(resp.text, "html.parser")

    # Parse grid-label / grid-value pairs
    fields = {}
    for row in soup.find_all("div", class_="grid-row"):
        label_el = row.find("div", class_="grid-label")
        value_el = row.find("div", class_="grid-value")
        if label_el and value_el:
            label = label_el.get_text(strip=True)
            value = value_el.get_text(" ", strip=True)
            fields[label] = value

    profile = {
        "org_number": fields.get("Organization Number", ""),
        "name": fields.get("Name", ""),
        "status": fields.get("Status", ""),
        "standing": fields.get("Standing", ""),
        "company_type": fields.get("Company Type", ""),
        "industry": fields.get("Industry", ""),
        "employees": fields.get("Number of Employees", ""),
        "county": fields.get("Primary County", ""),
        "file_date": fields.get("File Date", ""),
        "org_date": fields.get("Organization Date", ""),
        "last_annual_report": fields.get("Last Annual Report", ""),
        "principal_office": fields.get("Principal Office", ""),
        "managed_by": fields.get("Managed By", fields.get("Management Type", "")),
        "registered_agent": fields.get("Registered Agent", ""),
    }
    return profile


def lookup_entity(name: str, session: requests.Session) -> dict | None:
    """Search and get profile for a business entity. Returns the best match,
    {} when the search completed with no match, or None when the lookup
    itself failed (so the caller can tell a miss from an outage)."""
    try:
        results = sos_search(name, session)
        if not results:
            return {}

        # Try exact match first, then first active result
        for r in results:
            if r["name"].upper() == name.upper() and "Active" in r["status"]:
                if r["ctr"]:
                    return sos_profile(r["ctr"], session)

        # Fall back to first result with a ctr
        for r in results:
            if r["ctr"]:
                return sos_profile(r["ctr"], session)

        return {}
    except Exception as e:
        # None, not {}: a timeout/HTTP/parse failure is NOT a "no such entity"
        # and must not be recorded as a looked-up miss that --reuse-sos then
        # skips for 120 days (roborev 5148).
        print(f"    SOS lookup failed for {name}: {e}")
        return None


# ── SOS result reuse ─────────────────────────────────────────────────────────

SOS_FIELDS = [
    "sos_org_number", "sos_status", "sos_standing", "sos_company_type",
    "sos_industry", "sos_employees", "sos_county", "sos_file_date",
    "sos_principal_office", "sos_managed_by", "sos_registered_agent",
]
# Carried along with a reused row so scrape_officers.py can skip it too.
SOS_CARRY_FIELDS = SOS_FIELDS + ["sos_officers", "sos_looked_up"]


def _git_commit_date(path: str):
    """Date of the last commit touching `path`, or None if that cannot be
    determined (not a git checkout, file untracked, git missing, or a SHALLOW
    checkout: with `--depth 1` the only commit is a grafted root, so
    `git log -1 -- file` reports HEAD's date for every file, which would make
    an undated row look fresh on every run — roborev 5149)."""
    cwd = os.path.dirname(os.path.abspath(path)) or "."
    try:
        def git(*args):
            return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                                  text=True, timeout=10, check=False).stdout.strip()
        if git("rev-parse", "--is-shallow-repository") != "false":
            return None
        out = git("log", "-1", "--format=%cs", "--", os.path.basename(path))
        return _dt.date.fromisoformat(out) if out else None
    except Exception:
        return None


def load_prior_sos(path: str, max_age_days: int) -> dict[str, dict]:
    """SOS results from a previous profiles CSV, keyed by payee, that are
    recent enough to reuse (louisville-open-data-rm4).

    The KY SOS lookups are ~150 polite 1.5 s round-trips (~4.5 min on
    2026-09-22) and the answers change rarely, so a monthly rebuild reuses any
    row looked up within `max_age_days` and only fetches payees that are new
    to the top list or stale. A hit and a miss are both "looked up": the
    `sos_looked_up` date column records either (a FAILED lookup records
    nothing, so it is retried next time).

    Rows without a stamp (files written before the column existed) are aged
    by the file's last git commit date — the latest the data can have been
    fetched — never by mtime: the build plane does a fresh clone, so mtime is
    always "now" and every row would look fresh forever (roborev 5148). With
    no git date the row is treated as stale and re-fetched. The stamp carried
    over is the real one, or that commit date for a legacy file, so a
    committed rebuild keeps aging from the original fetch rather than from the
    new commit. In a file that has the column, a blank stamp is a failed
    lookup and is always re-fetched."""
    if not path or not os.path.exists(path) or max_age_days <= 0:
        return {}
    import pandas as pd
    try:
        prior = pd.read_csv(path, dtype=str, keep_default_na=False)
    except Exception as e:
        print(f"  (prior profiles at {path} unreadable: {e}; SOS cache ignored)")
        return {}
    if "payee" not in prior.columns or "sos_registered_agent" not in prior.columns:
        return {}
    has_stamp = "sos_looked_up" in prior.columns
    # The commit-date fallback is for files written before the column existed.
    # In a stamped file a blank cell means the lookup FAILED last time (or the
    # stamp is garbage) and the row must be re-fetched, not dated by the
    # commit and reused as a miss (roborev 5149).
    legacy_as_of = None if has_stamp else _git_commit_date(path)
    cutoff = _dt.date.today() - _dt.timedelta(days=max_age_days)
    out = {}
    for _, row in prior.iterrows():
        if has_stamp:
            try:
                when = _dt.date.fromisoformat(row.get("sos_looked_up", ""))
            except ValueError:
                continue
        else:
            when = legacy_as_of
        if when is None or when < cutoff:
            continue
        rec = {f: row.get(f, "") for f in SOS_CARRY_FIELDS if f in prior.columns}
        rec["sos_looked_up"] = when.isoformat()
        out[row["payee"]] = rec
    return out


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Build contractor profiles")
    parser.add_argument("--top", type=int, default=200, help="Number of top payees to profile")
    parser.add_argument("--skip-sos", action="store_true", help="Skip KY SOS lookups")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--output", default="data/contractor_profiles.csv")
    parser.add_argument("--reuse-sos", default=None,
                        help="Previous profiles CSV whose recent SOS results are reused "
                             "(default: --output if it exists; '' disables)")
    parser.add_argument("--sos-max-age-days", type=int, default=120,
                        help="Reuse SOS results looked up within this many days (0 = always re-fetch)")
    args = parser.parse_args()

    print("Loading expenditure data...")
    con = load_all_data(args.data_dir)

    # Get top payees by spend using canonical names to merge variants
    print(f"\nBuilding profiles for top {args.top} payees (using canonical names)...")
    payees = con.execute(f"""
        SELECT
            e.payee_canonical AS payee,
            ROUND(SUM(e.extended_amount), 2) AS total_spend,
            COUNT(*) AS transaction_count,
            COUNT(DISTINCT e.fiscal_year) AS years_active,
            MIN(e.fiscal_year) AS first_year,
            MAX(e.fiscal_year) AS last_year,
            COUNT(DISTINCT e.agency_canonical) AS agencies_served,
            STRING_AGG(DISTINCT e.agency_canonical, '; ' ORDER BY e.agency_canonical) AS agency_list,
            COUNT(DISTINCT e.fund) AS funds_used,
            COUNT(DISTINCT e.expenditure_type) AS expenditure_types
        FROM expenditures e
        WHERE e.payee_canonical IS NOT NULL AND e.is_data_artifact = FALSE
            AND e.extended_amount > 0
        GROUP BY e.payee_canonical
        ORDER BY total_spend DESC
        LIMIT {args.top}
    """).fetchdf()

    # Merge with active contractor data
    print("Matching with active contractor licenses...")
    contractors = con.execute("""
        SELECT FULLNAME, CATEGORY, DESCRIPTION, ADDRESS1, CITY, STATE, ZIPCODE,
               LICENSENO, EXPIRATIONDATE, EMAIL, DAYTIMEPHONE
        FROM active_contractors
    """).fetchdf()

    # Merge on lowercase name match
    payees["payee_lower"] = payees["payee"].str.lower().str.strip()
    contractors["name_lower"] = contractors["FULLNAME"].str.lower().str.strip()
    # One payee can hold several licenses (Johnson Controls: FIREDETECT and
    # HVAC). A row-fanning merge would repeat the payee-level total_spend once
    # per license and double-count any SUM over the table, so the license
    # fields are collapsed to one row per payee, values joined with "; ".
    # Only TEXT fields are joined. A numeric column (ZIPCODE) must stay a
    # single value: DuckDB infers the CSV column's type from the whole file,
    # so one "53201.0; 40220.0" cell would flip ZIPCODE to VARCHAR for all
    # 200 rows and break every numeric ZIP comparison in generated SQL.
    # Numeric/date columns take the first license's value instead.
    def _join_text(col):
        vals = col.dropna().astype(str).unique()
        return "; ".join(vals) if len(vals) else None

    def _first(col):
        vals = col.dropna()
        return vals.iloc[0] if len(vals) else None

    single_value = {"ZIPCODE", "EXPIRATIONDATE"}
    lic_cols = [c for c in contractors.columns if c != "name_lower"]
    contractors = contractors.groupby("name_lower", as_index=False)[lic_cols].agg(
        {c: (_first if c in single_value else _join_text) for c in lic_cols}
    )
    merged = payees.merge(contractors, left_on="payee_lower", right_on="name_lower", how="left")
    merged = merged.drop(columns=["payee_lower", "name_lower"], errors="ignore")
    assert merged["payee"].is_unique, "license merge fanned out payee rows"

    licensed = merged["LICENSENO"].notna().sum()
    print(f"  {licensed}/{len(merged)} matched to active contractor licenses")

    # KY SOS lookups
    if not args.skip_sos:
        print(f"\nLooking up top payees on KY Secretary of State...")
        session = requests.Session()
        session.headers.update({"User-Agent": "Louisville-OpenData-Research/1.0"})

        sos_fields = SOS_FIELDS
        for f in SOS_CARRY_FIELDS:
            merged[f] = None

        reuse_path = args.output if args.reuse_sos is None else args.reuse_sos
        prior = load_prior_sos(reuse_path, args.sos_max_age_days)
        if prior:
            print(f"  Reusing SOS results from {reuse_path} for lookups newer than "
                  f"{args.sos_max_age_days} days ({len(prior)} payees cached)")
        today = _dt.date.today().isoformat()

        # Only look up entities that look like businesses (contain LLC, INC, CO, CORP, etc.)
        biz_pattern = re.compile(r'\b(LLC|INC|CORP|CO\b|LTD|LP|COMPANY|ENTERPRISES|ASSOCIATES|GROUP|PARTNERS)', re.IGNORECASE)

        looked_up = 0
        reused = 0
        failed = 0
        for idx, row in merged.iterrows():
            name = row["payee"]
            if not biz_pattern.search(name):
                continue

            cached = prior.get(name)
            if cached is not None:
                for f, v in cached.items():
                    merged.at[idx, f] = v if v != "" else None
                reused += 1
                continue

            print(f"  [{looked_up + 1}] Looking up: {name[:60]}...")
            profile = lookup_entity(name, session)
            if profile is None:
                # Failed, not missing: leave sos_looked_up blank so the next
                # rebuild retries instead of reusing an outage for 120 days.
                failed += 1
                time.sleep(1.5)
                continue
            merged.at[idx, "sos_looked_up"] = today

            if profile:
                merged.at[idx, "sos_org_number"] = profile.get("org_number", "")
                merged.at[idx, "sos_status"] = profile.get("status", "")
                merged.at[idx, "sos_standing"] = profile.get("standing", "")
                merged.at[idx, "sos_company_type"] = profile.get("company_type", "")
                merged.at[idx, "sos_industry"] = profile.get("industry", "")
                merged.at[idx, "sos_employees"] = profile.get("employees", "")
                merged.at[idx, "sos_county"] = profile.get("county", "")
                merged.at[idx, "sos_file_date"] = profile.get("file_date", "")
                merged.at[idx, "sos_principal_office"] = profile.get("principal_office", "")
                merged.at[idx, "sos_managed_by"] = profile.get("managed_by", "")
                merged.at[idx, "sos_registered_agent"] = profile.get("registered_agent", "")
                looked_up += 1
            else:
                looked_up += 1

            # Be polite to the SOS server
            time.sleep(1.5)

        found = merged["sos_org_number"].notna().sum()
        print(f"\n  SOS matches found: {found}/{looked_up + reused} "
              f"({looked_up} looked up, {reused} reused, {failed} failed and left for next time)")

    # Save
    output_cols = [
        "payee", "total_spend", "transaction_count", "years_active",
        "first_year", "last_year", "agencies_served", "agency_list",
        "funds_used", "expenditure_types",
        # Contractor license
        "CATEGORY", "DESCRIPTION", "LICENSENO", "ADDRESS1", "CITY", "STATE", "ZIPCODE",
        "EMAIL", "DAYTIMEPHONE",
    ]
    if not args.skip_sos:
        output_cols.extend(SOS_CARRY_FIELDS)

    # Keep only columns that exist
    output_cols = [c for c in output_cols if c in merged.columns]
    merged[output_cols].to_csv(args.output, index=False)
    print(f"\nSaved to {args.output} ({len(merged)} profiles)")


if __name__ == "__main__":
    main()
