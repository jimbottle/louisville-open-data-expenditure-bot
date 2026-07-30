# City Open Data Survey

Survey of ~11 city open data portals to ground the canonical city data model
(louisville-open-data-8ve) in what cities actually publish. Tracks issue
louisville-open-data-1g5. Surveyed 2026-07-30.

**What we're looking for**, per city: platform (ArcGIS Hub vs Socrata vs
other), whether the Lou-relevant datasets exist (checkbook/expenditures is the
critical one), the expenditure dataset's actual schema, historical depth,
schema breaks across years, and metadata quality.

---

## Louisville, KY (baseline — the working system)

### Platform
ArcGIS Hub — https://data.louisvilleky.gov

### Datasets
- **Expenditures**: ✅ per-fiscal-year datasets (`eExpenditures_2008` … `eExpenditures_2026`), pulled via ArcGIS FeatureServer (`pull_arcgis.py`)
- **Salaries**: ✅ annual employee compensation
- **Capital projects**: ✅ with council district + allocations
- **Contractor licenses**: ✅ active contractors with category/expiration
- **Workforce demographics**: ✅ by department/gender/ethnicity/education
- **Enrichment beyond the portal**: KY Secretary of State business registry (officers, registered agents) — this cross-source enrichment is a Lou differentiator, not portal data

### Expenditure schema
Two eras with a hard schema break (the shape the canonical model must handle):
- **2008–2017**: `Fiscal_Year, Vendor_Name, InvoiceID, InvoiceDt, InvoiceAmt, CheckID, CheckDt, CheckAmt, CheckVoidDt, Agency_Name, Sub_Agency_Name, DepartmentName, Sub_DepartmentName, Budget_Type, Category, Sub_Category, Stimulus_Type, Funding_Source, DistributionAmt` (epoch-ms dates)
- **2018+**: `fiscal_year, invoice_date, invoice_number, invoice_amount, payee, payment_date, payment_number, agency, expenditure_type, expenditure_category, spend_category, cost_center, project, program, grant_, fund, financing_source, region, extended_amount`
- Depth: FY2008–FY2026, ~2.26M rows. Metadata quality: bare field names on the portal; the descriptions in `data_model.py`'s DATA_DICTIONARY are **our** curation — this gap is the product.

### Candidate score
n/a (baseline)

---

<!-- Surveyed cities appended below -->

## Lexington-Fayette, KY

### Platform
ArcGIS Hub — https://data.lexingtonky.gov/ (~153 datasets, essentially all GIS/geospatial layers).

### Datasets
- **Expenditures / checkbook: NOT PUBLISHED.** Full-catalog DCAT grep + Hub search for "checkbook", "expenditure", "budget", "salary", "finance" return zero financial datasets.
- **Budget:** PDF documents / engagement pages only — no machine-readable dataset.
- **Salaries / capital projects / licenses / demographics: NOT PUBLISHED** (salary data historically surfaces only via KY Open Records requests).

### Expenditure schema
No dataset exists. Onboarding Lexington would require open-records requests or a data-sharing agreement with LFUCG, not portal ingestion.

### Candidate score
**1/5** — pure GIS catalog; the critical checkbook data does not exist publicly in machine-readable form. (Key survey lesson: "city has an ArcGIS Hub" ≠ "city publishes finance data.")

## Nashville (Metro Nashville-Davidson), TN

### Platform
ArcGIS Hub — https://data.nashville.gov/ (migrated off Socrata; 168 datasets).

### Datasets
- **Vendor payments: YES** — "Metro Vendor Payments".
- **Budget: YES** — Budget to Actual Expenses + Revenues, FY2010–present.
- **Salaries: YES** — base salaries + per-FY earnings by pay type.
- **Capital projects: PARTIAL** — archived NDOT (transportation) only.
- **Contractor licenses: YES** — Registered Professional Contractors and Licenses.
- **Workforce demographics: YES** — General Government Employees Demographics.

