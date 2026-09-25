# Eval: judge-pass-prompts

- when: 2026-09-25T11:38:31
- model: `gpt-oss-120b` (paid)
- **23/23 pass** (sql 23, answer 23)
- repairs: 0 succeeded of 0 attempted
- latency: mean 2.1s per question

| case | tags | sql | answer | s | note |
|---|---|---|---|---:|---|
| fire-vehicles-fy2024 | vocabulary | ✅ | ✅ | 2.0 | 1,581,197.96 ≈ 1,581,197.96 |
| fuel-fy2025 | vocabulary | ✅ | ✅ | 2.4 | 8,206,675.49 ≈ 8,206,675.49 |
| arp-total | vocabulary, year | ✅ | ✅ | 1.8 | 37,636,175.82 ≈ 37,636,175.82 |
| cares-total | vocabulary, year | ✅ | ✅ | 2.0 | 139,614,685.03 ≈ 139,614,685.03 |
| seagrave-total | entity, year | ✅ | ✅ | 1.9 | 12,030,691.29 ≈ 12,030,691.29 |
| lge-total | entity, year | ✅ | ✅ | 1.6 | 345,725,377.26 ≈ 345,725,377.26 |
| paving-vendors | vocabulary | ✅ | ✅ | 2.3 | all present |
| top-agencies-all-time | summary | ✅ | ✅ | 2.0 | top 5 match |
| annual-spend-trend | summary, year | ✅ | ✅ | 2.4 | 19 rows |
| lmpd-by-year | year | ✅ | ✅ | 3.3 | 43,401,681.16 ≈ 43,401,681.16 |
| parks-fy2025 | year | ✅ | ✅ | 1.4 | 43,399,384.03 ≈ 43,399,384.03 |
| library-latest-year | year | ✅ | ✅ | 1.9 | 20,326,652.59 ≈ 20,326,652.59 |
| tech-fy2025 | vocabulary, year | ✅ | ✅ | 2.3 | 22,414,255.52 ≈ 22,414,255.52 |
| mayor-and-chief-pay | entity | ✅ | ✅ | 1.8 | all present |
| highest-paid-positions | summary | ✅ | ✅ | 1.5 | top 3 match |
| police-overtime-2024 | vocabulary, entity | ✅ | ✅ | 1.9 | 23,552,285.59 ≈ 23,552,285.59 |
| registered-agents | summary | ✅ | ✅ | 2.4 | top 5 match |
| grant-funding | summary | ✅ | ✅ | 2.4 | 1,174,232,849.99 ≈ 1,174,232,849.99 |
| largest-payments | summary | ✅ | ✅ | 4.3 | all present |
| capital-projects | summary | ✅ | ✅ | 1.8 | 14 rows |
| contract-splitting-fy2025 | summary | ✅ | ✅ | 2.7 | 25 rows |
| honest-empty-fy2031 | guard | ✅ | ✅ | 1.0 | empty as expected |
| off-topic | guard | ✅ | ✅ | 0.5 | no SQL executed |

## Answers

### fire-vehicles-fy2024

> How much did the Louisville Fire department spend on vehicles in fiscal year 2024?

```sql
SELECT ROUND(SUM(extended_amount), 2) AS total_vehicle_spend_2024
FROM expenditures
WHERE fiscal_year = 2024
  AND agency_canonical = 'Louisville Fire'
  AND (spend_category ILIKE '%fleet%' 
       OR spend_category ILIKE '%automotive%' 
       OR spend_category ILIKE '%auto%' 
       OR spend_category ILIKE '%vehicle%' 
       OR spend_category ILIKE '%truck%')
  AND is_data_artifact = FALSE
  AND extended_amount IS NOT NULL;
```

The Louisville Fire department spent $1.58 million on vehicles in fiscal year 2024. This amount reflects vendor payments for fleet‑related purchases and does not represent the department’s total cost or budget.

### fuel-fy2025

> How much did Louisville Metro spend on fuel in FY2025?

```sql
SELECT ROUND(SUM(extended_amount), 2) AS total_fuel_spend
FROM expenditures
WHERE fiscal_year = 2025
  AND spend_category ILIKE '%fuel%'
  AND is_data_artifact = FALSE
  AND extended_amount IS NOT NULL;
```

Louisville Metro spent $8.2M on fuel in FY 2025. FY 2025 is the most recent fiscal year with complete expenditure data.

