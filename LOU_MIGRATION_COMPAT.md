# Lou → Serverless Cloud Migration: Compatibility Analysis

**Analyzed:** 2026-08-28 · commit `687240e` · branch `main`
**Target evaluated:** AWS Lambda (Function URL / CloudFront) + S3 + scale-to-zero DB, no VPC, no API Gateway REST
**Method:** static read of the codebase plus live measurement of startup, memory, and query cost on the real 531 MB dataset

---

## 1. Verdict

> ### Lambda-compatible with refactor — **confidence: high** on the technical finding, with one strong caveat on the recommendation.

There is **no genuine architectural blocker**. Every one of the three canonical Cloud Run disqualifiers was checked and none survives:

| Cloud Run disqualifier | Present? | Evidence |
|---|---|---|
| Needs > 15 min execution | **No** | Worst-case request path is ~230 s (§7) |
| Persistent connections (websocket, gateway socket, held DB pool) | **No** | One-shot SSE over `fetch`/`ReadableStream`; DuckDB is an in-process embedded engine with no pool; no websockets anywhere |
| Heavy startup | **Yes, but fixable** | 8.7 s / 1.94 GB today → **measured 2.4 s / 415 MB** after one change (§6) |

**However — and this is the honest part:** Cloud Run reaches an identical outcome, at an identical cost (~$0), for roughly **a quarter of the engineering effort**. The existing `Dockerfile` deploys to Cloud Run essentially unmodified. The Lambda path costs ~30 hours because Lambda's execution model breaks three pieces of in-process state that Cloud Run preserves for free (rate limiter, response cache, stats — §3).

The correct framing is therefore:

- **If the goal is the running service:** choose Cloud Run. Less work, same bill, same scale-to-zero, native SSE.
- **If the goal is portfolio/resume value:** choose Lambda. The work that makes Lambda necessary — externalizing state to DynamoDB, response streaming through a Function URL, IaC — *is* the demonstrable skill. That is a legitimate reason to pick it, but it should be named as the reason rather than dressed up as technical fit.

This report assumes the Lambda target and details what it takes.

### The single most important finding

The app rebuilds its entire analytical database from 531 MB of CSVs **on every process start** (`data_model.py:304`, `app.py:477`). Measured locally:

| | Today (CSV rebuild, in-memory) | Prebuilt DuckDB file (measured) |
|---|---|---|
| Cold init | **8.7 s** (1.9 s import + 5.9 s load + 0.3 s schema) | **2.4 s** total |
| Peak RSS | **1.94 GB** | **415 MB** |
| Artifact size | 531 MB of CSVs | **113 MB** single file |
| Aggregate over 2.26 M rows | — | **0.03 s** |

Materializing the loaded DuckDB catalogue to a file took 11.2 s once, offline. Reopening it read-only and rebuilding the prompt schema took **0.41 s**. This one change converts the largest Lambda liability into a non-issue *and* is worth doing on the current self-hosted deployment regardless — it would have prevented the 2026-08-11 outage whose root cause was a cold start exceeding the health-check start period (`CLAUDE.md`, `monitoring/README.md`).

---

## 2. Blocker table

Severity: **BLOCKER** = prevents deployment · **REFACTOR** = works but must be rewritten · **COSMETIC** = should fix, not load-bearing.