### Expenditure schema
FeatureServer fields (verified live): `Operating_Unit, Supplier_Number, Invoice_Number, Distribution_Amount (double), Invoice_Date (date), Payment_Date (date), Payment_Number, Check_Amount (double), Department_Name, Department_Number, FID`
- **Critical gap: NO vendor/supplier NAME column** — only a numeric supplier ID, killing the "who did we pay?" question class.
- **Depth:** 1.31M rows, Payment_Date 2022-07 → 2025-12 (~3.5 FY). **Cadence:** stated monthly but ~7 months stale as of 2026-07-30.
- **API:** ArcGIS FeatureServer (same as Louisville) + bulk CSV download. **Schema breaks:** none.
- **Metadata quality:** thin — one-sentence description; column docs in an attached PDF. Observed quirk: sampled record's Supplier_Number equals Payment_Number (possible field-mapping sloppiness upstream).

### Candidate score
**3/5** — broadest dataset inventory of any surveyed city and same-platform plumbing, but the missing vendor name and stale feed undercut the core value proposition.

## Cincinnati, OH

### Platform
Socrata (Tyler Data & Insights) — https://data.cincinnati-oh.gov/, run by the Office of Performance & Data Analytics (CincyInsights dashboards alongside).

### Datasets
- **Vendor payments: YES** — "City of Cincinnati Vendor Payments" (qrj9-83t8).
- **Budget: YES** — approved budget by dept/object code, FY2004–present (hv35-hdk2).
- **Salaries: YES** — Employees w/ Salaries (wmj4-ygbf) + Salary Schedule.
- **Capital projects: NOT FOUND** (only archived 2013–2015 contracts + a 2016 street-rehab layer).
- **Business licenses: YES** (7dk3-gngs).
- **Workforce demographics: YES, embedded** — the salaries dataset carries `age_range, sex, race, eeo_job_group` per employee.

### Expenditure schema
Actual columns (via `/resource/qrj9-83t8.json?$limit=1` + `/api/views`): `fiscal_year, acct_period, dept_code, dept_desc, fund_code, fund_desc, exp_acct_cat, exp_acct_cat_desc, trans_id, trans_line_no, record_date (calendar_date), check_no, amount, vendor_name`
- **Depth:** 1.23M rows, FY2014–FY2027 (a record dated yesterday was returned). **Cadence:** weekly, verified fresh.
- **API:** SODA with full SoQL. **Schema breaks: none** — one continuous uniform dataset.
- **Metadata quality:** excellent dataset-level description; per-column API descriptions blank but a data-dictionary PDF is attached; self-describing code/desc column pairs.

### Candidate score
**5/5** — fresh, weekly, 1.2M-row, 13-FY checkbook with vendor names and a clean uniform schema; structurally almost a superset of Louisville's model (dept/fund/category/vendor/amount/date), backed by budget + salaries-with-demographics + licenses. Capital projects is the only gap. Requires the Socrata pull path.

## Indianapolis (Indy/Marion County), IN

### Platform
ArcGIS Hub — https://data.indy.gov/ (651 items). Financial content exists almost entirely as **PDF "document" items**, not queryable datasets.

### Datasets
- **Expenditures / checkbook: NOT PUBLISHED.** No transaction-level spending dataset anywhere; closest items are links to the Controller's Office web page and third-party FOIA aggregators.
- **Budget:** PDF-only (adopted/proposed budgets 2000–2025, verified `application/pdf`). Machine-readable local budget data lives on the **state's** Indiana Gateway, not the city portal.
- **Salaries:** state Indiana Gateway "Form 100R" report builder only. **Capital projects / contracts:** document links to web apps. **Demographics:** not found.

### Expenditure schema
N/A — no expenditure dataset exists; all fiscal items verified as PDFs or external links.

### Candidate score
**1/5** — GIS-focused portal; nothing tabular to load, and salaries live on a state site with a different model.

## Kansas City, MO

### Platform
Socrata (Tyler Data & Insights) — https://data.kcmo.org/ ("Open Data KC"), plus an OpenGov budget-visualization site (presentation only).

### Datasets
- **Checkbook: YES — flagship find.** Eleven per-calendar-year "Vendor Payments" datasets, 2016–2026, explicitly "Checkbook level data" (2016 4mdg-usvj … 2026 w3zd-mhbv).
- **Budget: YES, machine-readable** — line-item budget datasets FY2012–FY2024 (newest is FY2023-24).
- **Salaries: NOT PUBLISHED** (a real gap; third-party FOIA data only).
- **Capital projects: YES** — Capital Improvements 1996–2020 + sales-tax expenditures FY2008–2018.
- **Business licenses: YES** (pnm4-68wg, geocoded). **Workforce demographics: YES** (y5ky-3d6r, 20 columns, all documented).

