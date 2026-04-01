"""
Data model for Louisville Open Data analytics.

Loads and unifies expenditure data across schema eras (2008-2017 vs 2018+),
loads enrichment datasets, and provides a comprehensive data dictionary
with semantic column names.
"""

import glob
import os

import duckdb
import pandas as pd


# ── Schema Mapping ───────────────────────────────────────────────────────────

# Old schema (2008-2017) -> unified schema
OLD_TO_NEW = {
    "Fiscal_Year": "fiscal_year",
    "Vendor_Name": "payee",
    "InvoiceID": "invoice_number",
    "InvoiceDt": "invoice_date",
    "InvoiceAmt": "invoice_amount",
    "CheckID": "payment_number",
    "CheckDt": "payment_date",
    "CheckAmt": "payment_amount",
    "CheckVoidDt": "payment_void_date",
    "Agency_Name": "agency",
    "Sub_Agency_Name": "sub_agency",
    "DepartmentName": "department",
    "Sub_DepartmentName": "sub_department",
    "Budget_Type": "expenditure_type",
    "Category": "expenditure_category",
    "Sub_Category": "spend_category",
    "Stimulus_Type": "stimulus_type",
    "Funding_Source": "fund",
    "DistributionAmt": "extended_amount",
}


# ── Semantic Labels ──────────────────────────────────────────────────────────

# expenditures table
EXPENDITURE_LABELS = {
    "fiscal_year": "Fiscal Year",
    "invoice_date": "Invoice Date",
    "invoice_number": "Invoice Number",
    "invoice_amount": "Invoice Amount",
    "payee": "Payee",
    "payment_date": "Payment Date",
    "payment_number": "Payment Number",
    "payment_amount": "Payment Amount",
    "payment_void_date": "Payment Void Date",
    "agency": "Agency",
    "sub_agency": "Sub Agency",
    "department": "Department",
    "sub_department": "Sub Department",
    "expenditure_type": "Expenditure Type",
    "expenditure_category": "Expenditure Category",
    "spend_category": "Spend Category",
    "stimulus_type": "Stimulus Type",
    "cost_center": "Cost Center",
    "project": "Project",
    "program": "Program",
    "grant_": "Grant",
    "fund": "Fund",
    "financing_source": "Financing Source",
    "region": "Region",
    "extended_amount": "Extended Amount",
    "agency_canonical": "Agency (Normalized)",
    "payee_canonical": "Payee (Normalized)",
    "is_offsetting": "Offsetting Entry",
    "is_data_artifact": "Data Artifact",
}

# salary_data table
SALARY_LABELS = {
    "CalYear": "Calendar Year",
    "Employee_Name": "Employee Name",
    "Department": "Department",
    "jobTitle": "Job Title",
    "Annual_Rate": "Annual Rate",
    "Regular_Rate": "Regular Pay",
    "Overtime_Rate": "Overtime Pay",
    "Incentive_Allowance": "Incentive Allowance",
    "Other": "Other Pay",
    "YTD_Total": "Year-to-Date Total",
}

# capital_projects table
CAPITAL_PROJECT_LABELS = {
    "Project_Na": "Project Name",
    "Agency_Org": "Agency",
    "Project": "Project Type",
    "Council_Di": "Council District",
    "Allocation": "Allocation",
    "Description": "Description",
    "Priority_Area": "Priority Area",
    "Start_Date": "Start Date",
    "End_Date": "End Date",
    "Chief": "Chief",
    "Executive_": "Executive",
}

# active_contractors table
CONTRACTOR_LABELS = {
    "FULLNAME": "Contractor Name",
    "LICENSENO": "License Number",
    "CATEGORY": "Category",
    "DESCRIPTION": "Description",
    "ADDRESS1": "Address",
    "CITY": "City",
    "STATE": "State",
    "ZIPCODE": "Zip Code",
    "EXPIRATIONDATE": "License Expiration",
    "EMAIL": "Email",
    "DAYTIMEPHONE": "Phone",
}

# staff_demographics table
DEMOGRAPHICS_LABELS = {
    "DepartmentID": "Department ID",
    "Department": "Department",
    "Gender": "Gender",
    "EthnicGroup": "Ethnic Group",
    "HighestEducationLevel": "Education Level",
    "NumberOfEmployees": "Number of Employees",
}