### arp-total

> How much has Louisville spent from the American Rescue Plan?

```sql
SELECT ROUND(SUM(extended_amount), 2) AS total_spend
FROM expenditures
WHERE fund = 'ARP'
  AND is_data_artifact = FALSE
  AND extended_amount IS NOT NULL;
```

Louisville has spent $37.6M from the American Rescue Plan. These are vendor payments recorded in the city’s expenditure data.  

Related city legislation:  
- Resolution R-083-21 (passed 2021) established priority areas for use of the American Rescue Plan funds.  
- Ordinance O-374-22 (held in committee 2022) addressed under‑budget ARP projects through reappropriations of ARP funding.  
- Resolution R-062-22 (passed 2022) authorized the mayor to accept a grant from the American Rescue Plan to be administered by the Louisville Free Public Library for library services and technology.

### cares-total

> How much CARES Act coronavirus relief money did the city spend?

```sql
SELECT ROUND(SUM(total_amount), 2) AS total_cares_spend
FROM summary_grant_funding
WHERE fund ILIKE '%cares%';
```

The city has spent $139.6 M of CARES Act coronavirus relief money. This amount reflects payments recorded in the grant‑funding data, which are payments to vendors. Resolution R-011-21 authorized the mayor to accept CARES Act funding for the Emergency Solutions Grant program.

### seagrave-total

> How much has the city paid Seagrave Fire Apparatus in total?

```sql
SELECT ROUND(SUM(extended_amount), 2) AS total_spend
FROM expenditures
WHERE payee_canonical = 'SEAGRAVE FIRE APPARATUS LLC'
  AND is_data_artifact = FALSE
  AND extended_amount IS NOT NULL;
```

The city has paid Seagrave Fire Apparatus a total of $12.0 million.

### lge-total

> How much has Louisville Metro paid LG&E over the years?

```sql
SELECT ROUND(SUM(extended_amount), 2) AS total_spend
FROM expenditures
WHERE payee_canonical = 'Louisville Gas & Electric Company'
  AND payee_canonical IS NOT NULL;
```

Louisville Metro has paid Louisville Gas & Electric Company a total of $345.7 M across all years in the dataset. Note: FY 2026 expenditure data is incomplete, so the total includes a partial FY 2026 figure.

### paving-vendors

> Which vendors were paid the most for road paving?

```sql
SELECT payee_canonical,
       ROUND(SUM(extended_amount), 2) AS total_spend
FROM expenditures
WHERE is_data_artifact = FALSE
  AND (cost_center ILIKE '%paving%' OR cost_center ILIKE '%road%' OR cost_center ILIKE '%street%'
       OR spend_category ILIKE '%road%' OR spend_category ILIKE '%paving%' OR spend_category ILIKE '%street%'
       OR project ILIKE '%paving%' OR project ILIKE '%road%' OR project ILIKE '%street%')
  AND payee_canonical IS NOT NULL
GROUP BY payee_canonical
ORDER BY total_spend DESC
LIMIT 10
```

The vendors that received the largest payments for road‑paving work are:

1. Hall Contracting of Kentucky Inc – $157.1 M  
2. Louisville Gas & Electric Company – $80.9 M  
3. Flynn Brothers Contracting Inc – $52.9 M  
4. Louisville Paving Company Inc – $52.0 M  
5. Cargill Inc – $16.5 M  
6. MAC Construction & Excavating Inc – $11.0 M  
7. Pace Contracting LLC – $10.5 M  
8. Compass Minerals America Inc – $6.2 M  
9. Strand Associates Inc – $6.2 M  
10. HDR Engineering Inc – $5.7 M  

These amounts reflect payments recorded in the city’s expenditure data up through March 16 2026; the FY 2026 figures are partial, so totals could increase as more data are loaded.

### top-agencies-all-time

> Which agencies have spent the most money across all fiscal years?

```sql
SELECT agency, ROUND(total_spend, 2) AS total_spend, transaction_count
FROM summary_agency_spend
WHERE agency IS NOT NULL
ORDER BY total_spend DESC
```

The ten agencies that have spent the most money are:

