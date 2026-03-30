"""
Load Louisville expenditure data into Neo4j as a context graph.

Node types:
  - Agency (canonical name)
  - Payee (vendor/recipient)
  - Fund (funding source)
  - FiscalYear
  - ExpenditureType (Operating/Capital)
  - Project (2018+ data only)

Relationships:
  - (Agency)-[:PAID {total, count}]->(Payee)
  - (Agency)-[:USED_FUND {total, count}]->(Fund)
  - (Agency)-[:SPENT_IN {total, count}]->(FiscalYear)
  - (Agency)-[:SPENT_AS {total, count}]->(ExpenditureType)
  - (Agency)-[:WORKED_ON {total, count}]->(Project)
  - (Payee)-[:RECEIVED_FROM {total, count}]->(Fund)
  - (FiscalYear)-[:HAD_SPENDING {total}]->(ExpenditureType)

Usage:
  python graph/load_graph.py [--uri bolt://localhost:7687] [--password context123]
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from neo4j import GraphDatabase
from data_model import load_all_data


def create_constraints(session):
    """Create uniqueness constraints for node types."""
    constraints = [
        "CREATE CONSTRAINT IF NOT EXISTS FOR (a:Agency) REQUIRE a.name IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (p:Payee) REQUIRE p.name IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (f:Fund) REQUIRE f.name IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (y:FiscalYear) REQUIRE y.year IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (t:ExpenditureType) REQUIRE t.name IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (p:Project) REQUIRE p.name IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (ra:RegisteredAgent) REQUIRE ra.name IS UNIQUE",
    ]
    for c in constraints:
        session.run(c)
    print("Constraints created")


def clear_graph(session):
    """Remove all existing nodes and relationships."""
    session.run("MATCH (n) DETACH DELETE n")
    print("Graph cleared")


def load_nodes(session, con):
    """Create all nodes from the expenditure data."""

    # Agencies (canonical)
    agencies = con.execute("""
        SELECT DISTINCT agency_canonical AS name FROM expenditures
        WHERE agency_canonical IS NOT NULL
    """).fetchall()
    for (name,) in agencies:
        session.run("MERGE (a:Agency {name: $name})", name=name)
    print(f"  Agencies: {len(agencies)}")

    # Payees (top 500 by total spend to keep graph manageable)
    payees = con.execute("""
        SELECT payee AS name, ROUND(COALESCE(SUM(extended_amount), 0), 2) AS total
        FROM expenditures
        WHERE payee IS NOT NULL AND is_data_artifact = FALSE
        GROUP BY payee ORDER BY total DESC LIMIT 500
    """).fetchall()
    for name, total in payees:
        session.run("MERGE (p:Payee {name: $name}) SET p.total_received = $total",
                     name=name, total=float(total))
    print(f"  Payees: {len(payees)}")

    # Funds
    funds = con.execute("""
        SELECT DISTINCT fund AS name FROM expenditures
        WHERE fund IS NOT NULL
    """).fetchall()
    for (name,) in funds:
        session.run("MERGE (f:Fund {name: $name})", name=name)
    print(f"  Funds: {len(funds)}")

    # Fiscal Years
    years = con.execute("""
        SELECT DISTINCT fiscal_year AS year FROM expenditures
        WHERE fiscal_year IS NOT NULL ORDER BY fiscal_year
    """).fetchall()
    for (year,) in years:
        session.run("MERGE (y:FiscalYear {year: $year})", year=int(year))
    print(f"  Fiscal Years: {len(years)}")

    # Expenditure Types
    types = con.execute("""
        SELECT DISTINCT expenditure_type AS name FROM expenditures
        WHERE expenditure_type IS NOT NULL
    """).fetchall()
    for (name,) in types:
        session.run("MERGE (t:ExpenditureType {name: $name})", name=name)
    print(f"  Expenditure Types: {len(types)}")

    # Projects (top 200, 2018+ only)
    projects = con.execute("""
        SELECT project AS name, ROUND(COALESCE(SUM(extended_amount), 0), 2) AS total
        FROM expenditures
        WHERE project IS NOT NULL AND is_data_artifact = FALSE
        GROUP BY project ORDER BY total DESC LIMIT 200
    """).fetchall()
    for name, total in projects:
        session.run("MERGE (p:Project {name: $name}) SET p.total_spend = $total",
                     name=name, total=float(total))
    print(f"  Projects: {len(projects)}")


def load_contractor_profiles(session, con):
    """Load contractor profile data including SOS registered agents."""
    # Check if contractor_profiles table exists
    tables = [t[0] for t in con.execute("SHOW TABLES").fetchall()]
    if "contractor_profiles" not in tables:
        print("  contractor_profiles table not found, skipping")
        return

    profiles = con.execute("""
        SELECT payee, total_spend, sos_registered_agent, sos_company_type,
               sos_employees, sos_principal_office, sos_status, sos_file_date,
               sos_org_number, sos_county, sos_managed_by
        FROM contractor_profiles
        WHERE sos_registered_agent IS NOT NULL AND sos_registered_agent != ''
    """).fetchall()

    agents_seen = set()
    for (payee, total_spend, agent, company_type, employees, office,
         status, file_date, org_num, county, managed_by) in profiles:
        # Update existing Payee node with SOS data
        session.run("""
            MERGE (p:Payee {name: $name})
            SET p.company_type = $company_type, p.employees = $employees,
                p.principal_office = $office, p.status = $status,
                p.file_date = $file_date, p.org_number = $org_num,
                p.county = $county, p.managed_by = $managed_by
        """, name=payee, company_type=company_type or "", employees=employees or "",
             office=office or "", status=status or "", file_date=file_date or "",
             org_num=org_num or "", county=county or "", managed_by=managed_by or "")

        # Extract just the agent name (before the address)
        agent_name = agent.split(" ")[0:3]  # rough: first 3 words
        agent_name = " ".join(agent_name).strip().rstrip(",")

        # Create RegisteredAgent node and relationship
        session.run("""
            MERGE (ra:RegisteredAgent {name: $agent_full})
            MERGE (p:Payee {name: $payee})
            MERGE (p)-[:HAS_REGISTERED_AGENT]->(ra)
        """, agent_full=agent, payee=payee)
        agents_seen.add(agent)

    print(f"  Contractor profiles: {len(profiles)} payees enriched, {len(agents_seen)} unique registered agents")


def load_relationships(session, con):
    """Create relationships with aggregated amounts."""

    # Agency -> Payee (top 2000 pairs by spend)
    print("  Loading Agency-Payee relationships...")
    agency_payee = con.execute("""
        SELECT agency_canonical AS agency, payee,
               ROUND(COALESCE(SUM(extended_amount), 0), 2) AS total, COUNT(*) AS count
        FROM expenditures
        WHERE agency_canonical IS NOT NULL AND payee IS NOT NULL AND is_data_artifact = FALSE
        GROUP BY agency_canonical, payee
        ORDER BY total DESC LIMIT 2000
    """).fetchall()
    for agency, payee, total, count in agency_payee:
        session.run("""
            MATCH (a:Agency {name: $agency}), (p:Payee {name: $payee})
            MERGE (a)-[r:PAID]->(p)
            SET r.total = $total, r.count = $count
        """, agency=agency, payee=payee, total=float(total), count=int(count))
    print(f"    {len(agency_payee)} relationships")

    # Agency -> Fund
    print("  Loading Agency-Fund relationships...")
    agency_fund = con.execute("""
        SELECT agency_canonical AS agency, fund,
               ROUND(COALESCE(SUM(extended_amount), 0), 2) AS total, COUNT(*) AS count
        FROM expenditures
        WHERE agency_canonical IS NOT NULL AND fund IS NOT NULL AND is_data_artifact = FALSE
        GROUP BY agency_canonical, fund
    """).fetchall()
    for agency, fund, total, count in agency_fund:
        session.run("""
            MATCH (a:Agency {name: $agency}), (f:Fund {name: $fund})
            MERGE (a)-[r:USED_FUND]->(f)
            SET r.total = $total, r.count = $count
        """, agency=agency, fund=fund, total=float(total), count=int(count))
    print(f"    {len(agency_fund)} relationships")

    # Agency -> FiscalYear
    print("  Loading Agency-FiscalYear relationships...")
    agency_year = con.execute("""
        SELECT agency_canonical AS agency, fiscal_year,
               ROUND(COALESCE(SUM(extended_amount), 0), 2) AS total, COUNT(*) AS count
        FROM expenditures
        WHERE agency_canonical IS NOT NULL AND fiscal_year IS NOT NULL AND is_data_artifact = FALSE
        GROUP BY agency_canonical, fiscal_year
    """).fetchall()
    for agency, year, total, count in agency_year:
        session.run("""
            MATCH (a:Agency {name: $agency}), (y:FiscalYear {year: $year})
            MERGE (a)-[r:SPENT_IN]->(y)
            SET r.total = $total, r.count = $count
        """, agency=agency, year=int(year), total=float(total), count=int(count))
    print(f"    {len(agency_year)} relationships")

    # Agency -> ExpenditureType
    print("  Loading Agency-ExpenditureType relationships...")
    agency_type = con.execute("""
        SELECT agency_canonical AS agency, expenditure_type,
               ROUND(COALESCE(SUM(extended_amount), 0), 2) AS total, COUNT(*) AS count
        FROM expenditures
        WHERE agency_canonical IS NOT NULL AND expenditure_type IS NOT NULL AND is_data_artifact = FALSE
        GROUP BY agency_canonical, expenditure_type
    """).fetchall()
    for agency, etype, total, count in agency_type:
        session.run("""
            MATCH (a:Agency {name: $agency}), (t:ExpenditureType {name: $etype})
            MERGE (a)-[r:SPENT_AS]->(t)
            SET r.total = $total, r.count = $count
        """, agency=agency, etype=etype, total=float(total), count=int(count))
    print(f"    {len(agency_type)} relationships")

    # Agency -> Project (top 500 pairs)
    print("  Loading Agency-Project relationships...")
    agency_project = con.execute("""
        SELECT agency_canonical AS agency, project,
               ROUND(COALESCE(SUM(extended_amount), 0), 2) AS total, COUNT(*) AS count
        FROM expenditures
        WHERE agency_canonical IS NOT NULL AND project IS NOT NULL AND is_data_artifact = FALSE
        GROUP BY agency_canonical, project
        ORDER BY total DESC LIMIT 500
    """).fetchall()
    for agency, project, total, count in agency_project:
        session.run("""
            MATCH (a:Agency {name: $agency}), (p:Project {name: $project})
            MERGE (a)-[r:WORKED_ON]->(p)
            SET r.total = $total, r.count = $count
        """, agency=agency, project=project, total=float(total), count=int(count))
    print(f"    {len(agency_project)} relationships")

    # FiscalYear -> ExpenditureType
    print("  Loading FiscalYear-ExpenditureType relationships...")
    year_type = con.execute("""
        SELECT fiscal_year, expenditure_type,
               ROUND(COALESCE(SUM(extended_amount), 0), 2) AS total
        FROM expenditures
        WHERE fiscal_year IS NOT NULL AND expenditure_type IS NOT NULL AND is_data_artifact = FALSE
        GROUP BY fiscal_year, expenditure_type
    """).fetchall()
    for year, etype, total in year_type:
        session.run("""
            MATCH (y:FiscalYear {year: $year}), (t:ExpenditureType {name: $etype})
            MERGE (y)-[r:HAD_SPENDING]->(t)
            SET r.total = $total
        """, year=int(year), etype=etype, total=float(total))
    print(f"    {len(year_type)} relationships")


def main():
    parser = argparse.ArgumentParser(description="Load Louisville expenditure graph into Neo4j")
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD", "context123"))
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    print(f"Connecting to Neo4j at {args.uri}...")
    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))

    print("Loading data into DuckDB...")
    con = load_all_data(args.data_dir)

    with driver.session() as session:
        print("\nClearing existing graph...")
        clear_graph(session)

        print("Creating constraints...")
        create_constraints(session)

        print("\nCreating nodes...")
        load_nodes(session, con)

        print("\nLoading contractor profiles...")
        load_contractor_profiles(session, con)

        print("\nCreating relationships...")
        load_relationships(session, con)

    driver.close()
    print("\nGraph load complete!")

    # Print summary
    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    with driver.session() as session:
        nodes = session.run("MATCH (n) RETURN count(n) AS count").single()["count"]
        rels = session.run("MATCH ()-[r]->() RETURN count(r) AS count").single()["count"]
        print(f"Total: {nodes:,} nodes, {rels:,} relationships")
    driver.close()


if __name__ == "__main__":
    main()