| # | Finding | Files | Severity | Effort |
|---|---|---|---|---|
| 1 | **Full DB rebuild at startup.** `load_all_data()` reads 531 MB of CSVs into an in-memory DuckDB, canonicalizes 76,930 payee variants, and builds 8 summary tables on every boot. 8.7 s / 1.94 GB measured. Lambda's INIT phase soft-limits at ~10 s before init is re-run inside (and billed to) the invocation. | `data_model.py:304-314`, `app.py:477` | **BLOCKER** | **6 h** |
| 2 | **SSE streaming through a Function URL.** `/api/ask` returns `StreamingResponse` over a sync generator. Mangum (the usual FastAPI-on-Lambda shim) **buffers** — it would hold the entire ~40 s answer and emit it at once, destroying the product's core UX. Requires AWS Lambda Web Adapter with `AWS_LWA_INVOKE_MODE=response_stream` + `InvokeMode: RESPONSE_STREAM` on the Function URL, which mandates a container image. | `app.py:1029`, `app.py:1453`, `static/index.html:1270,1300` | **BLOCKER** | **8 h** |
| 3 | **Per-IP rate limiter is process-local.** `ip_requests: dict[str, list[float]]` in module memory. Under Lambda every concurrent container holds an independent dict, so the 5/min cap is effectively unenforced — and it is the only thing standing between an abusive client and a drained LLM balance (a service outage, not a runaway bill — see §5 risk 1). | `app.py:151-198` | **REFACTOR** | **5 h** |
| 4 | **Response cache is a local JSON file + in-process dict.** Load-bearing for cost, not just latency: cached starter answers are what the site serves when the LLM quota is exhausted (`DAILY_CAP_MSG`, `app.py:110`). A per-container cache means near-zero hit rate and an LLM call for nearly every request. | `app.py:822`, `app.py:875-916`, `app.py:1424-1431` | **REFACTOR** | **6 h** |
| 5 | **Stats persisted to local disk** under a `threading.Lock`, holding daily counters and error history that `/api/health` and the uptime workflow read. Lost on every container recycle; racy across instances. | `app.py:203-263`, `.github/workflows/uptime.yml` | **REFACTOR** | **4 h** |
| 6 | **Rotating file logs to a `/logs` volume.** Lambda's filesystem is read-only except `/tmp`; `os.makedirs("/logs")` fails (it is wrapped in try/except, so it degrades rather than crashes — but the handler silently vanishes). | `app.py:22`, `app.py:35-44` | **COSMETIC** | **0.5 h** |
| 7 | **Deployment package exceeds the 250 MB zip limit.** Measured deps ≈ **152 MB** (pandas 57, numpy 28, duckdb native `.so` 40, pydantic 12, openai 7, rest ~8) + 113 MB DB artifact ≈ **265 MB**. Container image (10 GB limit) is required — which #2 mandates anyway, so this costs nothing extra. | `requirements.txt`, `Dockerfile` | **COSMETIC** | 0 h |
| 8 | **DuckDB FTS extension is fetched at query time.** `rag._load_fts()` calls `INSTALL fts` against `extensions.duckdb.org` on miss. Lambda has egress, but `$HOME` is not writable, so the install target must be `/tmp` or the extension pre-baked. The `Dockerfile` already bakes it and `rag.py` already has a failure-suppression guard — carry both forward and set `HOME=/tmp`. | `rag.py:203-251`, `Dockerfile:8` | **COSMETIC** | 1 h |
| 9 | **`time.sleep(3)` on the hot request path.** A deliberate anti-RPM pause. On a container it is free; on Lambda it is 3 s × memory of billed idle on every uncached question. | `app.py:1252` | **COSMETIC** | 0.5 h |
| 10 | **`@app.on_event("startup")` mutates ~8 module globals** (`con`, `sql_system`, `interpret_system`, `client`, `CACHE_VERSION`, …). Works fine under LWA, which runs a real uvicorn inside the Lambda sandbox — flagged only so it is not "fixed" into a per-request cost. | `app.py:440-443` | **COSMETIC** | 0 h |
| 11 | **`/api/health` runs `COUNT(*)` on every table** behind a lock, and is hit every 60 s by the heartbeat. Cheap on a file-backed DuckDB (< 50 ms measured), but it is 43,200 invocations/month of billed compute. | `app.py:681-704` | **COSMETIC** | 1 h |
| 12 | **Trusted-proxy rate-limit config assumes a Docker bridge gateway.** `TRUSTED_PROXY_IPS` must become the CloudFront path (`CloudFront-Viewer-Address` / `X-Forwarded-For`), not an IP allowlist — Lambda has no stable peer address. | `app.py:151-180` | **REFACTOR** | 2 h |

