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
import os
import re
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


def lookup_entity(name: str, session: requests.Session) -> dict:
    """Search and get profile for a business entity. Returns best match or empty dict."""
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
        print(f"    SOS lookup failed for {name}: {e}")
        return {}


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Build contractor profiles")
    parser.add_argument("--top", type=int, default=200, help="Number of top payees to profile")
    parser.add_argument("--skip-sos", action="store_true", help="Skip KY SOS lookups")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--output", default="data/contractor_profiles.csv")
    args = parser.parse_args()

    print("Loading expenditure data...")
    con = load_all_data(args.data_dir)

    # Get top payees by spend (excluding individuals — rough filter on LLC, INC, etc.)
    print(f"\nBuilding profiles for top {args.top} payees...")
    payees = con.execute(f"""
        SELECT
            e.payee,
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
        WHERE e.payee IS NOT NULL AND e.is_data_artifact = FALSE
            AND e.extended_amount > 0
        GROUP BY e.payee
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
    merged = payees.merge(contractors, left_on="payee_lower", right_on="name_lower", how="left")
    merged = merged.drop(columns=["payee_lower", "name_lower"], errors="ignore")

    licensed = merged["LICENSENO"].notna().sum()
    print(f"  {licensed}/{len(merged)} matched to active contractor licenses")

    # KY SOS lookups
    if not args.skip_sos:
        print(f"\nLooking up top payees on KY Secretary of State...")
        session = requests.Session()
        session.headers.update({"User-Agent": "Louisville-OpenData-Research/1.0"})

        sos_fields = [
            "sos_org_number", "sos_status", "sos_standing", "sos_company_type",
            "sos_industry", "sos_employees", "sos_county", "sos_file_date",
            "sos_principal_office", "sos_managed_by", "sos_registered_agent",
        ]
        for f in sos_fields:
            merged[f] = None

        # Only look up entities that look like businesses (contain LLC, INC, CO, CORP, etc.)
        biz_pattern = re.compile(r'\b(LLC|INC|CORP|CO\b|LTD|LP|COMPANY|ENTERPRISES|ASSOCIATES|GROUP|PARTNERS)', re.IGNORECASE)

        looked_up = 0
        for idx, row in merged.iterrows():
            name = row["payee"]
            if not biz_pattern.search(name):
                continue

            print(f"  [{looked_up + 1}] Looking up: {name[:60]}...")
            profile = lookup_entity(name, session)

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
        print(f"\n  SOS matches found: {found}/{looked_up} lookups")

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
        output_cols.extend([
            "sos_org_number", "sos_status", "sos_standing", "sos_company_type",
            "sos_industry", "sos_employees", "sos_county", "sos_file_date",
            "sos_principal_office", "sos_managed_by", "sos_registered_agent",
        ])

    # Keep only columns that exist
    output_cols = [c for c in output_cols if c in merged.columns]
    merged[output_cols].to_csv(args.output, index=False)
    print(f"\nSaved to {args.output} ({len(merged)} profiles)")


if __name__ == "__main__":
    main()