# hr_requisitions table
HR_LABELS = {
    "DEPARTMENT": "Department",
    "JOB_TITLE": "Job Title",
    "JOB_CODE": "Job Code",
    "UNION_": "Union",
    "HIRE_STATUS": "Hire Status",
    "TYPE_OF_HIRE": "Hire Type",
    "WORK_LOCATION": "Work Location",
}

# contractor_profiles table
CONTRACTOR_PROFILE_LABELS = {
    "payee": "Payee",
    "total_spend": "Total Spend",
    "transaction_count": "Transaction Count",
    "years_active": "Years Active",
    "first_year": "First Year",
    "last_year": "Last Year",
    "agencies_served": "Agencies Served",
    "agency_list": "Agency List",
    "funds_used": "Funds Used",
    "expenditure_types": "Expenditure Types",
    "CATEGORY": "Contractor Category",
    "DESCRIPTION": "Contractor Description",
    "LICENSENO": "License Number",
    "ADDRESS1": "Address",
    "CITY": "City",
    "STATE": "State",
    "ZIPCODE": "Zip Code",
    "EMAIL": "Email",
    "DAYTIMEPHONE": "Phone",
    "sos_org_number": "SOS Org Number",
    "sos_status": "Business Status",
    "sos_standing": "Business Standing",
    "sos_company_type": "Company Type",
    "sos_industry": "Industry",
    "sos_employees": "Employee Size",
    "sos_county": "County",
    "sos_file_date": "Date Filed",
    "sos_principal_office": "Principal Office",
    "sos_managed_by": "Managed By",
    "sos_registered_agent": "Registered Agent",
    "sos_officers": "Current Officers",
}

# Master lookup for all tables
ALL_LABELS = {
    "expenditures": EXPENDITURE_LABELS,
    "salary_data": SALARY_LABELS,
    "capital_projects": CAPITAL_PROJECT_LABELS,
    "active_contractors": CONTRACTOR_LABELS,
    "staff_demographics": DEMOGRAPHICS_LABELS,
    "hr_requisitions": HR_LABELS,
    "contractor_profiles": CONTRACTOR_PROFILE_LABELS,
}


# ── Data Dictionary ──────────────────────────────────────────────────────────

