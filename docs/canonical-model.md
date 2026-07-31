# Canonical City Data Model — v0

Design for the schema layer of "Lou for cities" (issue louisville-open-data-8ve).
Grounded in `docs/city-survey.md`: five live checkbook schemas were verified
(Louisville, Cincinnati, Kansas City, Chicago, Baltimore) and this model must
express all of them. Louisville must map on with **zero information loss**
(§7 proves it). The config format that carries this model per-city feeds the
engine refactor (louisville-open-data-jy0).

## 1. Design rules

1. **Canonical names are a vocabulary, not a straitjacket.** A source column
   that matches a canonical concept is renamed to it. A source column that
   matches nothing is **kept verbatim as an extension column** and documented
   in the city's dictionary. Zero loss is guaranteed by construction — mapping
   never drops a column.
2. **Tiers, not requirements.** The survey shows huge variance (Chicago: 6
   columns; Baltimore: 17). The model defines what each tier unlocks rather
   than rejecting thin cities: Required = the bot works; Recommended = the
   high-value question classes work; Optional = extra dimensions.
3. **Code/description pairs collapse to the description.** Cincinnati, KC, and
   Baltimore all publish `X_code` + `X_desc(ription)` pairs. The canonical
   column is the human-readable one (`fund`, `agency`, `category`); the code
   lands in a `*_code` extension column.
4. **Eras are first-class.** Every multi-year city surveyed has fragmentation
   (Louisville 2 schemas, Baltimore 2 schemas/3 datasets, KC 11 datasets, 1
   type break). "Multiple source datasets → per-era column maps → one unified
   table" is the core loader concept, not a special case.

## 2. Core table: `expenditures`

One row = one payment line item (the finest grain the city publishes).

### Required (bot functions at all)
| Canonical | Meaning | Seen as |
|---|---|---|
| `payee` | vendor/recipient name | payee, vendor_name, Supplier_Name |
| `amount` | line-item amount — THE spend measure | extended_amount (Lou), amount (Cincy/Chi), sum_amount (KC), Payment_Amount_split (Balt) |
| `payment_date` *or* `invoice_date` | when | payment_date, record_date, check_date, Payment_Date |
| `agency` | department/agency (human-readable) | agency, dept_desc, deptid_descr, department_name, Agency |

Nashville's missing `payee` is exactly why it scored 3/5: no Required tier, no
onboard. `fiscal_year` is *not* required as a source column — it is derived
(§4) when absent (Chicago, KC, Baltimore).

### Recommended (unlocks the high-value question classes)
`fiscal_year`, `fund`, `category` (the city's main spend classification —
spend_category/exp_acct_cat_desc/Category), `invoice_number`, `payment_number`
(check_no/payment_no/Check_Number), `payment_amount` (check-level total, distinct
from line `amount` — KC/Lou/Balt publish both; summing it double-counts, see §6).

### Optional (extra dimensions when published)
`expenditure_type` (Operating/Capital), `expenditure_category`, `cost_center`,
`program`, `project`, `grant`, `contract_number`, `payment_method`,
`sub_agency`, `department`, `sub_department`, `region`, `financing_source`,
`invoice_amount`, `payment_void_date`.

### Engine-derived (never mapped from source)
`agency_canonical`, `payee_canonical` (§5), `is_offsetting`,
`is_data_artifact` (§6), `source_era` (which era/mapping produced the row —
lets answers caveat cross-era comparisons).

## 3. Optional enrichment tables

Each lights up features when present; absence degrades gracefully.

