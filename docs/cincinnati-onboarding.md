# Cincinnati Onboarding Log (second-city portability proof)

Tracks louisville-open-data-ftq. Executed 2026-07-31. Result: **the bot
answered real Cincinnati questions end-to-end** (NL → SQL → DuckDB → chart →
streamed interpretation) on a config pack, with zero city-specific engine code.

## What was done

1. **Pull**: `pull_socrata.py` (new, reusable) bulk-downloaded 4 datasets from
   data.cincinnati-oh.gov — vendor payments (215 MB, 1,233,639 rows,
   FY2014–2027), salaries w/ demographics (6,877), business licenses (35,256),
   budget (222,248) — in ~2 minutes, one HTTP call each, no pagination.
2. **Engine additions** (one-time, benefit every future city): glob/literal
   `files` sources without a year range, and `column_map` on the `duckdb_union`
   reader via `SELECT * RENAME` (fast in-query mapping, no pandas pass).
3. **Config pack** `cities/cincinnati/city.yaml`: 14-column map to canonical v0
   names, data-quality params (`group_key: trans_id` — flagged 57 offsetting
   rows on first load), 5 summary tables incl. a budget summary Louisville
   can't have, and a curated dictionary written from the actual CSV headers +
   Socrata column metadata.
4. **Verification**: loader row count matches the survey exactly (1,233,639);
   spend per FY is plausible ($550–820M); `payment_date` sniffed as a real
   TIMESTAMP; app served `/api/health` ok and answered "Which five city
   departments spent the most in FY2025?" correctly (Dept of Sewers, $107.1M).

Run it: `CITY_CONFIG=cities/cincinnati/city.yaml DATA_DIR=data_cincinnati uvicorn app:app`

## Effort (the cost-of-sale numbers)

| Bucket | Time | Repeats per city? |
|---|---|---|
| Platform work (Socrata pull script + engine glob/RENAME) | ~20 min | **No** — one-time, now done |
| Config authoring + dictionary curation (first pass) | ~10 min | Yes |
| Data pull | ~2 min | Yes (automated) |
| Verification incl. live LLM Q&A | ~5 min | Yes |
| **Marginal cost of city #2, to a working bot** | **~15–20 min** | |

Honest caveats on that number: it's the *agent-executed* first pass, with the
city survey already done (knowing which datasets/ids to pull was the survey's
work), and it excludes the curation that separates "working" from "polished":

## Remaining for Louisville-level quality (not blocking, tracked as future work)

- **Canonical map seeding/curation** — agency/payee maps are unseeded stubs
  (engine mirrors source values). Louisville's maps took real curation; this
  is the dominant human cost per city and needs the seeding tool from
  docs/canonical-model.md §5.
- **Frontend branding + starter questions** — UI still says Louisville; needs
  config-driven branding.
- **Fuzzy join quality** (salaries/licenses ↔ expenditures) untested.
- **Hosting** — a second deployment target/subdomain if Cincinnati should be
  publicly demoable.