DATA_DICTIONARY = {
    "expenditures": {
        "description": "Louisville Metro government expenditure transactions, FY2008-FY2026. Unified from two schema eras: 2008-2017 (old format) and 2018+ (current format).",
        "record_scope": "Each row is a single invoice line item / payment distribution.",
        "columns": {
            "fiscal_year": "The fiscal year of the expenditure (July-June cycle). Integer.",
            "invoice_date": "Date the invoice was issued.",
            "invoice_number": "Unique identifier for the invoice.",
            "invoice_amount": "Total dollar amount on the invoice.",
            "payee": "Vendor or recipient name.",
            "payment_date": "Date payment was issued.",
            "payment_number": "Unique identifier for the payment/check.",
            "payment_amount": "Total check amount (2008-2017 data only).",
            "payment_void_date": "Date payment was voided, if applicable (2008-2017 data only).",
            "agency": "Metro government agency responsible for the expenditure.",
            "sub_agency": "Sub-agency detail (2008-2017 data only).",
            "department": "Department within the agency (2008-2017 data only).",
            "sub_department": "Sub-department detail (2008-2017 data only).",
            "expenditure_type": "High-level type: Operating or Capital.",
            "expenditure_category": "Category of expenditure.",
            "spend_category": "Detailed spend classification.",
            "stimulus_type": "Federal stimulus funding type, if applicable (2008-2017 data only).",
            "cost_center": "Organizational cost center code and name (2018+ data only).",
            "project": "Project name or code (2018+ data only).",
            "program": "Program name or code (2018+ data only).",
            "grant_": "Grant identifier and description (2018+ data only).",
            "fund": "Funding source (e.g., General Fund, Grant Fund, Capital Project Fund).",
            "financing_source": "Specific financing source detail (2018+ data only).",
            "region": "Council district or geographic region (2018+ data only).",
            "extended_amount": "Distributed/allocated amount for this line item. Use this for spend analysis.",
            "agency_canonical": "Normalized agency name. Use this instead of 'agency' for aggregations to avoid duplicate entities from naming variations.",
            "payee_canonical": "Normalized payee/vendor name. Maps abbreviations and variants to canonical names (e.g., 'LG&E' and 'LOUISVILLE GAS & ELECTRIC COMPANY' both become 'Louisville Gas & Electric Company'). Use this instead of 'payee' when grouping or filtering by vendor.",
        },
    },
    "salary_data": {
        "description": "Annual salary and compensation data for Louisville Metro employees.",
        "record_scope": "Each row is one employee's compensation for a calendar year.",
        "joins": "Join to expenditures on Department <-> agency (fuzzy match needed).",
        "columns": {
            "CalYear": "Calendar year of the salary record.",
            "Employee_Name": "Full name of the employee.",
            "Department": "Department name.",
            "jobTitle": "Job title/position.",
            "Annual_Rate": "Annual salary rate.",
            "Regular_Rate": "Regular pay received.",
            "Overtime_Rate": "Overtime pay received.",
            "Incentive_Allowance": "Incentive and allowance pay.",
            "Other": "Other compensation.",
            "YTD_Total": "Total year-to-date compensation.",
        },
    },
    "capital_projects": {
        "description": "Capital improvement projects with geographic locations and allocations.",
        "record_scope": "Each row is a capital project.",
        "joins": "Join to expenditures on Project_Na <-> project, Agency_Org <-> agency, Council_Di <-> region.",
        "columns": {
            "Project_Na": "Project name.",
            "Agency_Org": "Responsible agency/organization.",
            "Project": "Project category (e.g., Park, Library, Road).",
            "Council_Di": "Council district number.",
            "Allocation": "Budget allocation amount.",
            "Description": "Project description.",
            "Priority_Area": "Priority area designation.",
            "Start_Date": "Project start date.",
            "End_Date": "Project end date.",
            "Chief": "Chief officer responsible.",
            "Executive_": "Executive sponsor.",
        },
    },
    "active_contractors": {
        "description": "Licensed contractors registered with Louisville Metro.",
        "record_scope": "Each row is a licensed contractor.",
        "joins": "Join to expenditures on FULLNAME <-> payee (fuzzy match needed).",
        "columns": {
            "FULLNAME": "Contractor business name.",
            "LICENSENO": "License number.",
            "CATEGORY": "Contractor category code.",
            "DESCRIPTION": "Category description (e.g., General Building, Electrical).",
            "EXPIRATIONDATE": "License expiration date.",
        },
    },
    "staff_demographics": {
        "description": "Workforce demographics by department, gender, ethnicity, and education level.",
        "record_scope": "Each row is a demographic group count within a department.",
        "joins": "Join to expenditures on Department <-> agency.",
        "columns": {
            "Department": "Department name.",
            "Gender": "Gender category.",
            "EthnicGroup": "Ethnic group category.",
            "HighestEducationLevel": "Highest education level.",
            "NumberOfEmployees": "Count of employees in this group.",
        },
    },
    "hr_requisitions": {
        "description": "Human resources hiring requisition log.",
        "record_scope": "Each row is a hiring requisition.",
        "joins": "Join to expenditures on DEPARTMENT <-> agency.",
        "columns": {
            "DEPARTMENT": "Department with the open position.",
            "JOB_TITLE": "Position title.",
            "JOB_CODE": "Job classification code.",
            "UNION_": "Union affiliation.",
            "HIRE_STATUS": "Current hiring status.",
            "TYPE_OF_HIRE": "Type of hire (new, replacement, etc.).",
            "WORK_LOCATION": "Work location.",
        },
    },
    "contractor_profiles": {
        "description": "Enriched profiles for top 200 payees by spend. Combines expenditure history, contractor license data, and KY Secretary of State business entity records.",
        "record_scope": "Each row is a payee/contractor with aggregated spend data and business registration details.",
        "joins": "Join to expenditures on payee <-> payee. This is the primary table for questions about contractors, vendors, company ownership, registered agents, and business details.",
        "columns": {
            "payee": "Payee/vendor name as it appears in expenditure records.",
            "total_spend": "Total expenditure amount received across all years.",
            "transaction_count": "Number of expenditure transactions.",
            "years_active": "Number of distinct fiscal years with transactions.",
            "first_year": "First fiscal year with transactions.",
            "last_year": "Most recent fiscal year with transactions.",
            "agencies_served": "Number of distinct agencies this payee received payments from.",
            "agency_list": "Semicolon-separated list of agencies.",
            "CATEGORY": "Contractor license category (from Louisville Metro).",
            "DESCRIPTION": "Contractor license description.",
            "LICENSENO": "Louisville Metro contractor license number.",
            "sos_org_number": "Kentucky Secretary of State organization number.",
            "sos_status": "Business entity status (e.g., A - Active).",
            "sos_standing": "Business standing (e.g., G - Good).",
            "sos_company_type": "Company type (e.g., Kentucky LLC, Kentucky Corporation).",
            "sos_industry": "Industry classification from KY SOS.",
            "sos_employees": "Employee size category (Small, Medium, Large 100+).",
            "sos_county": "Primary county of the business.",
            "sos_file_date": "Date the business was filed/incorporated.",
            "sos_principal_office": "Principal office address.",
            "sos_managed_by": "Management structure (Members, Managers, etc.).",
            "sos_registered_agent": "Name and address of the registered agent — the person legally responsible for receiving official documents on behalf of the company.",
            "sos_officers": "Current officers and directors from KY Secretary of State filings. Format: 'Title: Name; Title: Name'. Includes President, VP, Secretary, Treasurer, Directors, Members, Managers. Available for ~31 companies that use corporate agent services (CT Corporation, etc.) as their registered agent.",
        },
    },
}


