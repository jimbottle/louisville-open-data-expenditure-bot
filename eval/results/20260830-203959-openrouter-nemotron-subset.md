# Eval: openrouter-nemotron-subset

- when: 2026-08-30T20:39:59
- model: `nvidia/nemotron-3-super-120b-a12b:free` (openrouter)
- **5/6 pass** (sql 5, answer 5)
- repairs: 1 succeeded of 2 attempted
- latency: mean 28.6s per question

| case | tags | sql | answer | s | note |
|---|---|---|---|---:|---|
| fire-vehicles-fy2024 | vocabulary | ✅ | ✅ | 26.2 | 1,581,197.96 ≈ 1,581,197.96 |
| arp-total | vocabulary, year | ❌ | ❌ | 29.4 | SQL: served query returned nothing · answer: does not state 37,636,175.82 |
| cares-total | vocabulary, year | ✅ | ✅ | 61.8 | 139,614,685.03 ≈ 139,614,685.03 · repaired |
| mayor-and-chief-pay | entity | ✅ | ✅ | 38.4 | all present |
| honest-empty-fy2031 | guard | ✅ | ✅ | 12.7 | empty as expected |
| off-topic | guard | ✅ | ✅ | 2.9 | no SQL executed |

## Answers

### fire-vehicles-fy2024

> How much did the Louisville Fire department spend on vehicles in fiscal year 2024?

```sql
SELECT ROUND(SUM(extended_amount), 2) AS total_spent_on_vehicles
FROM expenditures
WHERE agency_canonical = 'Louisville Fire'
  AND fiscal_year = 2024
  AND spend_category IS NOT NULL
  AND (spend_category ILIKE '%fleet%' OR spend_category ILIKE '%automotive%' OR spend_category ILIKE '%auto%' OR spend_category ILIKE '%vehicle%' OR spend_category ILIKE '%truck%');
```

The Louisville Fire department spent $1,581,197.96 on vehicles in fiscal year 2024.

### arp-total

> How much has Louisville spent from the American Rescue Plan?

```sql
SELECT ROUND(SUM(extended_amount), 2) AS total_spent_arp_fy2025
FROM expenditures
WHERE fund = 'ARP'
  AND fiscal_year = 2025;
```

The query filtered for fund = 'ARP' and fiscal_year = 2025, but the expenditure data only contains ARP‑funded rows for fiscal year 2022. Since no rows match both conditions, the sum returns nothing.

To get a result, you could ask:
- How much has Louisville spent from the American Rescue Plan overall?
- How much has Louisville spent from the American Rescue Plan in fiscal year 2022?

### cares-total

> How much CARES Act coronavirus relief money did the city spend?

```sql
SELECT ROUND(SUM(extended_amount), 2) AS total_cares_spend
FROM expenditures
WHERE fund ILIKE '%cares%'
  AND is_data_artifact = FALSE;
```

The city spent $139,614,685.03 in CARES Act coronavirus relief money. This total reflects all expenditure records tagged as CARES Act funding in the city’s financial system, with any test or duplicate rows removed. FY2026 expenditure data is incomplete, having been loaded only through March 16, 2026. [R-011-21] authorized the mayor to accept $5,430,961 of CARES Act funding through the U.S. Department of Housing and Urban Development for the Emergency Solutions Grant Program.

### mayor-and-chief-pay

> How much does the mayor make? What about the police chief?

```sql
SELECT Employee_Name, jobTitle, YTD_Total AS total_compensation
FROM salary_data
WHERE CalYear = 2025
  AND jobTitle IN ('Mayor', 'Police Chief')
  AND YTD_Total IS NOT NULL
ORDER BY YTD_Total DESC;
```

Mayor Craig Greenberg earned $158,115.58 and Police Chief Paul Louis Humphrey earned $267,811.04 in total compensation for calendar year 2025.

### honest-empty-fy2031

> How much did Louisville Fire spend in fiscal year 2031?

```sql
SELECT ROUND(SUM(extended_amount), 2) AS total_spend
FROM expenditures
WHERE agency_canonical = 'Louisville Fire'
  AND fiscal_year = 2031;
```

The expenditure data only covers fiscal years 2008 through 2026, with FY2026 being partial. Fiscal year 2031 is outside this range, so there are no records for Louisville Fire in that year, resulting in no rows returned.

Suggested rephrased questions:
- How much did Louisville Fire spend in fiscal year 2025?
- What is Louisville Fire’s total spending across all available fiscal years?

### off-topic

> Tell me a joke about cats.

```sql
(none)
```

This question doesn't appear to be answerable from the Louisville Metro expenditure data. Try asking about government spending, agency budgets, contractor payments, employee salaries, or capital projects.
