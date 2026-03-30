"""
Scrape current officers from KY Secretary of State for companies that use
registered agent services (CT Corporation, CSC, etc.) instead of named individuals.

Uses the spagents MCP browser tool via HTTP to handle JavaScript postbacks.
Falls back to extracting officers from the page text if available.

Usage:
    python graph/scrape_officers.py [--input data/contractor_profiles.csv] [--output data/contractor_profiles.csv]
"""

import argparse
import csv
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SOS_SEARCH_URL = "https://sosbes.sos.ky.gov/BusSearchNProfile/search.aspx"
SOS_PROFILE_URL = "https://sosbes.sos.ky.gov/BusSearchNProfile/Profile.aspx"

AGENT_SERVICES = [
    "C T CORPORATION",
    "CT CORPORATION",
    "CORPORATION SERVICE",
    "CSC ",
    "NATIONAL REGISTERED AGENTS",
    "COGENCY GLOBAL",
    "REGISTERED AGENTS INC",
    "CAPITOL SERVICES",
    "LEGALINC",
    "INCORP SERVICES",
]


def is_agent_service(agent_name: str) -> bool:
    """Check if a registered agent is a corporate agent service rather than a named individual."""
    if not agent_name:
        return False
    upper = agent_name.upper()
    return any(svc in upper for svc in AGENT_SERVICES)


def get_ctr_for_company(name: str, session: requests.Session) -> str:
    """Search KY SOS and return the ctr ID for the best matching entity."""
    resp = session.get(SOS_SEARCH_URL, timeout=15)
    soup = BeautifulSoup(resp.text, "html.parser")

    vs = soup.find("input", {"name": "__VIEWSTATE"})
    if not vs:
        return ""

    data = {
        "__VIEWSTATE": vs["value"],
        "ctl00$MainContent$txtSearch": name,
        "ctl00$MainContent$ddlSearchBy": "Business Name or Organization Number",
        "ctl00$MainContent$BSearch": "Search",
    }
    vsg = soup.find("input", {"name": "__VIEWSTATEGENERATOR"})
    if vsg:
        data["__VIEWSTATEGENERATOR"] = vsg["value"]

    resp = session.post(SOS_SEARCH_URL, data=data, timeout=15)
    soup = BeautifulSoup(resp.text, "html.parser")

    table = soup.find("table", {"id": "MainContent_gvSearchResults"})
    if not table:
        return ""

    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) >= 4:
            link = cells[0].find("a")
            status = cells[2].get_text(strip=True)
            if link and "Active" in status and "ctr=" in link.get("href", ""):
                return link["href"].split("ctr=")[1].split("&")[0]
    return ""


def scrape_officers_from_page(ctr: str, session: requests.Session) -> list[dict]:
    """
    Get current officers by loading the profile page and parsing officer data.
    Uses a two-step approach: first GET to get ViewState, then POST to click the officers button.
    """
    # Step 1: Load the profile page
    url = f"{SOS_PROFILE_URL}?ctr={ctr}"
    resp = session.get(url, timeout=15)
    soup = BeautifulSoup(resp.text, "html.parser")

    # Step 2: Try to find officer links in the page
    # After clicking "Show Current Officers", officers appear as links with pattern:
    # Offsearch.aspx?sf=FirstName&sm=MiddleInitial&sl=LastName
    officers = []

    # Try direct text extraction if officers are already visible
    text = soup.get_text()
    if "Current Officers" in text:
        # Parse officer section from text
        officer_section = text.split("Current Officers")[-1].split("Kentucky Unbridled")[0]
        lines = [l.strip() for l in officer_section.split("\n") if l.strip()]
        # Skip header row "Title Officer"
        i = 0
        while i < len(lines):
            if lines[i] in ("Title", "Officer"):
                i += 1
                continue
            title = lines[i]
            if i + 1 < len(lines) and title in ("President", "Vice President", "Secretary",
                "Treasurer", "Director", "Member", "Manager", "CEO", "CFO", "COO",
                "Chairman", "Chief Executive Officer", "Chief Financial Officer",
                "General Counsel", "Partner", "Organizer"):
                name = lines[i + 1] if i + 1 < len(lines) else ""
                if name and name not in ("Kentucky Unbridled Spirit", "Privacy", "Security"):
                    officers.append({"title": title, "name": name})
                i += 2
            else:
                i += 1

    return officers