1. Public Works & Assets – $1.036 B  
2. Economic Development – $390.4 M  
3. Facilities & Fleet Management – $368.8 M  
4. Community Services & Revitalization – $351.4 M  
5. Office of Housing & Community Development – $348.6 M  
6. OMB Finance – $319.9 M  
7. Develop Louisville – $288.1 M  
8. Parks & Recreation – $276.3 M  
9. Louisville Metro Police Department – $263.6 M  
10. Public Health & Wellness – $262.4 M  

These ten represent the highest spenders out of the 61 agencies listed in the data.

### annual-spend-trend

> How has total annual spending changed from 2008 to 2026?

```sql
-- FY2026 data is partial; includes all fiscal years 2008-2026
SELECT fiscal_year,
       ROUND(total_spend, 2) AS total_spend
FROM summary_annual_spend
WHERE fiscal_year IS NOT NULL
ORDER BY fiscal_year ASC;
```

Total annual spending grew from $244.8 M in FY2008 to $643.7 M in FY2025; the FY2026 figure of $439.7 M is based on partial data.

Key points:
1. FY2008‑FY2015 ranged between about $210 M and $280 M, with the lowest amount in FY2013 ($210.3 M).
2. FY2016 rose to $252.0 M, and spending stayed between $233 M and $258 M through FY2020.
3. FY2021 increased sharply to $374.6 M, followed by $411.6 M in FY2022.
4. FY2023 fell back to $277.7 M, then FY2024 rebounded to $541.5 M.
5. FY2025 reached the highest level at $643.7 M.

### lmpd-by-year

> Give me a year-over-year breakdown of LMPD spending

```sql
SELECT fiscal_year,
       ROUND(SUM(extended_amount), 2) AS total_spend
FROM expenditures
WHERE agency_canonical = 'Louisville Metro Police Department'
  AND is_data_artifact = FALSE
GROUP BY fiscal_year
ORDER BY fiscal_year ASC
```

LMPD spending by fiscal year is:

2008: $9.8M; 2009: $9.1M; 2010: $12.2M; 2011: $8.2M; 2012: $6.6M; 2013: $8.8M; 2014: $8.2M; 2015: $9.2M; 2016: $10.4M; 2017: $10.9M; 2018: $11.2M; 2019: $12.1M; 2020: $12.4M; 2021: $11.6M; 2022: $19.4M; 2023: $8.5M; 2024: $27.4M; 2025: $43.4M; 2026 (partial): $24.4M.

FY2025 is the most recent fiscal year with complete expenditure data; FY2026 figures are partial, covering only through March 16, 2026.

Resolution R‑037‑21 approved a non‑competitively negotiated professional‑service contract for LMPD consulting services to establish a DNA laboratory.

### parks-fy2025

> How much did Parks & Recreation spend in FY2025?

```sql
SELECT ROUND(SUM(extended_amount), 2) AS total_spend
FROM expenditures
WHERE agency_canonical = 'Parks & Recreation'
  AND fiscal_year = 2025
  AND is_data_artifact = FALSE;
```

Parks & Recreation spent $43.4 M in FY 2025. FY 2025 is the most recent fiscal year with complete expenditure data.

### library-latest-year

> How much did the library spend last year?

```sql
SELECT ROUND(SUM(extended_amount), 2) AS total_spend
FROM expenditures
WHERE agency_canonical = 'Louisville Free Public Library'
  AND fiscal_year = 2025
  AND is_data_artifact = FALSE;
```

The Louisville Free Public Library spent $20.3 million in FY 2025, the most recent complete fiscal year.

### tech-fy2025

> How much did Louisville spend on technology in FY2025?

```sql
SELECT 'Metro Technology Services department' AS spend_view, ROUND(SUM(extended_amount), 2) AS total_spend
FROM expenditures
WHERE fiscal_year = 2025
  AND agency_canonical = 'Metro Technology Services'
  AND is_data_artifact = FALSE
UNION ALL
SELECT 'Computer, software and cloud purchases (all departments)' AS spend_view, ROUND(SUM(extended_amount), 2) AS total_spend
FROM expenditures
WHERE fiscal_year = 2025
  AND (spend_category LIKE 'Computer%' OR spend_category ILIKE '%Software%' OR spend_category = 'Cloud Computing Services')
  AND is_data_artifact = FALSE;
```

Louisville spent $34.7 million on computer, software and cloud purchases across all departments and $22.4 million on the Metro Technology Services department in FY 2025. These amounts represent separate categories of spending and are not added together.

### mayor-and-chief-pay

> How much does the mayor make? What about the police chief?