**Total: ~34 hours.** Nothing on this list is unsolved or novel.

### Explicitly checked and clean

- **No background workers, schedulers, cron jobs, or queue consumers in the serving path.** The only two `while True` loops are an interactive CLI REPL (`analytics_agent.py:1229`) and a paginated ingest fetch (`rag.py:130`) — neither runs in the web process.
- **No websockets, no Discord/Slack gateway socket, no polling consumer.**
- **No connection pool.** DuckDB is embedded and in-process; there is nothing to pool and nothing that a serverless-Postgres pooler would be needed for.
- **No file uploads, no user sessions, no auth cookies.** The only credential is `ADMIN_TOKEN`, compared with `hmac.compare_digest` (`app.py:771-780`) — a plain env var, maps directly to SSM.
- **No Tailscale, no local-network dependency in application code.** `cloudflared` is a deployment-topology detail (CLAUDE.md), not a runtime dependency; the app never dials it. `localhost:8000` appears only in operator tooling (`warm_cache.py:80`, `refresh_data.py:224`).
- **Neo4j is offline-only** (`refresh_data.py:181-194`, `graph/`); `neo4j` is not in `requirements.txt` and the server never touches it.
- **Conversation history is client-supplied** (`app.py:1036`), so there is no server-side session state to migrate.

### Offline entry points — all map cleanly

| Script | Purpose | Serverless mapping |
|---|---|---|
| `refresh_data.py` | Pull ArcGIS/Socrata → CSV → rebuild profiles | **EventBridge Scheduler + Lambda** (or CodeBuild if the 15 min cap binds — a full pull of 19 years may). Manual today; no cron exists. |
| `rag.py ingest` | Legistar → `rag_documents.duckdb` | Same schedule as above; produces a 2.4 MB artifact |
| `warm_cache.py` | Pre-answer starter questions post-deploy | Post-deploy step in the pipeline; **more important on Lambda**, since it is what makes the shared cache useful from the first request |
| `pull_arcgis.py`, `pull_socrata.py`, `canonical_seed.py`, `graph/*` | ETL / one-shot tooling | Stay as local/CI scripts. Not deployed. |
| `monitoring/*.sh` + launchd plist | 60 s dead-man's-switch → healthchecks.io | **Replaced** by EventBridge Scheduler + Lambda, or kept as-is pointing at the new URL |

---

## 3. State inventory

| State | Where today | Classification | Target |
|---|---|---|---|
| DuckDB analytical tables (2.26 M rows) | In-memory, rebuilt from CSVs | **Movable — this is the key change** | Prebuilt 113 MB `.duckdb` baked into the container image, read-only |
| RAG document corpus (2.4 MB) | `data/rag_documents.duckdb`, opened read-only per query | **Stateless-safe** | Bake into image alongside the above |
| Response cache (~500 entries) | `.response_cache.json` + module dict | **Movable** | DynamoDB, `question_hash` PK, `cache_version` attribute, TTL |
| Rate-limit buckets | Module dict | **Movable** | DynamoDB with TTL, or CloudFront/WAF rate rule (see §4) |
| Daily stats / error ring | `.stats.json` + `threading.Lock` | **Movable** | DynamoDB atomic counters, or drop for CloudWatch EMF metrics |
| Logs | Rotating file in `/logs` | **Movable** | stdout → CloudWatch, 7-day retention |
| Static frontend (63 KB HTML + 206 KB Chart.js, vendored — no CDN) | `static/`, mounted by FastAPI | **Stateless-safe** | S3 + CloudFront, or keep same-origin through LWA |
| Raw CSVs (531 MB) | `louisville-data` volume | **Movable** | S3, read only by the offline build job — never by the serving function |

**Architectural blockers: none.**

---

## 4. Database recommendation

> ### Recommendation: **none of the three named options.** Ship a prebuilt, read-only DuckDB file as a deployment artifact, with **DynamoDB** alongside it for the small mutable key-value state (cache, rate limits, counters).