# ── Loader ───────────────────────────────────────────────────────────────────

def load_all_data(data_dir: str = "data") -> duckdb.DuckDBPyConnection:
    """Load all datasets into DuckDB with unified schema."""
    con = duckdb.connect()

    # ── Load expenditure data ────────────────────────────────────────────
    # New era: 2018-2026 (lowercase column names)
    new_era_years = range(2018, 2027)
    new_era_files = [
        os.path.join(data_dir, f"eExpenditures_{y}.csv")
        for y in new_era_years
        if os.path.exists(os.path.join(data_dir, f"eExpenditures_{y}.csv"))
    ]
    # Old era: 2008-2017 (different schema with Fiscal_Year, Agency_Name, etc.)
    old_era_years = range(2008, 2018)
    old_era_files = [
        os.path.join(data_dir, f"eExpenditures_{y}.csv")
        for y in old_era_years
        if os.path.exists(os.path.join(data_dir, f"eExpenditures_{y}.csv"))
    ]

    loaded_years = []

    # Load new era (2018+) — these share the same schema
    if new_era_files:
        file_list = ", ".join(f"'{f}'" for f in new_era_files)
        con.execute(f"""
            CREATE TABLE expenditures AS
            SELECT * FROM read_csv_auto([{file_list}], union_by_name=true)
        """)
        for f in new_era_files:
            year = f.split("_")[-1].replace(".csv", "")
            loaded_years.append(year)

    # Load old era (2008-2017) — needs column mapping
    for csv_path in old_era_files:
        year = csv_path.split("_")[-1].replace(".csv", "")
        df = pd.read_csv(csv_path)
        # Rename columns to unified schema
        df = df.rename(columns=OLD_TO_NEW)
        # Drop ObjectId to avoid conflicts
        df = df.drop(columns=["ObjectId"], errors="ignore")
        # Ensure fiscal_year is integer
        if "fiscal_year" in df.columns:
            df["fiscal_year"] = pd.to_numeric(df["fiscal_year"], errors="coerce").astype("Int64")
        # Convert epoch timestamps to date strings
        for date_col in ["invoice_date", "payment_date", "payment_void_date"]:
            if date_col in df.columns:
                df[date_col] = pd.to_datetime(df[date_col], unit="ms", errors="coerce").dt.strftime("%Y-%m-%d")

        if "expenditures" not in [t[0] for t in con.execute("SHOW TABLES").fetchall()]:
            con.execute("CREATE TABLE expenditures AS SELECT * FROM df")
        else:
            # Align columns to match existing table schema
            existing_cols = [c[0] for c in con.execute("DESCRIBE expenditures").fetchall()]
            for col in existing_cols:
                if col not in df.columns:
                    df[col] = None
            df = df[existing_cols]
            con.execute("INSERT INTO expenditures SELECT * FROM df")
        loaded_years.append(year)

    total = con.execute("SELECT COUNT(*) FROM expenditures").fetchone()[0]
    print(f"Expenditures: {total:,} rows across {len(loaded_years)} fiscal years ({', '.join(sorted(loaded_years))})")

    # ── Normalize agency names ────────────────────────────────────────────
    from agency_mapping import AGENCY_MAP
    # Build a CASE statement for the mapping
    cases = "\n".join(f"WHEN agency = '{k.replace(chr(39), chr(39)+chr(39))}' THEN '{v.replace(chr(39), chr(39)+chr(39))}'" for k, v in AGENCY_MAP.items())
    con.execute(f"""
        ALTER TABLE expenditures ADD COLUMN IF NOT EXISTS agency_canonical VARCHAR;
        UPDATE expenditures SET agency_canonical = CASE {cases} ELSE agency END;
    """)
    canonical_count = con.execute("SELECT COUNT(DISTINCT agency_canonical) FROM expenditures").fetchone()[0]
    print(f"Agency normalization: 98 variants -> {canonical_count} canonical names")

    # ── Normalize payee names ─────────────────────────────────────────────
    from payee_mapping import PAYEE_MAP, PAYEE_PREFIX_MAP
    # Direct mappings
    cases = "\n".join(f"WHEN UPPER(payee) = '{k.upper().replace(chr(39), chr(39)+chr(39))}' THEN '{v.replace(chr(39), chr(39)+chr(39))}'" for k, v in PAYEE_MAP.items())
    # Prefix mappings
    prefix_cases = "\n".join(f"WHEN UPPER(payee) LIKE '{p.upper().replace(chr(39), chr(39)+chr(39))}%' THEN '{v.replace(chr(39), chr(39)+chr(39))}'" for p, v in PAYEE_PREFIX_MAP.items())
    con.execute(f"""
        ALTER TABLE expenditures ADD COLUMN IF NOT EXISTS payee_canonical VARCHAR;
        UPDATE expenditures SET payee_canonical = CASE {cases} {prefix_cases} ELSE payee END;
    """)
    payee_before = con.execute("SELECT COUNT(DISTINCT payee) FROM expenditures WHERE payee IS NOT NULL").fetchone()[0]
    payee_after = con.execute("SELECT COUNT(DISTINCT payee_canonical) FROM expenditures WHERE payee_canonical IS NOT NULL").fetchone()[0]
    print(f"Payee normalization: {payee_before:,} variants -> {payee_after:,} canonical names")

    # ── Data quality cleaning ────────────────────────────────────────────
    # Add net_amount: for invoices with offsetting entries, compute net per invoice
    # Flag rows that are part of zero-sum offsetting pairs
    con.execute("""
        ALTER TABLE expenditures ADD COLUMN IF NOT EXISTS is_offsetting BOOLEAN DEFAULT FALSE;
        UPDATE expenditures SET is_offsetting = TRUE
        WHERE invoice_number IN (
            SELECT invoice_number FROM expenditures
            WHERE extended_amount IS NOT NULL
            GROUP BY invoice_number
            HAVING ABS(SUM(extended_amount)) < 0.01 AND COUNT(*) > 1
        );
    """)
    # Flag extreme outliers (|extended_amount| > 100M) that have offsetting counterparts
    con.execute("""
        ALTER TABLE expenditures ADD COLUMN IF NOT EXISTS is_data_artifact BOOLEAN DEFAULT FALSE;
        UPDATE expenditures SET is_data_artifact = TRUE
        WHERE ABS(extended_amount) > 100000000
        AND invoice_number IN (
            SELECT invoice_number FROM expenditures
            GROUP BY invoice_number
            HAVING ABS(SUM(extended_amount)) < ABS(MAX(extended_amount))
        );
    """)
    offset_count = con.execute("SELECT COUNT(*) FROM expenditures WHERE is_offsetting").fetchone()[0]
    artifact_count = con.execute("SELECT COUNT(*) FROM expenditures WHERE is_data_artifact").fetchone()[0]
    print(f"Data quality: {offset_count} offsetting rows flagged, {artifact_count} data artifacts flagged")

    # ── Load enrichment tables ───────────────────────────────────────────
    enrichment = {
        "salary_data": "salary_data.csv",
        "capital_projects": "capital_projects.csv",
        "active_contractors": "active_contractors.csv",
        "staff_demographics": "staff_demographics.csv",
        "hr_requisitions": "hr_requisitions.csv",
        "contractor_profiles": "contractor_profiles.csv",
    }

    for table_name, filename in enrichment.items():
        csv_path = os.path.join(data_dir, filename)
        if os.path.exists(csv_path):
            con.execute(f"CREATE TABLE {table_name} AS SELECT * FROM read_csv_auto('{csv_path}')")
            count = con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            print(f"{table_name}: {count:,} rows")
        else:
            print(f"{table_name}: not found ({csv_path})")

    # ── Pre-computed summary tables ──────────────────────────────────────
    # Materialized aggregations for common starter questions

    # Q: Which agencies have spent the most money across all fiscal years?
    con.execute("""
        CREATE TABLE summary_agency_spend AS
        SELECT agency_canonical AS agency, ROUND(SUM(extended_amount), 2) AS total_spend,
               COUNT(*) AS transaction_count, MIN(fiscal_year) AS first_year, MAX(fiscal_year) AS last_year
        FROM expenditures WHERE is_data_artifact = FALSE
        GROUP BY agency_canonical ORDER BY total_spend DESC
    """)

    # Q: How has total annual spending changed from 2008 to 2026?
    con.execute("""
        CREATE TABLE summary_annual_spend AS
        SELECT fiscal_year, ROUND(SUM(extended_amount), 2) AS total_spend,
               COUNT(*) AS transaction_count
        FROM expenditures WHERE is_data_artifact = FALSE AND fiscal_year IS NOT NULL
        GROUP BY fiscal_year ORDER BY fiscal_year
    """)

    # Q: What are the largest single payments ever made?
    con.execute("""
        CREATE TABLE summary_largest_payments AS
        SELECT payee, agency_canonical AS agency, invoice_amount, invoice_number,
               invoice_date, fiscal_year, expenditure_type, fund
        FROM expenditures
        WHERE invoice_amount IS NOT NULL AND invoice_amount > 0 AND is_data_artifact = FALSE
        ORDER BY invoice_amount DESC
    """)

    # Q: What are the highest-paid job titles?
    con.execute("""
        CREATE TABLE summary_top_salaries AS
        SELECT jobTitle AS job_title, Department AS department,
               ROUND(AVG(YTD_Total), 2) AS avg_total_comp,
               ROUND(MAX(YTD_Total), 2) AS max_total_comp,
               COUNT(*) AS employee_count
        FROM salary_data
        GROUP BY jobTitle, Department
        ORDER BY avg_total_comp DESC
    """)

    # Q: How does spending break down between operating and capital?
    con.execute("""
        CREATE TABLE summary_expenditure_type AS
        SELECT expenditure_type, fiscal_year,
               ROUND(SUM(extended_amount), 2) AS total_spend,
               COUNT(*) AS transaction_count
        FROM expenditures WHERE is_data_artifact = FALSE AND expenditure_type IS NOT NULL
        GROUP BY expenditure_type, fiscal_year
        ORDER BY fiscal_year, total_spend DESC
    """)

    # Q: Which agencies use the most licensed contractors?
    con.execute("""
        CREATE TABLE summary_agency_contractors AS
        SELECT e.agency_canonical AS agency, COUNT(DISTINCT ac.FULLNAME) AS contractor_count,
               ROUND(SUM(e.extended_amount), 2) AS total_contractor_spend
        FROM active_contractors ac
        JOIN expenditures e ON LOWER(e.payee) = LOWER(ac.FULLNAME)
        WHERE e.is_data_artifact = FALSE
        GROUP BY e.agency_canonical
        ORDER BY contractor_count DESC
    """)

    print("Summary tables created: agency_spend, annual_spend, largest_payments, top_salaries, expenditure_type, agency_contractors")

    # Lock down DuckDB — disable external file access and make read-only safe
    con.execute("SET enable_external_access = false")
    return con