| Table | Canonical core | Survey presence |
|---|---|---|
| `budget` | fiscal_year, agency, fund, category, budget_amount, actual_amount?, budget_stage | 7/12 cities — **more common than checkbooks**; new table (Louisville doesn't publish one). Enables budget-vs-actual and lets budget-only cities (Chattanooga) onboard in a reduced mode |
| `salary_data` | year, employee_name, agency, job_title, annual_rate, total_comp + pay-component extensions | 5/12 |
| `capital_projects` | project_name, agency, category, allocation, region, start/end dates | 4/12, often partial |
| `contractor_licenses` | name, license_number, category, expiration | 6/12 |
| `staff_demographics` | agency, gender, ethnic_group, education, employee_count | 4/12 (Cincinnati embeds these per-employee in salaries — an era-style mapping reshapes that into either table) |
| `contractor_profiles` | cross-source enrichment (Lou: KY SOS registry) | Lou differentiator; per-state adapters, out of v0 scope but the table shape is canonical |

## 4. Source mapping rules (the config's schema section)

Per table, a city declares **sources**; each source declares **eras**:

```yaml
expenditures:
  grain: invoice line item
  sources:
    - id: expenditures_old            # era 1
      kind: arcgis                    # arcgis | socrata | csv | excel
      items: "eExpenditures_{2008..2017}"
      column_map: {Vendor_Name: payee, DistributionAmt: amount, Agency_Name: agency, ...}
      coerce: {invoice_date: epoch_ms_date, fiscal_year: int}
    - id: expenditures_new            # era 2
      kind: arcgis
      items: "eExpenditures_{2018..}"
      column_map: {extended_amount: amount, ...}   # mostly 1:1
```

Rule set an era supports:
- **column_map**: source → canonical (unmapped columns pass through as
  extensions; collisions across eras union by name, missing → NULL — exactly
  today's old-era loader behavior).
- **coerce**: `epoch_ms_date` (Lou old era), `text_date:%m/%d/%Y` (Chicago),
  `cast:text` (KC's 2026 number→text fund/deptid break), `int`.
- **derive**: `fiscal_year: from payment_date` using the city-level
  `fiscal_year_start_month` (Lou/Balt = 7, KC = 5, Chicago = 1). Cities with an
  explicit fiscal_year column skip this.
- **filters**: row-level excludes (e.g. Raleigh's portal surfacing county
  data is why source ids are explicit, never catalog searches).
- **grain caveat** (Chicago): an era can declare `grain_note` that the agent
  surfaces when a question crosses the boundary (pre-2-year rollups).

## 5. Canonicalization (per-city data, engine mechanism)

Unchanged from today's proven design: per dimension (`agency`, `payee`), an
**exact map** + **prefix map** applied as `*_canonical` columns. What v0 adds:
these live in the config pack as data files (CSV/YAML), not Python modules,
and the engine ships a **seeding tool** (normalize case/punctuation/suffixes,
cluster near-duplicates, emit a draft map for human/LLM curation). Curation
effort is the real onboarding cost the second-city proof (ftq) must measure.

## 6. Data-quality flags (engine mechanism, per-city parameters)

- **Offsetting**: flag zero-sum groups. Parameter: `group_key`, which may
  name a canonical or extension column (Lou: `invoice_number`; KC: the
  `voucher` extension). KC's documented voucher/sum_amount
  double-counting caveat is this same pattern — its rule is "sum `amount`
  (line), never `payment_amount` per group", which the model encodes by making
  `amount` the only blessed measure.
- **Artifact**: `|amount| > threshold` with offsetting counterpart (Lou:
  100M). Threshold per city.
- Flags are engine-computed columns, and summary specs must filter
  `is_data_artifact = FALSE` — same as today.

## 7. Zero-loss check: Louisville on the canonical model

Every column of Louisville's unified schema, mapped:

- **Required**: payee→`payee`, extended_amount→`amount`,
  invoice_date/payment_date→same, agency→`agency` ✓
- **Recommended**: fiscal_year, fund, invoice_number, payment_number,
  payment_amount→same; spend_category→`category` ✓
- **Optional**: expenditure_type, expenditure_category, cost_center, project,
  program, grant_→`grant`, financing_source, region, sub_agency, department,
  sub_department, invoice_amount, payment_void_date→same ✓
- **Extensions**: stimulus_type (nothing canonical — kept verbatim,
  documented) ✓
- **Engine-derived**: agency_canonical, payee_canonical, is_offsetting,
  is_data_artifact — regenerated, not mapped ✓

25/25 source columns land; 24 canonical, 1 extension. Zero loss. ✓

Shortlist fit: **Cincinnati** maps 14/14 (desc-pair rule; acct_period,
trans_id, trans_line_no as extensions; explicit fiscal_year). **KC** maps
13/13 (sum_amount→amount, payment_no→payment_number, desc pairs; `voucher`
kept as an extension column; derived FY, group_key=`voucher`). **Baltimore** FY22+ and FY2020 are two eras of one
config — its "two schemas" problem is Louisville's, already solved.
**Chicago** maps 6/6 with text-date coercion and a grain_note.

## 8. Out of scope for v0

RAG/document grounding (louisville-open-data-p4y), per-state business-registry
adapters, budget-vs-actual join semantics (needs a real budget city to design
against), Nashville-style supplier-ID resolution.

## v0 acceptance (feeds louisville-open-data-jy0)

1. Config schema (the YAML shapes above) checked into the engine refactor.
2. Louisville expressed entirely as a config pack; loader output identical
   (same health-endpoint table counts, known-answer suite passes).
3. A paper config for Cincinnati and Kansas City written against real schemas
   (no implementation) to prove the format holds for both platforms.
