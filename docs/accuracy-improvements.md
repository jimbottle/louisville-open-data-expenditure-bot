# Accuracy Improvements Plan

## 1. Agency Name Normalization
**Status:** Not started
**Goal:** Map all agency name variants to canonical names so aggregations don't split the same entity.
**Approach:** Build a mapping table from distinct agency names across all tables. Clean at data load time by adding a `agency_canonical` column.

## 2. Budget Data as Validation
**Status:** Not started
**Goal:** Pull approved budget data from Louisville open data portal. Cross-reference expenditure totals against budgets to catch outliers.
**Approach:** Find and pull budget datasets. Add as enrichment table. Update system prompt to reference budgets for validation context.

## 3. Data Quality Pre-processing
**Status:** Not started
**Goal:** Clean offsetting entries and known erroneous records at load time instead of relying on LLM prompt instructions.
**Approach:** Identify and flag/exclude offsetting pairs. Add a `net_amount` column that's safe to aggregate. Remove or flag known data artifacts.

## 4. Pre-computed Summary Tables
**Status:** Not started
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
**Status:** Not started
**Goal:** Validate agent accuracy against manually verified answers.
**Approach:** For each starter question, manually run and verify the correct SQL and expected result. Save as a test suite that can be run on demand.

## 6. Data Dictionary Enrichment
**Status:** Not started
**Goal:** Encode fund codes, expenditure types, and category definitions into the system prompt for better interpretation.
**Approach:** Extract definitions from Louisville's budget documents and CAFR. Add to the interpretation prompt context.