def scrape_officers_browser(ctr: str) -> list[dict]:
    """
    Use spagents browser to click the officers button and extract data.
    Requires spagents MCP to be running.
    """
    try:
        # Browse to profile page
        resp = requests.post("http://localhost:3002/browse", json={
            "url": f"{SOS_PROFILE_URL}/?ctr={ctr}"
        }, timeout=30)
        data = resp.json()
        session_id = data.get("session_id", "")

        if not session_id:
            return []

        # Click "Show Current Officers" button
        resp = requests.post("http://localhost:3002/click", json={
            "session_id": session_id,
            "selector": "#MainContent_BtnCurrent"
        }, timeout=30)
        data = resp.json()

        # Extract officers from the page text
        text = data.get("content", {}).get("main_text", "")
        officers = []

        if "Current Officers" in text:
            section = text.split("Current Officers")[-1].split("Kentucky Unbridled")[0]
            lines = [l.strip() for l in section.split("\n") if l.strip()]
            i = 0
            while i < len(lines):
                if lines[i] in ("Title", "Officer"):
                    i += 1
                    continue
                title = lines[i]
                if title in ("President", "Vice President", "Secretary",
                    "Treasurer", "Director", "Member", "Manager", "CEO", "CFO", "COO",
                    "Chairman", "Chief Executive Officer", "Chief Financial Officer",
                    "General Counsel", "Partner", "Organizer"):
                    name = lines[i + 1] if i + 1 < len(lines) else ""
                    if name and name not in ("Kentucky Unbridled Spirit", "Privacy", "Security"):
                        officers.append({"title": title, "name": name})
                    i += 2
                else:
                    i += 1

        # Close session
        try:
            requests.post("http://localhost:3002/close_session", json={"session_id": session_id}, timeout=5)
        except Exception:
            pass

        return officers
    except Exception as e:
        print(f"    Browser scrape failed: {e}")
        return []


def format_officers(officers: list[dict]) -> str:
    """Format officer list as a readable string."""
    if not officers:
        return ""
    seen = set()
    unique = []
    for o in officers:
        key = f"{o['title']}:{o['name']}"
        if key not in seen:
            seen.add(key)
            unique.append(o)
    return "; ".join(f"{o['title']}: {o['name']}" for o in unique)


def main():
    parser = argparse.ArgumentParser(description="Scrape officers for agent-service companies")
    parser.add_argument("--input", default="data/contractor_profiles.csv")
    parser.add_argument("--output", default="data/contractor_profiles.csv")
    parser.add_argument("--use-browser", action="store_true", help="Use spagents browser for officer scraping")
    args = parser.parse_args()

    import pandas as pd
    df = pd.read_csv(args.input)

    if "sos_registered_agent" not in df.columns:
        print("No SOS data in profiles — run build_contractor_profiles.py first (without --skip-sos)")
        return

    if "sos_officers" not in df.columns:
        df["sos_officers"] = None

    # Find companies using agent services
    mask = df["sos_registered_agent"].apply(lambda x: is_agent_service(str(x)) if pd.notna(x) else False)
    targets = df[mask]
    print(f"Found {len(targets)} companies using registered agent services")

    session = requests.Session()
    session.headers.update({"User-Agent": "Louisville-OpenData-Research/1.0"})

    scraped = 0
    for idx, row in targets.iterrows():
        name = row["payee"]
        print(f"  [{scraped + 1}/{len(targets)}] {name[:50]}...")

        # Get the ctr ID
        ctr = get_ctr_for_company(name, session)
        if not ctr:
            print(f"    No SOS match found")
            scraped += 1
            time.sleep(1)
            continue

        # Try browser-based scraping first if available
        officers = []
        if args.use_browser:
            officers = scrape_officers_browser(ctr)

        # Fall back to HTTP-based scraping
        if not officers:
            officers = scrape_officers_from_page(ctr, session)

        if officers:
            formatted = format_officers(officers)
            df.at[idx, "sos_officers"] = formatted
            print(f"    Found {len(officers)} officers: {formatted[:80]}...")
        else:
            print(f"    No officers found")

        scraped += 1
        time.sleep(1.5)

    found = df["sos_officers"].notna().sum()
    print(f"\nOfficers found for {found} companies")

    df.to_csv(args.output, index=False)
    print(f"Saved to {args.output}")


if __name__ == "__main__":
    main()