```sql
SELECT Employee_Name, jobTitle, ROUND(YTD_Total, 2) AS YTD_Total
FROM salary_data
WHERE CalYear = 2025
  AND jobTitle IN ('Mayor', 'Police Chief')
  AND Employee_Name IS NOT NULL
ORDER BY jobTitle, Employee_Name
```

The mayor earned $158,115.58 in calendar year 2025, and the police chief earned $267,811.04 in calendar year 2025. These amounts are the year‑to‑date compensation figures for the most recent complete salary year.

### highest-paid-positions

> What are the highest paid positions in Louisville Metro government?

```sql
SELECT job_title, department, ROUND(avg_total_comp, 2) AS avg_total_comp, ROUND(max_total_comp, 2) AS max_total_comp, employee_count
FROM summary_top_salaries
WHERE calendar_year = (SELECT MAX(calendar_year) FROM summary_top_salaries)
  AND job_title IS NOT NULL
ORDER BY avg_total_comp DESC
LIMIT 10
```

The highest paid positions in Louisville Metro government are:

1. Police Chief – Louisville Metro Police Department – $267,811.04 (1 employee)  
2. Director – Louisville Zoo – $213,466.08 (1 employee)  
3. Deputy Police Chief – Louisville Metro Police Department – average $202,891.98, maximum $204,679.22 (2 employees)  
4. Fire Chief – Louisville Fire – $197,516.03 (1 employee)  
5. Deputy Mayor – Chief of Staff, General Counsel – Mayor Office – $193,776.70 (1 employee)  

These figures reflect average total compensation for calendar year 2025, the most recent complete year of salary data. Compensation amounts do not include benefits.

### police-overtime-2024

> How much overtime pay did police employees receive in 2024?

```sql
SELECT ROUND(SUM(Overtime_Rate), 2) AS total_overtime_pay
FROM salary_data
WHERE CalYear = 2024
  AND Department ILIKE '%police%'
  AND Overtime_Rate IS NOT NULL
  AND Overtime_Rate > 0;
```

Police employees received about $23.6 million in overtime pay in calendar year 2024.

### registered-agents

> Who are the registered agents for the top 10 contractors by total spend?

```sql
SELECT payee, total_spend, sos_registered_agent
FROM summary_top_contractors
ORDER BY total_spend DESC
LIMIT 10
```

The registered agents for the ten contractors with the highest total spending are:

1. Louisville Gas & Electric Company – $345.7 M – C T Corporation System, 306 W Main St Suite 512, Frankfort, KY 40601  
2. Hall Contracting of Kentucky Inc – $161.0 M – Stephen M. Priebe, 3800 Crittenden Drive, Louisville, KY 40209  
3. Motorola Solutions Inc – $89.1 M – C T Corporation System, 306 W Main St Suite 512, Frankfort, KY 40601  
4. Flynn Brothers Contracting Inc – $73.0 M – Steve E. Mossholder, Jr., 4515 Bishop Ln Ste A, Louisville, KY 40218  
5. Sullivan & Cozart Inc – $69.8 M – Thomas E. Roma, Jr., Manion Stigger, LLP, 2301 River Road Suite 101, Louisville, KY 40206  
6. Humana Health Plan Inc – $67.0 M – C T Corporation System, 306 W Main St, Suite 512, Frankfort, KY 40601  
7. Louisville Paving Company Inc – $62.1 M – Luke Fitzgerald, 15415 Shelbyville Road, Louisville, KY 40245  
8. Genuine Parts Company – $61.5 M – C T Corporation System, 306 W Main Street Suite 512, Frankfort, KY 40601  
9. Waste Management of Kentucky LLC – $54.6 M – C T Corporation System, 306 W Main St Suite 512, Frankfort, KY 40601  
10. CDW LLC – $53.2 M – Corporation Service Company, 315 High Street, Frankfort, KY 40601

### grant-funding

> How much has Louisville spent from grant and federal funds, and which funds are largest?

```sql
SELECT COALESCE(fund, 'TOTAL - ALL GRANT FUNDS') AS fund,
       ROUND(SUM(total_amount), 2) AS total_amount
FROM summary_grant_funding
GROUP BY ROLLUP(fund)
ORDER BY total_amount DESC NULLS LAST;
```

Louisville has spent $1.174 billion from grant and federal funds (TOTAL - ALL GRANT FUNDS).