The brief anticipated a Postgres codebase. This is not one. Lou is a **read-only OLAP workload with zero runtime writes** to its analytical store: `load_all_data()` finishes by executing `SET enable_external_access = false` (`data_model.py:313`), and `execute_sql_safe()` runs every LLM-generated query through a `BLOCKED_SQL` regex guard (`analytics_agent.py:972-976`). The database is a *build artifact*, refreshed on a manual/scheduled ETL cadence, not a transactional system of record. Against that shape, each named option is worse on its own terms:

**Neon** would add a network round trip to every query that DuckDB currently answers in 30 ms from a memory-mapped local file, in exchange for durability and concurrent writes the app does not use. Its HTTP driver solves a connection-pooling problem Lou does not have. **Aurora Serverless v2** is disqualified outright by the brief's own constraint: it requires a VPC, and a VPC-attached Lambda needs a NAT Gateway (~$32/month, and *the* classic surprise bill) to keep reaching OpenRouter and Cerebras — that alone breaks the ~$0 idle target, before counting Aurora's ~15 s scale-from-zero resume latency against a 0.41 s file open. **DynamoDB as the analytical store** is the clearest non-starter: the entire product is an LLM emitting arbitrary `GROUP BY` / `SUM` / `JOIN` / `ORDER BY` SQL against 2.26 M rows, and the system prompt (`app.py:487-560`) hard-codes dozens of DuckDB-specific query shapes. There is no access-pattern enumeration to design keys around, because the access pattern is "whatever the user asks."