def get_full_schema_description(con: duckdb.DuckDBPyConnection) -> str:
    """Build a comprehensive schema description for all loaded tables."""
    tables = [t[0] for t in con.execute("SHOW TABLES").fetchall()]
    lines = []

    for table in tables:
        dd = DATA_DICTIONARY.get(table, {})
        desc = dd.get("description", "")
        col_docs = dd.get("columns", {})
        joins = dd.get("joins", "")
        labels = ALL_LABELS.get(table, {})

        row_count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        columns = con.execute(f"DESCRIBE {table}").fetchall()

        lines.append(f"## Table: {table}")
        if desc:
            lines.append(f"{desc}")
        lines.append(f"Rows: {row_count:,}")
        if joins:
            lines.append(f"Joins: {joins}")
        lines.append("")
        lines.append("Columns:")

        for col_name, col_type, *_ in columns:
            semantic = labels.get(col_name, col_name)
            doc = col_docs.get(col_name, "")
            line = f"  - {col_name} ({col_type}) — \"{semantic}\""
            if doc:
                line += f": {doc}"

            # Add sample values for string columns
            if "VARCHAR" in col_type.upper():
                try:
                    distincts = con.execute(
                        f"SELECT DISTINCT {col_name} FROM {table} WHERE {col_name} IS NOT NULL LIMIT 5"
                    ).fetchall()
                    vals = [str(r[0]) for r in distincts]
                    if vals:
                        total_distinct = con.execute(
                            f"SELECT COUNT(DISTINCT {col_name}) FROM {table}"
                        ).fetchone()[0]
                        line += f"  e.g. {', '.join(repr(v) for v in vals[:4])} [{total_distinct} distinct]"
                except Exception:
                    pass
            elif "INT" in col_type.upper() or "DOUBLE" in col_type.upper():
                try:
                    stats = con.execute(
                        f"SELECT MIN({col_name}), MAX({col_name}) FROM {table}"
                    ).fetchone()
                    line += f"  range: {stats[0]} to {stats[1]}"
                except Exception:
                    pass

            lines.append(line)
        lines.append("")

    return "\n".join(lines)