### Expenditure schema
Actual columns (verified identical names across 2016/2020/2025/2026): `vendor_name, payment_no, payment_date (calendar_date), payment_amount, payment_method, voucher, sum_amount, fund, fund_descr, deptid, deptid_descr, account, account_descr`
- **Depth:** 2016–2026, ~100–145k rows/year (~1.2M total). **Cadence:** weekly; the 2026 dataset was updated today (2026-07-30).
- **API:** full SODA/SoQL + CSV export. **Schema breaks:** column names stable across all 11 years; one minor type break (`fund`/`deptid`/`account` number→text in 2026 — cast on ingest). Data must be UNIONed across per-year datasets (same pattern as Louisville's per-FY files).
- **Metadata quality:** 0 of 13 columns documented, but names self-explanatory; good prose description including a documented `sum_amount`/`voucher` double-counting caveat — directly analogous to Louisville's offsetting-row handling.

### Candidate score
**5/5** — stable 13-column checkbook across 11 years, weekly updates, clean Socrata API, plus machine-readable budget/capital/licenses/demographics. Gaps: no salary data, undocumented (but obvious) columns.

## Tucson, AZ

### Platform
ArcGIS Hub — https://gisdata.tucsonaz.gov/ (192 datasets, heavily GIS). Procurement on OpenGov (solicitations, not payments).

### Datasets
- **Checkbook: NOT PUBLISHED.** Nothing on the city portal; could not verify participation in the state transparency portal.
- **Budget:** PDF-only, off-portal. **Salaries:** not published. **Capital projects:** bond-program project *maps* only. **Business licenses: YES** (FeatureServer, current). **Workforce demographics:** no (census data about residents only).

### Expenditure schema
N/A — no expenditure dataset exists on any official City of Tucson property.

### Candidate score
**1.5/5** — functional ArcGIS Hub, zero financial tables; nothing for an expenditure-centric bot to ingest without a records request.

## Durham, NC

### Platform
ArcGIS Hub — https://live-durhamnc.opendata.arcgis.com/ (joint City/County site, ~260 items, overwhelmingly GIS).

### Datasets
- **Checkbook: NOT PUBLISHED.** Closest item is "City Expenses (2005-2017)" — a static, frozen Excel file, and a pivoted hierarchy (LEVEL1–5 + one column per year), structurally incompatible with a row-per-transaction model.
- **Budget:** PDFs on the city site only. **Salaries / capital projects / demographics: NOT PUBLISHED.** **Licenses:** n/a — NC repealed municipal privilege licenses in 2015, so no NC city has classic business-license data.

### Expenditure schema
N/A — only the frozen FY2005–FY2017 pivoted Excel, no API, no updates since abandonment.

### Candidate score
**1/5** — onboarding Durham would require a records request, not an ETL job.

## Raleigh, NC

### Platform
ArcGIS Hub — https://data.raleighnc.gov/ (~199 items; migrated off Socrata years ago). Caution: portal surfaces **Wake County** WATCH budget services that look like city expenditure data but aren't.

### Datasets
- **Checkbook: NOT PUBLISHED** (only an "Uncashed Checks" service).
- **Budget:** "Adopted Expense Budget" FeatureServer — FY2012–FY2019 only, metadata literally says "Update Frequency: Never". Schema (verified live): `Account_Level_6, Fund_Description, DeptID_Description, Account_Description, Budget_Year, Account_Type, Program, Program_Description, Budget_Amount`.
- **Capital projects:** FY2017–FY2021 CIP, last modified 2018 — stale. **Salaries / licenses / demographics: NOT PUBLISHED.**

### Expenditure schema
No checkbook; the budget dataset above is line items, not actuals, and is abandoned.

### Candidate score
**2/5** — ideal plumbing (clean single-table FeatureServer, consistent schema) but everything financial frozen at FY2019; Raleigh would need to restart publication before it's onboardable.

## Chattanooga, TN

### Platform
ArcGIS Hub — https://data.chattanooga.gov/ (90 items). **Recently migrated from Socrata** — chattadata.org is now a landing page, old Socrata endpoints redirect to a legacy notice, and the **Open Checkbook app (checkbook.chattanooga.gov) returns HTTP 500**.

### Datasets
- **Checkbook: LOST IN MIGRATION** — the former Socrata "Spending by the City of Chattanooga" dataset (every check since ~2010) no longer exists anywhere on the new portal, and the checkbook app is down. Headline finding: a city can *lose* its transparency data in a platform migration.
- **Budget: YES** — "Open Budget - Expenses" CSV, FY2015–FY2027, 57k rows, updated 2026-07-01 (schema: `Fiscal Year, Service, Department, Program, Expense Category, Recommended Amount, Approved Amount, Fund, Fund Type, Description, Expense Type`). Plus Revenue.
- **Capital projects: YES** — FY2016–FY2027 with actuals, updated July 2026, plus per-project details.
- **Procurement-adjacent:** Purchase Agreements (vendor names, diversity flags, no dollars), Diverse Supplier Info, contractor permits. **Salaries / demographics: NOT PUBLISHED.**
- Also useful for Lou-style Q&A: 311 requests, police incidents, code enforcement, permits — all CSV.

### Expenditure schema
True checkbook currently absent; the live stand-in is the consolidated 13-year budget CSV above (no schema breaks, static CSV download rather than a query API).

### Candidate score
**3/5** — actively maintained, consistent 13-year budget + capital + procurement CSVs that would drop straight into the DuckDB loader; but until the checkbook resurfaces you'd be onboarding *budget* analytics, not Louisville-style *expenditure* analytics. Jumps to 4–5 if the checkbook returns — worth contacting their Open Data office (migration fallout, not policy, by all appearances).

## Chicago, IL

### Platform
Socrata (now Tyler Data & Insights) — confirmed live: SODA `/resource/<id>.json` endpoints, `api/views` metadata, and the Socrata Discovery catalog API all respond. Portal: https://data.cityofchicago.org/

### Datasets
- **Expenditures / checkbook — YES (strong).** "Payments" — https://data.cityofchicago.org/Administration-Finance/Payments/s4vu-giwb (transaction-level, 450,319 rows). Companion "Vendor Payments" (pkr3-4xv7, same schema, vendor-first view) and "Vendor Payments - New Arrivals" (gxzc-43gg).
- **Budget — YES.** Per-year datasets, current through FY2026 ("Budget - 2026 Budget Ordinance - Appropriations" 6694-f78c; Recommendations - Appropriations/Revenue/Positions & Salaries), series back to 2011.
- **Employee salaries — YES.** "Current Employee Names, Salaries, and Position Titles" (xzkq-xp2w, updated daily) plus per-year Budget Ordinance positions/salaries datasets.
- **Capital projects — NO dataset found.** CIP is published outside the data portal.
- **Contractor/business licenses — YES.** "Business Licenses" (r5kz-chrr, updated daily) plus filtered views.
- **Workforce demographics — NO dataset found.**

### Expenditure schema
Actual columns of `s4vu-giwb` (via `/resource/s4vu-giwb.json?$limit=1` + `api/views`):
- `voucher_number` (text), `amount` (number), `check_date` (text — stored as `MM/DD/YYYY` text, needs casting), `department_name` (text), `contract_number` (text, the only documented column), `vendor_name` (text)
- **Depth:** 1996–present, but with a built-in granularity break: 1996–2002 rolled up and dated "2002", and anything older than 2 years is summarized per vendor+contract — only recent data is truly transaction-level.
- **Cadence:** daily. **API:** standard SODA, no auth for reads, SoQL verified working; bulk CSV via `/api/views/<id>/rows.csv`.
- **Metadata quality:** good dataset-level description, but column descriptions almost entirely absent (5 of 6 fields undocumented); only 6 columns — no fund/account/category dimensions, limiting question variety vs Louisville.

### Candidate score
**4/5.** Best-in-class freshness, 30 years of history, rock-solid unauthenticated SODA API, salaries+budget+licenses present — docked for the sparse 6-column undocumented schema, text-typed dates, and the pre-2-year rollup that silently changes granularity. **Socrata support cost:** pagination `$limit`/`$offset` instead of ArcGIS `resultOffset`, types from `api/views` metadata, bulk CSV in one call — arguably simpler than ArcGIS; keep the download-CSV-into-DuckDB architecture and SoQL can be ignored. Main new work: a Socrata metadata adapter + text-date handling.

## Baltimore, MD

### Platform
**NOT Socrata** — Baltimore migrated to **Esri ArcGIS Hub** (relaunched Dec 2020). Portal: https://data.baltimorecity.gov/. All data APIs are ArcGIS FeatureServer REST — same platform family as Louisville. (Fails as a Socrata contrast case; low-friction onboarding candidate instead.)

### Datasets
- **Expenditures / checkbook — YES, split by fiscal year with a schema break:** "Open Checkbook FY2022 Through Present" (370,020 rows, verified current through 2026-07-28); "Open Checkbook FY2021 Dataset" (Q1–Q3 only — apparent Q4 gap); "Open Checkbook FY2020 Dataset"; plus a standalone Open Checkbook Data Dictionary document and a Power BI app.
- **Budget — YES but stale.** FY2023 and FY2022 datasets; nothing newer.
- **Employee salaries — YES (good depth).** "Baltimore City Employee Salaries" — FY2011→last FY in a single dataset (`Name, JobTitle, AgencyID, AgencyName, HireDate, AnnualSalary, GrossPay, FiscalYear`).
- **Capital projects — NO.**
- **Contractor/business licenses — partial.** Liquor licenses only; also a city contracts dataset (contracts, not licenses).
- **Workforce demographics — NO.**

### Expenditure schema
FY2022+ FeatureServer layer (fetched from services1.arcgis.com):
- `Category, Check_Number, Payment_Date (date-typed), Supplier_Name, Supplier_Invoice_Number, Payment_Amount (double), Payment_Amount_split (double), Supplier_Contract_Name, Supplier_Contract_Number, Agency_Code, Agency, Fund, Fund_Description, Cost_Center, Cost_Center_Description, Spend_Category, Spend_Category_Description, FileName, InsertDate, Unique_ID, ID`
- **Depth:** FY2020→present across three datasets. **Schema break confirmed:** FY2020 dataset uses a completely different schema (`Date, Agency, Service, Spending_Category, Spending_Description, Fund, Amount, Vendor_Name`) vs the Workday-style FY2022+ schema — multi-year union requires field mapping (exactly Louisville's two-era problem).
- **Cadence:** actively maintained (~weekly/continuous batch loads). **API:** ArcGIS FeatureServer REST, no auth, same access method as Louisville.
- **Metadata quality: notably good** — glossary embedded in the description, self-describing `*_Description` companion columns, standalone data dictionary. Richer dimensions (fund, cost center, spend category) than Chicago.

### Candidate score
**3/5.** Checkbook is current, richly dimensioned, well documented — but fragmented per-FY with a hard schema break at FY2022, budget stale at FY2023, and capital projects/licenses/demographics absent. Platform-wise: essentially **zero new plumbing** (same ArcGIS FeatureServer path as Louisville); the effort is data modeling, not integration.

---

# Comparison matrix

| City | Platform | Live checkbook | Vendor names | Depth | Cadence | Budget | Salaries | Capital | Licenses | Demogr. | Score |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **Louisville, KY** | ArcGIS Hub | ✅ | ✅ | FY08–26 | ~annual files | ➖ | ✅ | ✅ | ✅ | ✅ | baseline |
| **Cincinnati, OH** | Socrata | ✅ | ✅ | FY14–27 | weekly | ✅ | ✅ | ❌ | ✅ | ✅ (embedded) | **5/5** |
| **Kansas City, MO** | Socrata | ✅ | ✅ | CY16–26 | weekly | ✅ | ❌ | ✅ | ✅ | ✅ | **5/5** |
| **Chicago, IL** | Socrata | ✅ | ✅ | 1996–now* | daily | ✅ | ✅ | ❌ | ✅ | ❌ | 4/5 |
| **Baltimore, MD** | ArcGIS Hub | ✅ | ✅ | FY20–now | ~weekly | stale | ✅ | ❌ | partial | ❌ | 3/5 |
| **Nashville, TN** | ArcGIS Hub | ✅ | ❌ (ID only) | FY22–25, stale | monthly (stale) | ✅ | ✅ | partial | ✅ | ✅ | 3/5 |
| **Chattanooga, TN** | ArcGIS Hub (ex-Socrata) | ❌ lost in migration | — | — | — | ✅ | ❌ | ✅ | partial | ❌ | 3/5 |
| **Raleigh, NC** | ArcGIS Hub | ❌ | — | — | — | frozen FY19 | ❌ | stale | n/a (NC) | ❌ | 2/5 |
| **Tucson, AZ** | ArcGIS Hub | ❌ | — | — | — | PDF only | ❌ | maps only | ✅ | ❌ | 1.5/5 |
| **Indianapolis, IN** | ArcGIS Hub | ❌ | — | — | — | PDF only | state site | PDF | web app | ❌ | 1/5 |
| **Durham, NC** | ArcGIS Hub | ❌ | — | — | — | PDF only | ❌ | ❌ | n/a (NC) | ❌ | 1/5 |
| **Lexington, KY** | ArcGIS Hub | ❌ | — | — | — | PDF only | ❌ | ❌ | ❌ | ❌ | 1/5 |

\* Chicago: pre-2-year data is rolled up per vendor+contract; only recent data is transaction-level.

# Second-city shortlist

1. **Cincinnati, OH (5/5)** — fresh weekly 1.2M-row checkbook, FY2014–27, uniform schema, vendor names, dept/fund/category dimensions nearly a superset of Louisville's model, salaries with embedded demographics. Cost: requires the Socrata pull path.
2. **Kansas City, MO (5/5)** — 11 years of stable-schema checkbook (per-year datasets, like Louisville's), weekly updates, budget + capital + demographics. Gap: no salaries.
3. **Baltimore, MD (3/5)** — the zero-new-plumbing option: same ArcGIS FeatureServer path as Louisville, current well-documented checkbook; effort is purely data modeling (FY2020 vs FY2022+ schema break — exactly Louisville's two-era problem).
4. **Chicago, IL (4/5)** — great API and freshness but only 6 sparse columns; limits question variety.

# What the survey means for the canonical model (feeds louisville-open-data-8ve)

1. **The core schema converges.** Every live checkbook reduces to: *payee/vendor name, amount, date(s), department/agency, fund, category/account,* plus identifiers (invoice/check/voucher) and *fiscal year*. Optional tier seen in the wild: cost center, program, project, contract number, payment method. Louisville's unified schema is close to the natural canonical form already.
2. **Per-year dataset fragmentation + schema breaks are the norm**, not a Louisville quirk (Louisville 2 eras; Baltimore 3 datasets/2 schemas; KC 11 datasets/1 schema + a type break; Chicago a granularity break). The canonical model MUST treat "multiple source datasets → era mappings → one unified table" as a first-class concept.
3. **Socrata support is required, not optional.** Both 5/5 candidates are Socrata. The pull path is simple (bulk CSV in one call; types from `api/views`) and fits the existing download-CSV-into-DuckDB architecture. ArcGIS Hub ≠ finance data: 7 of the 9 ArcGIS Hub cities surveyed (incl. Louisville) publish no usable checkbook, counting Nashville's vendor-name-less feed as unusable.
4. **Metadata quality is universally poor** — even 5/5 cities have blank column descriptions (Cincinnati/KC: 0 columns documented in the API; Chicago: 5 of 6 undocumented). The curated data dictionary + canonicalization layer IS the product; the survey confirms cities won't have done it themselves.
5. **Offsetting/artifact handling generalizes.** KC documents a voucher/sum_amount double-counting caveat directly analogous to Louisville's offsetting rows; Chicago has silent granularity rollups. Data-quality flags belong in the engine with per-city rules.
6. **Market shape:** of 12 cities (incl. Louisville), only 6 have a live checkbook and one (Chattanooga) *lost theirs in a platform migration*. Two product implications: (a) the addressable market for "query your checkbook" is the transparency-leader cohort, and (b) there may be a second offering in helping the other half publish — or restoring what migrations dropped (Chattanooga is a warm lead: their checkbook app 500s today and their budget CSVs would drop straight into the loader).
