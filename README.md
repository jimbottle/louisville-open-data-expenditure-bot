# Lou — an agentic analytics assistant for Louisville Metro's open data

**Live:** https://louisville.raylytics.io

Ask a plain-English question about Louisville Metro government spending —
*"How much did the Fire department spend on vehicles in FY2024?"* — and Lou
writes the SQL, runs it against 2.26 million expenditure rows (FY2008–FY2026),
checks its own work, charts the result, and explains it in plain language,
citing the council legislation that explains the money when there is any.

It is a working product with real users, and it is built to be **accurate
first**: every layer below exists because a specific wrong answer was observed
and then made impossible to repeat.

```
Q: How much did the Louisville Fire department spend on vehicles in fiscal year 2024?

  SELECT ROUND(SUM(extended_amount), 2) AS total_spend
  FROM expenditures
  WHERE agency_canonical = 'Louisville Fire' AND fiscal_year = 2024
    AND is_data_artifact = FALSE
    AND (spend_category ILIKE '%fleet%' OR spend_category ILIKE '%automotive%'
         OR spend_category ILIKE '%vehicle%' OR spend_category ILIKE '%truck%')

A: Louisville Fire spent $1.58M on vehicle-related purchases in fiscal year 2024.
```

## What is in the box

| Layer | What it does |
|---|---|
| **Data engine** (`data_model.py`) | Loads the city's CSVs into DuckDB, unifies two schema eras, builds canonical agency/payee columns, flags offsetting and artifact rows, materializes summary tables, and indexes every categorical vocabulary. City-agnostic: everything Louisville-specific lives in `cities/louisville/city.yaml`. |
| **Agent loop** (`app.py`, `analytics_agent.py`, `grounding.py`) | question → vocabulary grounding → SQL generation → safety guard → execute → **verify-and-repair** → chart inference → document retrieval → draft interpretation → refinement pass → citations, streamed as SSE. |
| **Provider resilience** | OpenRouter (free tier) primary, Cerebras fallback; automatic model replacement when a provider retires a model; quota/daily-cap/rate-limit classification with honest user-facing messages. |
| **Evaluation** (`eval/`) | An LLM-in-the-loop harness that drives the real request path over a golden question set and scores both the served SQL and the prose. |
| **Operations** | Prebuilt read-only DuckDB artifact, versioned response cache, per-IP rate limiting behind a trusted proxy, health endpoint, uptime probe from CI, and a dead-man's switch that pages when the API stops answering. |

## The request path

```
 question ─┬─▶ grounding.grounding_block ─────────────┐   (real values the question's
           │   "vehicles" → 51 Automotive/Fleet/…      │    words match — no LLM call)
           │                                           ▼
           └─▶ generate_sql (system prompt + schema + vocabulary block)
                     │
                     ▼
               _looks_like_sql? ── no ──▶ off-topic reply
                     │ yes
                     ▼
               BLOCKED_SQL guard → fix_sql (canonical columns) → DuckDB (read-only, no FS access)
                     │
                     ▼
               empty or all-NULL? ── yes ──▶ grounding.diagnose_filters
                     │                          │ a filter matched nothing / was the narrow
                     │                          │ corner of a wider family → ONE regeneration
                     │                          │ with the real values; genuinely empty → explain
                     ▼                          ▼
               order_for_display, totals moved last, truncation notes
                     │
                     ├─▶ infer_chart  (bar/line/pie, currency vs count axis)
                     ├─▶ rag.retrieve (BM25 over council legislation) → topic gate
                     ▼
               interpret (draft, server-side) ─▶ refine (streamed to the reader)
                     │
                     ▼
               citations footer (only file numbers the answer actually used)
```

Every stage emits a structured SSE event (`status`, `sql`, `results`, `chart`,
`interpretation`, `sources`, `info`, `log`, `debug`, `usage`, `error`,
`done`), so the UI can show progress, the dev toggle can show the SQL and
timings retroactively, and a failure anywhere still terminates the stream
cleanly instead of leaving a spinner.

## Accuracy engineering

The interesting part of a text-to-SQL product is not the model call. It is
everything that keeps a fluent model from being confidently wrong. Each of
these was added in response to an observed failure.