def humanize_text(text: str, table: str = "expenditures") -> str:
    """Replace column names with semantic labels in display text.
    Falls back to converting snake_case to Title Case for unknown columns."""
    import re
    labels = ALL_LABELS.get(table, EXPENDITURE_LABELS)
    # Also include all labels from all tables for cross-table results
    all_flat = {}
    for tbl_labels in ALL_LABELS.values():
        all_flat.update(tbl_labels)
    for col, label in sorted(all_flat.items(), key=lambda x: -len(x[0])):
        text = re.sub(rf'\b{re.escape(col)}\b', label, text)
    # Convert any remaining snake_case words to Title Case
    text = re.sub(r'\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b', lambda m: m.group(1).replace('_', ' ').title(), text)
    return text


def get_data_dictionary_text() -> str:
    """Return a human-readable data dictionary."""
    lines = ["# Louisville Open Data — Data Dictionary", ""]
    for table, info in DATA_DICTIONARY.items():
        labels = ALL_LABELS.get(table, {})
        lines.append(f"## {table}")
        lines.append(info.get("description", ""))
        lines.append(f"Scope: {info.get('record_scope', '')}")
        if info.get("joins"):
            lines.append(f"Joins: {info['joins']}")
        lines.append("")
        lines.append("| Column | Semantic Name | Description |")
        lines.append("|--------|--------------|-------------|")
        for col, doc in info.get("columns", {}).items():
            semantic = labels.get(col, col)
            lines.append(f"| `{col}` | {semantic} | {doc} |")
        lines.append("")
    return "\n".join(lines)
