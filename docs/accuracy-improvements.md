# Accuracy Improvements Plan

## 1. Agency Name Normalization
**Status:** Complete
**Goal:** Map all agency name variants to canonical names so aggregations don't split the same entity.
**Approach:** Build a mapping table from distinct agency names across all tables. Clean at data load time by adding a `agency_canonical` column.

## 2. Budget Data as Validation
**Status:** Skipped (not available on portal)
**Goal:** Pull approved budget data from Louisville open data portal. Cross-reference expenditure totals against budgets to catch outliers.
**Approach:** Find and pull budget datasets. Add as enrichment table. Update system prompt to reference budgets for validation context.

## 3. Data Quality Pre-processing
**Status:** Complete
**Goal:** Clean offsetting entries and known erroneous records at load time instead of relying on LLM prompt instructions.
**Approach:** Identify and flag/exclude offsetting pairs. Add a `net_amount` column that's safe to aggregate. Remove or flag known data artifacts.

## 4. Pre-computed Summary Tables
**Status:** Complete
**Goal:** Materialize common aggregations for the starter questions so the LLM can query smaller, pre-validated tables.
**Target questions:**
- Which agencies have spent the most money across all fiscal years?
- How has total annual spending changed from 2008 to 2026?
- What are the largest single payments ever made and who received them?
- What are the highest-paid job titles in Louisville Metro government?
- How does spending break down between operating and capital expenditures?
- What capital projects exist and how much was allocated to each?
- Which agencies use the most licensed contractors?
**Approach:** Create materialized views/tables at startup in data_model.py. Update system prompt to direct the LLM to use summary tables for these common patterns.

## 5. Known-Answer Test Suite
**Status:** Complete (12 tests passing)
**Goal:** Validate agent accuracy against manually verified answers.
**Approach:** For each starter question, manually run and verify the correct SQL and expected result. Save as a test suite that can be run on demand.

## 6. Data Dictionary Enrichment
**Status:** Complete

## 7. Contractor Profiles with KY SOS Data
**Status:** Complete
**Result:** 203 top payee profiles with expenditure history, contractor license matching, and KY Secretary of State business entity data (registered agents, industry, employee size, filing dates). 140/146 business entities matched on SOS.
**Script:** `graph/build_contractor_profiles.py`
**Output:** `data/contractor_profiles.csv`

## 8. Fiscal Year Context Default
**Status:** Complete
**Rule:** Questions about a single value default to FY2025 (most recent complete year). All-time queries require explicit "all time" / "across all years" language.
**Goal:** Encode fund codes, expenditure types, and category definitions into the system prompt for better interpretation.
**Approach:** Extract definitions from Louisville's budget documents and CAFR. Add to the interpretation prompt context.

## 9. Council District Spending Analysis
**Status:** Needs investigation
**Issue:** The `region` column in expenditures (2018+ only) has very sparse district-level data — most records don't have a district assigned. The question "Which areas or council districts receive the most funding?" produces misleading results showing only ~$115K across 7 districts when actual spending is $643M+. Removed as a starter question.
**To investigate:** Pull Louisville Metro Council Districts geographic data from LOJIC and cross-reference with capital projects (which have Council_Di field) for a more complete district analysis.