**Canonicalization.** Agencies and vendors arrive under dozens of spellings
(`LG&E`, `LOUISVILLE GAS & ELECTRIC COMPANY`, `CDW GOVT #1234`). Curated exact
and prefix maps produce `agency_canonical` / `payee_canonical`; a
post-processor rewrites generated SQL that groups by the raw column. Seeding
tooling (`canonical_seed.py`) bootstraps the maps for a new city.

**Data-quality flags.** Government ledgers contain offsetting pairs and
correction artifacts (a $224M entry that nets to zero). `is_offsetting` and
`is_data_artifact` are computed at load time from configurable rules, and the
prompt tells the model when to use `SUM(extended_amount)` versus
`invoice_amount`.

**Pre-computed summary tables.** The starter questions ("which agencies spend
the most", "highest-paid positions", grant funding by source) are answered
from validated summary tables rather than a fresh 2M-row aggregation, and the
prompt carries the exact query for the ones that are easy to get subtly wrong
(ranking positions by *average* pay, not by one officer's overtime-inflated
maximum).

**Vocabulary grounding** (`grounding.py`). The schema prompt can enumerate a
column's values only when there are a dozen or fewer; `spend_category` has
979, `fund` 246, `jobTitle` 1,537. Without them the model guesses literals —
in production, "vehicles" became `spend_category ILIKE '%Vehicle%'`, which
matched nothing, and the answer was "no recorded vehicle spending" when the
real figure was $1.58M under *Automotive Parts & Accessories*, *Automotive
Fuel*, and friends. Now a `_value_index` (every categorical value with its
dollar weight, built at load time) backs two mechanisms:

- *Proactive:* the question's content words — plus synonyms, so "vehicles"
  reaches "fleet" and "American Rescue Plan" reaches the fund literally named
  `ARP` — are looked up and the matching real values are appended to the SQL
  request, with a covering pattern when a family is large.
- *Reactive (verify-and-repair):* when a query returns nothing, its string
  literals are checked against the index. A literal that matches no value, or
  that is the narrow corner of a wider family, earns exactly one regeneration
  with the real values spelled out; a result whose filters all matched is a
  genuine empty and is reported as such. The reader is told when this
  happened. Diagnosis is scoped to the tables the SQL actually read, so
  `fund = 'ARP'` against a summary table that lacks it is caught and pointed
  at the table that has it.

**Result framing the model cannot misread.** Long results are truncated
head-and-tail with an explicit note stating the row count and that the middle
is missing; ROLLUP totals are moved to the end and labelled; the order the
reader sees is the order the model interprets. Each of these closed a
specific misreading (a 24-of-102 list presented as "all the sources"; a grand
total reported as the largest item).

**Two-pass interpretation.** A draft interpretation is written server-side,
then a refinement pass with a lean, results-anchored rubric rewrites it for
plain language and checks every number against the results table (no
rescaling, no invented totals, no summing of overlapping views). If the
refiner fails, the draft is served; an answer is never lost.

**Data-derived facts, never hardcoded.** Which fiscal year is complete, how
far payments run, which salary year can be cited — all computed from the
loaded data at startup and injected into the prompts, so a data refresh
cannot leave the prompt asserting last quarter's truth.

**Prompt-hashed cache.** Answers are cached as their SSE frames, keyed by a
hash of every model-visible input (prompts, truncation notes, grounding
wording). A prompt edit orphans every stale answer automatically; a fix can
never be shadowed by a cached pre-fix reply.

**Guards.** A blocklist stops file access and DDL in generated SQL, and the
serving DuckDB connection is opened read-only with external access disabled —
`read_csv('/etc/passwd')` and `COPY TO` are both blocked at the engine, not
just the regex. Off-topic questions are detected from the shape of the
model's reply (is the first token a SQL statement?), and injected
instructions inside a question are neutralized by the refinement rubric.

## Measured, not asserted

`eval/run_eval.py` drives the real `/api/ask` path — same prompts, grounding,
repair loop and refinement the site serves — over `eval/golden.yaml` (23
questions across vocabulary, summary-table, fiscal-year, named-entity and
guard categories). For each question it re-executes the *served* SQL locally
and checks it against a reference query computed on the same data, then
checks that the prose the reader saw states the reference figure.

Same 23 questions, same model (`gpt-oss-120b` on Cerebras), one day's work:

| | pass | SQL correct | answer states the figure | mean latency |
|---|---|---|---|---|
| before this pass | 19 / 23 | 20 | 20 | 7.2 s |
| after | **23 / 23** | 23 | 23 | 2.2 s |

The full scorecards, with every served query and answer, are in
`eval/results/` — including the intermediate run where the first version of
vocabulary grounding *lowered* the score to 17/23 by distracting the model on
three questions, and the diagnosis that fixed it. The harness is for finding
failures, not for hiding them; a small golden set with an honest history is
worth more than a large one nobody re-runs.

## Reliability

- **Provider failover.** OpenRouter's free models are primary; a 402, a spent
  daily allowance, or an exhausted retry ladder fails over to Cerebras
  pay-as-you-go, and the primary is latched out for fifteen minutes so
  every later call does not pay to rediscover the outage.
- **Model deprecation.** Providers retire models without notice (this bot's
  original model started 404-ing under it). On `model_not_found` a
  replacement is resolved from a vetted list — never from the paid
  catalogue — recorded process-wide, and surfaced in `/api/health`.
- **Honest errors.** Rate limit, out of credit, daily cap, provider down, and
  "your question could not be turned into a query" are five different
  messages, because they need five different responses from the reader.
- **Cold-start discipline.** Production opens a prebuilt DuckDB artifact
  (0.5 s, 400 MB) instead of rebuilding from 531 MB of CSV (5 s, 1.9 GB); the
  build is atomic and a missing artifact fails loudly at boot rather than
  degrading into the rebuild that once overran the health check.
- **Monitoring.** A GitHub Actions probe hits the production API every 30
  minutes; a dead-man's switch pings healthchecks.io only while the API
  actually answers a question, and pages within four minutes when it stops.

## Multi-city by construction

The engine knows nothing about Louisville. A city is a config pack:

```
cities/louisville/
  city.yaml           sources & schema eras, canonicalization, data-quality rules,
                      summary SQL, data dictionary, branding, grounding synonyms
  agency_map.csv      curated canonical maps
  payee_map.csv
  payee_prefix_map.csv
```

`cities/cincinnati/` is a second, runnable pack; onboarding notes and the
canonical-model format are in `docs/`.

## Running it

```bash
pip install -r requirements.txt
python refresh_data.py --pull-only      # fetch the open-data CSVs into data/
python data_model.py --materialize data/lou.duckdb
python rag.py ingest                    # optional: council legislation corpus

export OPENROUTER_API_KEY=...           # or CEREBRAS_PAID_API_KEY
PREBUILT_DB=data/lou.duckdb uvicorn app:app --port 8000
```

```bash
python -m pytest -q                     # ~600 tests; data-bound ones skip without data/
python eval/run_eval.py --provider cerebras --label my-change   # real-model eval
```

Deployment (Docker, Cloudflare tunnel, volumes, verification steps) is
documented in `CLAUDE.md`; a planned migration to AWS Lambda is analysed in
`LOU_MIGRATION_COMPAT.md`.

## Known limitations

- Retrieval is BM25 over ordinance *titles*; the topic gate removes the worst
  noise but relevance judgement is still the model's, and a weaker model
  will occasionally cite a loosely related ordinance.
- The free-tier primary provider allows ~16–25 questions a day before every
  answer is served by the paid fallback; that is a deliberate cost choice.
- Fiscal-year defaulting ("last year" → the latest complete FY) is a
  convention the prompt enforces; genuinely ambiguous questions are answered
  under a stated assumption rather than asked back.
- `DuckDB 1.5.1` has a planner bug (window partitioned by a column that is
  also filtered with `<>` in the same SELECT returns swapped column values);
  the engine's own queries avoid the shape, generated SQL could still hit it.

## Repository map

```
app.py                 FastAPI app, /api/ask agent loop, cache, rate limit, health
analytics_agent.py     LLM calls, retry/failover/model-fallback, SQL guard, prompts
grounding.py           vocabulary index, question-term lookup, filter diagnosis
data_model.py          city-agnostic DuckDB engine (load, canonicalize, flag, summarize)
city_config.py         config-pack loader
rag.py                 Legistar corpus + BM25 retrieval + citation links
cities/                per-city config packs
static/index.html      the chat UI (vanilla JS, Chart.js)
eval/                  golden questions, eval runner, scorecards
tests/                 ~600 tests (engine invariants, prompts, endpoint flow, fallbacks)
monitoring/            heartbeat script + healthchecks.io setup
docs/                  canonical model, onboarding, RAG design, accuracy plan
```
