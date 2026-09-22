# Lambda image cold start — 2026-09-21

Image: `Dockerfile.lambda` at commit after 5028c23 (schema snapshot in the
artifact), 185 MB, arm64, python:3.12-slim + Lambda Web Adapter 1.0.0.
Function: `lou-bot-coldstart`, 1 GB ephemeral storage, placeholder LLM keys.
Each row is a guaranteed cold environment (configuration change between
invocations); the invoke is `GET /api/health`, which counts every table.
`Init Duration` is what the user waits for on a cold start before the request
itself runs; the request took ~60 ms in every case.

## Before the schema snapshot (startup recomputed the compact schema)

| memory | run 1 | run 2 | run 3 | max mem used |
|---|---|---|---|---|
| 1024 MB | **10 000 ms → init-timeout retry, 12.6 s billed** | 3 288 ms | 4 219 ms | 549 MB |
| 1769 MB | 6 055 ms | 6 238 ms | 6 027 ms | 547 MB |
| 3008 MB | 7 334 ms | 4 823 ms | 4 939 ms | 544 MB |

Memory size did not help: Lambda already runs the init phase with boosted CPU,
so the time is inside our startup, not the platform. The 10 s row was the
function's first-ever invoke (image layers not yet in Lambda's cache); Lambda
caps init at 10 s, retries, and bills the retry inside the invoke.

Timeline of one 4.9 s cold start from the CloudWatch stream: ~3.4 s from
process start to "Started server process" (python + pandas/duckdb/openai/
fastapi imports and module-level work), then 1.5 s in the startup event, of
which ~1.3 s was `get_compact_schema_description` sampling DISTINCT values of
every low-cardinality column across all tables. Locally the same work is
0.3 s; Lambda's init CPU is roughly 4-5x slower than an M-series core.

## After (compact schema snapshotted into the artifact's `_meta` table)

| memory | run 1 | run 2 | run 3 | max mem used |
|---|---|---|---|---|
| 1024 MB | 4 455 ms | 3 119 ms | **2 549 ms** | 297 MB |
| 1769 MB | 3 135 ms | 4 107 ms | 3 114 ms | 291 MB |

Mean 3.4 s, every run under the 5 s acceptance bar. Peak memory halved as a
side effect: the DISTINCT sampling was what pushed DuckDB's buffers up.

## What is left in the cold start (~3.4 s)

Roughly 2.5-3 s is interpreter start plus imports (pandas, numpy, duckdb,
openai, fastapi, pydantic) on Lambda's init CPU; the rest is the corpus
probe (one real FTS retrieve), opening the artifact (10 ms), year context and
prompt assembly. Options if this ever needs to be lower, none taken now:

- Lazy-import `pandas` — no gain: `execute_sql_safe` returns a DataFrame, so
  the first request would pay it instead.
- SnapStart — Python zip packages only, not container images, so not
  available here.
- Provisioned concurrency — removes cold starts entirely but is a fixed
  monthly charge, outside the cost envelope.
- A post-deploy warm invoke, so the 10 s image-cache miss lands on the deploy
  and not on the first visitor. Cheap; belongs in the IaC/deploy step (i0q).

## Sizing recommendation for i0q

1769 MB (one full vCPU for query execution; cold start is the same as 1024).
Peak memory observed is ~290 MB on health; expect more on wide GROUP BYs
over the 2.2M-row table, and `DUCKDB_TEMP_DIR=/tmp/duckdb` with 1 GB
ephemeral storage catches a spill. Timeout 120 s (answers can run past 60 s;
CloudFront's origin read timeout is idle-based and was verified with a 70 s
stream in the 4l4 spike).