The prebuilt-file approach wins on every measured axis — 113 MB, 0.41 s to open, 0.03 s aggregates, $0 idle, no VPC, no pooler, no connection management — and it *preserves the DuckDB SQL dialect the prompts are written for*, which is the real lock-in here. DynamoDB earns its place for exactly the state that must be shared and mutable across instances (blockers #3, #4, #5): tiny items, simple key lookups, on-demand billing, TTL for free expiry, and no VPC. That is DynamoDB used where it is genuinely good.

**If a managed AWS data service is required for portfolio reasons,** the defensible variant is S3 + DuckDB `httpfs` querying Parquet in place — it keeps the dialect, adds a real cloud-storage story, and costs cents. At 113 MB, baking into the image is still simpler and faster.

---

## 5. Estimated monthly cost

**Assumptions**, stated explicitly:

- Human traffic ~**30 questions/day** (~900/month). The persisted `data/.stats.json` shows `requests_today: 9` on a dev instance; production volume is not instrumented over time, so this is a deliberate over-estimate.
- ~50% cache hit rate on questions (starter chips dominate, and `warm_cache.py` pre-warms them).
- **Heartbeat: every 60 s, forever** — `/api/health` + a `POST /api/ask` empty-question probe = **86,400 invocations/month** independent of human traffic (`monitoring/louisville-bot-heartbeat.sh`). The empty question short-circuits with no LLM call (`app.py:1038`).
- Uptime workflow: every 30 min, 2 requests = 2,880/month.
- Lambda at **2048 MB** (415 MB measured RSS + pandas headroom; more memory also buys vCPU, shortening every path).
- Uncached answer ≈ 40 s wall clock; cached ≈ 50 ms; health probe ≈ 300 ms; cold start ≈ 4 s.

| Component | Usage | Cost |
|---|---|---|
| Lambda requests | ~90,000/month vs **1 M free** | **$0.00** |
| Lambda compute | ~**92,000 GB-s** vs **400,000 free** (450 uncached × 40 s × 2 GB = 36,000; heartbeat 86,400 × 0.3 s × 2 GB = 51,840; cold starts + cached + static ≈ 4,000) | **$0.00** — ~23% of free tier |
| CloudFront | < 1 GB out, ~100 K requests vs 1 TB / 10 M free (perpetual) | **$0.00** |
| S3 | 531 MB CSVs (build inputs) + 270 KB static | **~$0.02** |
| DynamoDB | on-demand, ~200 K RCU/WCU, < 1 MB stored | **~$0.05** |
| CloudWatch Logs | ~150 MB ingest at 7-day retention (verbose per-request `log.info`) | **~$0.08** |
| ECR | ~600 MB image; 500 MB free for 12 months only | **$0.00** yr 1 → **~$0.06** after |
| **Total** | | **≈ $0.15 / month**, **$0.00 idle** |

### Surprise-bill risks — ranked

1. **🔴 The rate limiter does not survive the migration (blocker #3) — an availability risk, not a billing one.** Both LLM providers are hard-capped by construction: OpenRouter's free tier stops at 50 requests/day, and the Cerebras key is **prepaid with a fixed balance that simply stops answering at zero** (HTTP 402 `payment_required`, already handled — the user sees `QUOTA_MSG`, `app.py:121`). Neither can generate a surprise charge. What an abusive client *can* do, with the 5/min cap unenforced across Lambda instances, is drain the prepaid balance in an afternoon and leave the bot serving "out of credit" until it is topped up — plus burn Lambda GB-seconds at ~460 GB-s per pathological request (risk 2). *Deploy with a CloudFront/WAF rate rule or DynamoDB-backed limiter, and reserved concurrency as the compute ceiling.* Balance depletion is already tracked as `louisville-open-data-8uk`.
2. **🟠 A pathological request bills ~230 s.** Three retries at a 16 s base delay (`analytics_agent.py:29-30, 205`) plus two 90 s stream timeouts (`app.py:1289, 1382`) — all `time.sleep`, all billed. One such request costs ~460 GB-s; ~870 of them exhaust the monthly free tier. This compounds risk #1. Cap Lambda concurrency (e.g. 10) as a circuit breaker.
3. **🟡 The 60 s heartbeat is over half the projected compute.** It also means "scale to zero" is partly notional — the function is warm most of the time anyway. Consider 5 min, or probe a static CloudFront path.
4. **🟡 If Aurora is chosen despite §4**, the VPC → NAT Gateway requirement adds **~$32/month** and single-handedly breaks the ~$0 target.
5. **🟢 CloudWatch ingestion** if `log.info` volume grows — 7-day retention and dropping the per-request `debug` SSE mirror keep it in cents.

---

## 6. Migration task list

Ordered by dependency. **S** ≈ ≤ 2 h · **M** ≈ 3–8 h · **L** ≈ > 8 h.

| # | Task | Size | Why this codebase needs it |
|---|---|---|---|
| 1 | **Split `load_all_data()` into a build step + a load step.** Add a `--materialize <path>` mode that runs the existing `_load_expenditures` → `_apply_canonicalization` → `_apply_data_quality` → `_load_enrichment` → `_build_summaries` chain against a *file-backed* connection; add a `load_prebuilt()` that opens it read-only. Keep the CSV path for local dev and for `refresh_data.py`. | **M** | Blocker #1. Measured: 8.7 s/1.94 GB → 2.4 s/415 MB. Note `SET enable_external_access = false` is irreversible once set, so the lockdown must move to the *load* path only. |
| 2 | **Verify SSE survives Lambda Web Adapter + Function URL + CloudFront.** Spike first, before anything else is built: container image with LWA, `AWS_LWA_INVOKE_MODE=response_stream`, `InvokeMode: RESPONSE_STREAM`, and confirm chunks arrive incrementally *through CloudFront* (CloudFront buffering behaviour is the specific risk). | **M** | Blocker #2. If this spike fails, the verdict flips to Cloud Run — so run it first. |
| 3 | **Externalize the rate limiter** to DynamoDB (TTL) or a CloudFront/WAF rate-based rule, and rework `_client_ip()` for the CloudFront header chain instead of `TRUSTED_PROXY_IPS`. | **M** | Blockers #3, #12. **Gates deployment** — see cost risk #1. |
| 4 | **Move the response cache to DynamoDB**, preserving the existing `CACHE_VERSION`-prefixed keys, LRU-touch-on-hit semantics, the `MAX_CACHE_ENTRIES` cap, and the dead-citation-link pruning in `_load_cache()`. | **M** | Blocker #4. Cache correctness is what keeps the LLM bill down and what serves answers when quota is exhausted. |
| 5 | **Move stats to DynamoDB atomic counters** (or CloudWatch EMF), keeping the `/api/health` contract intact — `.github/workflows/uptime.yml` asserts on `status`, `model_fallback`, and `errors_last_hour`. | **M** | Blocker #5. Breaking the health shape silently breaks the uptime monitor. |
| 6 | **Container image**: prebuilt `.duckdb` + FTS extension baked in, `HOME=/tmp`, logging to stdout only, LWA layer. Drop the CSVs from the image. | **S** | Blockers #6, #7, #8 |
| 7 | **Trim the billed-idle paths**: remove or shorten `time.sleep(3)`; make the 16 s retry ladder and 90 s stream timeouts configurable by env. | **S** | Cost risk #2 |
| 8 | **Secrets to SSM Parameter Store** (SecureString): `OPENROUTER_API_KEY`, `CEREBRAS_PAID_API_KEY`, `ADMIN_TOKEN`. All are already read via `os.environ` (`analytics_agent.py:1049-1126`, `app.py:768`) — no code change, only wiring. **Rotate every key during the move**, since the current values have lived in a gitignored `.env` and on a container command line. | **S** | §5 external calls |
| 9 | **IaC**: AWS CDK or Terraform for Lambda + Function URL + CloudFront + OAC + S3 + DynamoDB + log retention + a concurrency cap. | **M** | Also the primary resume artifact |
| 10 | **Move the data-refresh job**: `refresh_data.py` + `rag.py ingest` + the new materialize step → EventBridge Scheduler + Lambda, writing the artifact to S3 and triggering an image rebuild. Check the 15 min cap against a full 19-year pull; fall back to CodeBuild if it binds. | **L** | Only offline job that must survive the move |
| 11 | **Re-point monitoring**: heartbeat + `.github/workflows/uptime.yml` at the new URL; add CloudWatch alarms on Lambda errors/throttles, and carry forward the Cerebras prepaid-balance alert (`louisville-open-data-8uk`) — a drained balance is a silent outage, not a charge. | **S** | The 2026-08-11 outage is the reason this monitoring exists |
| 12 | **Cutover**: deploy in parallel, run `warm_cache.py` against the new URL, verify end-to-end through the production URL per the CLAUDE.md checklist (health + real SSE + one browser question), then move DNS. | **S** | Existing deploy-verification discipline applies unchanged |

---

## 7. Timeouts and duration

| Path | Typical | Worst case |
|---|---|---|
| Cached answer | ~50 ms | — |
| SQL generation (1 LLM call) | ~7 s (benchmarked nemotron, per CLAUDE.md) | +48 s if the retry ladder engages (3 × 16 s, `analytics_agent.py:29-30`) |
| SQL execution | **0.03–0.05 s measured** | negligible |
| Document retrieval (BM25) | ~ms | degrades to `[]` on failure |
| Deliberate anti-RPM pause | **3 s** (`app.py:1252`) | 3 s |
| Interpretation stream | ~10–20 s | 90 s cap (`app.py:1289`) |
| Refinement stream | ~10–20 s | 90 s cap (`app.py:1382`) |
| **Total** | **~30–50 s** | **~230 s** |

Comfortably inside Lambda's 900 s ceiling — and decisively **outside API Gateway REST's 29 s hard limit**, which independently confirms the brief's Function-URL-or-CloudFront choice. The 20 MB response-streaming payload cap is not a concern; answers are capped at 200 words plus a ≤ 50-row table (`analytics_agent.py:214`).

---

## 8. External calls and secrets

| Outbound | When | Notes |
|---|---|---|
| `openrouter.ai/api/v1` | Per question (2–3 calls) | Primary. Free tier: 20 req/min, **50 req/day** |
| `api.cerebras.ai/v1` | Fallback on 429/402 | **Prepaid balance, no auto-recharge** — stops at zero with HTTP 402, surfaced as `QUOTA_MSG`. Bounded by construction; the alert target is depletion, not spend |
| `openrouter.ai/api/v1/models` | On a 404 `model_not_found` | Model-replacement ladder; 15 min latch (`PRIMARY_RECHECK_SECONDS`) is **per-instance module state** and will re-probe more often under Lambda — acceptable, but worth knowing |
| `extensions.duckdb.org` | Only on FTS install miss | Eliminated by baking the extension (task #6) |
| `services1.arcgis.com`, Socrata, Legistar | Offline ETL only | Never from the serving path |

All credentials are read from `os.environ` today and map 1:1 to SSM Parameter Store. **Nothing in application code depends on Tailscale, Cloudflare Tunnel, or the LAN.** The `cloudflared` hop is pure deployment topology and disappears cleanly, replaced by CloudFront.

---

## 9. Resume framing

- **Re-architected a stateful, self-hosted FastAPI analytics service into an event-driven serverless system on AWS** — Lambda behind CloudFront with response streaming (Lambda Web Adapter, `RESPONSE_STREAM` invoke mode) to preserve token-by-token SSE delivery, deliberately avoiding API Gateway REST's 29-second ceiling on a request path that runs up to 230 seconds.
- **Cut cold start 3.6× and memory 4.7× (8.7 s → 2.4 s, 1.94 GB → 415 MB, measured)** by converting a 531 MB CSV-to-DuckDB rebuild-on-boot into a 113 MB precomputed columnar artifact built by a scheduled ETL job — separating the build plane from the serving plane, and eliminating the cold-start class of failure that had previously caused a production outage.
- **Designed for genuine scale-to-zero at ~$0/month idle**, deliberately rejecting VPC-attached Aurora because the required NAT Gateway would have dominated the entire cost envelope; shared mutable state (distributed rate limiting, response cache, usage counters) moved to DynamoDB with TTL, with WAF rate rules and reserved concurrency as cost circuit breakers on an LLM-backed endpoint.
- **Infrastructure defined as code** (CDK/Terraform) covering Lambda, CloudFront + OAC, S3, DynamoDB, EventBridge Scheduler, and log-retention policy — reproducible from an empty account.

---

## Appendix: measurements

All figures measured on this repository's real `data/` directory (531 MB, 74 files) on 2026-08-28, Apple silicon, warm page cache. Lambda's slower per-vCPU performance and cold filesystem will inflate wall-clock numbers; memory and artifact sizes carry over directly.

```
CSV rebuild path (current production behaviour)
  import data_model ............................  1.9 s
  load_all_data('data') ........................  5.9 s
  get_compact_schema_description ...............  0.3 s
  ─────────────────────────────────────────────────────
  total cold init ..............................  8.7 s
  peak RSS ..................................... 1.94 GB
  expenditures rows ............................ 2,257,399 across FY2008-FY2026
  payee canonicalization ....................... 76,930 variants → 76,818
  tables built ................................. 17

Prebuilt-file path (proposed)
  offline materialize (one time, in CI) ........ 11.2 s
  artifact size ................................ 113 MB   (from 531 MB CSV)
  open read-only + schema description ..........  0.41 s
  import + open + schema + year_context ........  2.40 s
  peak RSS .....................................  415 MB
  top-10 payees by SUM over 2.26 M rows ........  0.031 s
  spend by fiscal year (19 groups) .............  0.050 s
  indexed salary lookup ........................  0.005 s

Dependency footprint (unzipped)
  pandas 56.9 · duckdb native .so 39.8 · numpy 27.5 · pydantic 12.5
  openai 6.8 · fastapi+starlette 1.5 · rest ~7.4  →  ~152 MB
  + 113 MB DB artifact = ~265 MB  →  exceeds 250 MB zip limit, needs container image
```

**Note:** no files in the repository were modified during this analysis. The measurement DuckDB file was written to the session scratchpad.