The five largest individual funds, out of 102 grant‑fund rows, are:

1. Grant Fund – $437.5 M  
2. Federally Funded – $151.6 M  
3. Pass Thru Federal Other – $122.9 M  
4. CAP KACA Funding – $75.3 M  
5. CARES Coronavirus Relief Fund (CRF) – $73.8 M

### largest-payments

> What are the largest single payments ever made and who received them?

```sql
SELECT payee AS payee_canonical, agency AS agency_canonical, ROUND(invoice_amount, 2) AS invoice_amount, invoice_date
FROM summary_largest_payments
WHERE invoice_amount > 0
ORDER BY invoice_amount DESC
LIMIT 10
```

The five largest single payments recorded are:

1. Louisville Arena Authority Inc – OMB Finance – $12.0M – 2022‑10‑31  
2. Motorola Solutions Inc – Emergency Services & MetroSafe – $11.6M – 2009‑03‑13  
3. Motorola Solutions Inc – Emergency Services & MetroSafe – $11.6M – 2009‑03‑13  
4. Louisville Arena Authority Inc – OMB Finance – $10.8M – 2025‑10‑21  
5. Louisville Arena Authority Inc – OMB Finance – $10.8M – 2023‑11‑03  

These amounts come from the city’s expenditure records through FY 2025; FY 2026 data is incomplete.

### capital-projects

> What are the largest capital projects and how much was allocated to each?

```sql
SELECT Project_Na AS project_name, Allocation
FROM capital_projects
WHERE Allocation IS NOT NULL
ORDER BY Allocation DESC;
```

The five largest capital projects by allocation are:

1. Rhodia Brownfields Remediation – $10.0 M  
2. Main Library – $8.0 M  
3. Algonquin Swimming Pool – $7.5 M  
4. Baxter Community Center – $6.0 M  
5. Fern Creek Library – $5.0 M  

These amounts come directly from the city’s capital‑projects records.

### contract-splitting-fy2025

> Are there signs of contract splitting in FY2025?

```sql
SELECT payee_canonical, agency_canonical, COUNT(*) AS payments_just_under, ROUND(SUM(invoice_amount), 2) AS total, ROUND(AVG(invoice_amount), 2) AS avg_invoice FROM expenditures WHERE fiscal_year = 2025 AND is_data_artifact = FALSE AND invoice_amount BETWEEN 4000 AND 4999.99 GROUP BY payee_canonical, agency_canonical HAVING COUNT(*) >= 5 ORDER BY payments_just_under DESC, total DESC LIMIT 25
```

Yes, the FY 2025 expenditure data includes many vendors that received a large number of invoices just under $5,000, a pattern that can indicate contract splitting.

1. Louisville Computer Guys Inc – Parks & Recreation – 120 payments – total $528,000 – average $4,400.00  
2. Overdrive Inc – Louisville Free Public Library – 99 payments – total $439,736 – average $4,441.78  
3. Louisville Gas & Electric Company – Facilities & Fleet Management – 35 payments – total $156,149 – average $4,461.40  
4. Strand Associates Inc – Public Works & Assets – 33 payments – total $146,420 – average $4,436.97  
5. Walker Mechanical Contractors Inc – Department of Corrections – 32 payments – total $141,531 – average $4,422.84  

These repeated small payments appear across several departments, which is a pattern sometimes examined for contract splitting, though the data alone does not confirm any improper activity.

### honest-empty-fy2031

> How much did Louisville Fire spend in fiscal year 2031?

```sql
SELECT ROUND(SUM(extended_amount), 2) AS total_spend
FROM expenditures
WHERE fiscal_year = 2031
  AND agency_canonical = 'Louisville Fire'
  AND extended_amount IS NOT NULL
  AND is_data_artifact = FALSE;
```

The expenditures table only contains fiscal years up to 2026 (with 2026 partial), so a query for fiscal year 2031 falls outside the available date range and returns no rows.  

Rephrased questions that should return data:  
1. “How much did Louisville Fire spend in fiscal year 2025?”  
2. “What is the total spend for Louisville Fire across all fiscal years in the dataset?”

### off-topic

> Tell me a joke about cats.

```sql
(none)
```

This question doesn't appear to be answerable from the Louisville Metro expenditure data. Try asking about government spending, agency budgets, contractor payments, employee salaries, or capital projects.
