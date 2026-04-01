"""
FastAPI web backend for the Louisville Open Data analytics agent.

Serves a chat interface that translates natural language questions into SQL,
executes them against DuckDB, and streams interpreted results via SSE.
"""

import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime

import openai
from fastapi import FastAPI, Request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("app")
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from analytics_agent import (
    build_interpret_prompt,
    execute_sql_safe,
    generate_sql,
    get_last_tier_used,
    interpret_results_stream,
    make_client,
    make_paid_client,
    reason_about_query,
)
from data_model import (
    DATA_DICTIONARY,
    get_data_dictionary_text,
    get_full_schema_description,
    humanize_text,
    load_all_data,
)

# ── Config ───────────────────────────────────────────────────────────────────

DATA_DIR = os.environ.get("DATA_DIR", "data")
MODEL = os.environ.get("MODEL", "gemini-2.5-flash")

RATE_LIMIT_MSG = "Evan was too cheap to use anything other than a free tier and we just hit that free tier's limit. Try again in a few minutes."

# ── State ────────────────────────────────────────────────────────────────────

app = FastAPI(title="Louisville Open Data Explorer")
app.mount("/static", StaticFiles(directory="static"), name="static")

db_lock = threading.Lock()

# ── IP Rate Limiting ─────────────────────────────────────────────────────────

ip_requests: dict[str, list[float]] = {}
IP_RPM_LIMIT = 5  # max requests per minute per IP


def check_ip_rate_limit(ip: str) -> bool:
    """Returns True if IP is within rate limit."""
    now = time.time()
    if ip not in ip_requests:
        ip_requests[ip] = []
    ip_requests[ip] = [t for t in ip_requests[ip] if now - t < 60]
    if len(ip_requests[ip]) >= IP_RPM_LIMIT:
        return False
    ip_requests[ip].append(now)
    return True

# ── Persistent Stats ─────────────────────────────────────────────────────────
# Stats persist to a JSON file in the data directory so they survive restarts.

STATS_DIR = os.environ.get("STATS_DIR", DATA_DIR)
STATS_FILE = os.path.join(STATS_DIR, ".stats.json")

_default_stats = {
    "errors": {
        "total_errors": 0,
        "sql_gen_errors": 0,
        "sql_exec_errors": 0,
        "interpretation_errors": 0,
        "rate_limit_errors": 0,
        "last_error": None,
        "last_error_time": None,
        "errors_last_hour": [],
    },
    "usage": {
        "requests_today": 0,
        "tokens_today": 0,
        "prompt_tokens_today": 0,
        "completion_tokens_today": 0,
        "date": date.today().isoformat(),
    },
    "api_limits": {
        "rpm": None,
        "rpd": None,
        "tpm": None,
        "rpm_remaining": None,
        "rpd_remaining": None,
        "tpm_remaining": None,
    },
}

stats_lock = threading.Lock()


def _load_stats() -> dict:
    """Load stats from disk, or return defaults."""
    try:
        with open(STATS_FILE) as f:
            saved = json.load(f)
        # Reset daily counters if date changed
        if saved.get("usage", {}).get("date") != date.today().isoformat():
            saved["usage"] = dict(_default_stats["usage"])
            saved["errors"]["errors_last_hour"] = []
        return saved
    except (FileNotFoundError, json.JSONDecodeError):
        return json.loads(json.dumps(_default_stats))


def _save_stats():
    """Persist stats to disk. Call after any mutation."""
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(persistent_stats, f)
    except Exception as e:
        log.warning("Failed to save stats: %s", e)


persistent_stats = _load_stats()


# ── Error Tracking ───────────────────────────────────────────────────────────

def track_error(category: str, detail: str = ""):
    """Record an error occurrence."""
    now = time.time()
    with stats_lock:
        errs = persistent_stats["errors"]
        errs["total_errors"] += 1
        errs[f"{category}_errors"] = errs.get(f"{category}_errors", 0) + 1
        errs["last_error"] = f"{category}: {detail}" if detail else category
        errs["last_error_time"] = datetime.now().isoformat()
        errs["errors_last_hour"].append(now)
        errs["errors_last_hour"] = [t for t in errs["errors_last_hour"] if now - t < 3600]
        _save_stats()
    log.warning("Error tracked [%s]: %s", category, detail[:200] if detail else "")


def get_error_summary() -> dict:
    """Return error stats for the health endpoint."""
    now = time.time()
    errs = persistent_stats["errors"]
    recent = [t for t in errs.get("errors_last_hour", []) if now - t < 3600]
    return {
        "total_errors": errs["total_errors"],
        "errors_last_hour": len(recent),
        "sql_gen_errors": errs.get("sql_gen_errors", 0),
        "sql_exec_errors": errs.get("sql_exec_errors", 0),
        "interpretation_errors": errs.get("interpretation_errors", 0),
        "rate_limit_errors": errs.get("rate_limit_errors", 0),
        "last_error": errs.get("last_error"),
        "last_error_time": errs.get("last_error_time"),
    }


# ── Usage Tracking ───────────────────────────────────────────────────────────
# Tracks usage from API response headers (Cerebras provides x-ratelimit-* headers)
# Falls back to local counting if headers aren't available.

def track_usage(prompt_tokens: int = 0, completion_tokens: int = 0):
    """Record an LLM call's token usage."""
    with stats_lock:
        usage = persistent_stats["usage"]
        # Reset if new day
        if usage.get("date") != date.today().isoformat():
            usage["requests_today"] = 0
            usage["tokens_today"] = 0
            usage["prompt_tokens_today"] = 0
            usage["completion_tokens_today"] = 0
            usage["date"] = date.today().isoformat()
        usage["requests_today"] += 1
        usage["prompt_tokens_today"] += prompt_tokens
        usage["completion_tokens_today"] += completion_tokens
        usage["tokens_today"] += prompt_tokens + completion_tokens
        _save_stats()


def update_limits_from_headers(response):
    """Extract rate limit info from API response headers. These are the source of truth."""
    headers = getattr(response, "headers", {}) or {}
    mapping = {
        "rpm": "x-ratelimit-limit-requests-minute",
        "rpd": "x-ratelimit-limit-requests-day",
        "tpd": "x-ratelimit-limit-tokens-day",
        "rpm_remaining": "x-ratelimit-remaining-requests-minute",
        "rpd_remaining": "x-ratelimit-remaining-requests-day",
        "tpd_remaining": "x-ratelimit-remaining-tokens-day",
    }
    with stats_lock:
        for key, header in mapping.items():
            val = headers.get(header)
            if val is not None:
                try:
                    persistent_stats["api_limits"][key] = int(val)
                except ValueError:
                    pass
        _save_stats()


def get_usage_summary() -> dict:
    """Return usage stats. Local counters for requests, API headers for tokens (more accurate)."""
    limits = persistent_stats["api_limits"]
    usage = persistent_stats["usage"]
    rpd = limits.get("rpd") or 14400
    rpm = limits.get("rpm") or 30
    rpm_remaining = limits.get("rpm_remaining")
    tpd = limits.get("tpd") or 1000000
    tpd_remaining = limits.get("tpd_remaining")

    # Requests: use local counter (API header is unreliable for daily totals)
    rpd_used = usage.get("requests_today", 0)
    # RPM: use API header (accurate for current minute)
    rpm_used = (rpm - rpm_remaining) if rpm_remaining is not None else 0
    # Tokens: use API header (accurate daily total from Cerebras)
    tpd_used = (tpd - tpd_remaining) if tpd_remaining is not None else usage.get("tokens_today", 0)
    rpd_pct = round(rpd_used / rpd * 100, 1) if rpd else 0

    return {
        "requests_today": rpd_used,
        "requests_per_minute": rpm_used,
        "tokens_today": tpd_used,
        "limits": {"rpm": rpm, "rpd": rpd, "tpd": tpd},
        "rpd_remaining": max(0, rpd - rpd_used),
        "rpm_remaining": rpm_remaining if rpm_remaining is not None else rpm,
        "rpd_pct": rpd_pct,
        "local_requests_today": usage.get("requests_today", 0),
        "local_tokens_today": usage.get("tokens_today", 0),
        "local_prompt_tokens_today": usage.get("prompt_tokens_today", 0),
        "local_completion_tokens_today": usage.get("completion_tokens_today", 0),
    }


def is_rate_limit_error(e: Exception) -> bool:
    """Check if an exception is a rate limit error (after retries exhausted)."""
    return isinstance(e, openai.RateLimitError) or "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)


# ── Startup ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    global con, schema_desc, sql_system, interpret_system, client, paid_client

    con = load_all_data(DATA_DIR)
    schema_desc = get_full_schema_description(con)

    sql_system = f"""You are a data analytics assistant. You translate natural language questions into SQL queries
and interpret results. You work with Louisville Metro government open data.

## Rules
- Write DuckDB-compatible SQL (similar to PostgreSQL syntax).
- The primary table is `expenditures`. Enrichment tables are: `salary_data`, `capital_projects`, `active_contractors`, `staff_demographics`, `hr_requisitions`, `contractor_profiles`.
- Return ONLY the SQL query, no explanation, no markdown fences, no preamble.
- If a question is ambiguous, make reasonable assumptions.
- IMPORTANT: When a question asks for a single value related to a time period (e.g., "how much did agency X spend?" or "what is the biggest payment?") and does NOT specify "all time" or "total", default to the most recent complete fiscal year (2025). Only use all fiscal years if the question explicitly says "all time", "across all years", "historically", or asks for a trend/comparison. If the intent is genuinely unclear, note in a SQL comment which year you assumed.
- CRITICAL: 2026 is a PARTIAL year across ALL tables (expenditures AND salary_data). It has significantly fewer transactions and lower totals because the year is still in progress. When presenting 2026 data in trends or comparisons, always note that it represents partial-year data. When asked about "current" or "latest" spending OR salaries, use 2025 (the most recent COMPLETE year) unless the user specifically asks about 2026. This applies to BOTH fiscal_year in expenditures AND CalYear in salary_data.
- Use appropriate aggregations, GROUP BY, ORDER BY, and LIMIT clauses.
- ALWAYS include dollar amounts in results when available. If ranking entities, include the monetary value that drives the ranking. Never return a ranked list without the values that determined the ranking.
- For monetary columns, use ROUND() in summaries.
- Date columns may be strings in YYYY-MM-DD format. Use string comparisons or CAST to DATE.
- NULL values are common — use COALESCE or IS NOT NULL where appropriate.
- When joining tables, be aware that agency/department names may differ slightly between tables. Use LIKE or fuzzy matching when needed.
- ALWAYS use `agency_canonical` instead of `agency` when grouping, filtering, or aggregating by agency. The `agency_canonical` column normalizes naming variations (e.g., "Public Works & Assets" and "Public Works & Assets Department" both map to "Public Works & Assets").
- ALWAYS use `payee_canonical` instead of `payee` when grouping, filtering, or aggregating by vendor/contractor. The `payee_canonical` column normalizes abbreviations and variants (e.g., "LG&E" and "LOUISVILLE GAS & ELECTRIC COMPANY" both map to "Louisville Gas & Electric Company", all "CDW GOVT #..." variants map to "CDW LLC"). When searching for a specific vendor, use LIKE on payee_canonical for best matching.

## CRITICAL: Data Quality Awareness
- The extended_amount column contains offsetting entries (positive and negative values that cancel out). This is common in government accounting for corrections, reversals, and adjustments.
- When reporting aggregates (totals, rankings, "largest"), always use SUM(extended_amount) which naturally nets out offsetting entries, NOT individual row values.
- When asked about "largest single payments", use invoice_amount (the actual invoice value) rather than extended_amount, and filter for invoice_amount > 0.
- When ranking payees or agencies by total spend, use SUM(extended_amount) grouped by the entity. Do NOT use MAX() or pick individual rows, as single rows may contain erroneous outlier values that are offset by other rows.
- If a query asks for individual transactions (not aggregates), add WHERE is_data_artifact = FALSE to exclude known erroneous records.
- The `is_offsetting` column flags rows that are part of zero-sum offsetting pairs. The `is_data_artifact` column flags extreme outliers with offsetting counterparts (e.g., $224M SUSTEEN entry that nets to zero). Exclude these when looking for individual large transactions.

## Pre-computed Summary Tables (use these for common questions — they are pre-validated and faster)
- `summary_agency_spend` — total spend by agency (canonical names), transaction count, year range. Use for "which agencies spend the most".
- `summary_annual_spend` — total spend by fiscal year. Use for "how has spending changed over time".
- `summary_largest_payments` — all payments ranked by invoice_amount with payee, agency, date. Use for "largest single payments".
- `summary_top_salaries` — all job titles ranked by avg compensation with employee count. Use for "highest paid positions". NOTE: For salary queries about specific people or titles, query the `salary_data` table directly with CalYear = 2025 (most recent complete year), not summary_top_salaries. The salary_data table has Employee_Name, jobTitle, Department, CalYear, YTD_Total, Annual_Rate, Regular_Rate, Overtime_Rate columns.
- IMPORTANT for salary queries: When asked about a specific role like "Mayor" or "Police Chief", show INDIVIDUAL employee records (Employee_Name, jobTitle, YTD_Total) rather than grouping by jobTitle. Multiple people may share a title (e.g., 6 Deputy Mayors). SUM by jobTitle would be misleading — show each person's individual compensation.
- `summary_expenditure_type` — spending by type (Operating/Capital) per fiscal year. Use for "spending by type".
- `summary_agency_contractors` — agencies ranked by number of licensed contractors used. Use for "which agencies use the most contractors".
- The `capital_projects` table already covers "what capital projects exist" directly.
- `contractor_profiles` — top 200 payees by total spend, enriched with KY Secretary of State data. IMPORTANT: this table only contains the 200 highest-spending vendors. For questions about small vendors, low-spend contractors, or the full universe of payees, query the `expenditures` table directly (GROUP BY payee_canonical).
- `summary_top_contractors` — pre-filtered list of contractors WITH registered agents, ranked by total spend. Use this for "who are the registered agents" or "who runs the top contractors" questions. Already excludes government entities and null agents. Columns: payee, total_spend, sos_registered_agent, sos_officers, sos_company_type, sos_employees.
- Government entities (JEFFERSON COUNTY CLERK, LOUISVILLE METRO AFFORDABLE HOUSING TRUST FUND, FLEETONE, KENTUCKY STATE TREASURER) are NOT contractors — exclude them from contractor queries.
- `summary_grant_funding` — grant/federal funding by fund source with total amounts, transaction counts, and year ranges. Use for "how much grant funding" or "funding sources" questions. Already filtered to grant-related funds.
- Use summary tables for quick overviews and the starter questions. For questions asking about specific entities, full breakdowns, "all" of something, outliers, filtering, or any detailed analysis, query the raw `expenditures` table directly. When computing totals or sums, NEVER limit the query to a subset — include all matching rows unless the user explicitly asks for a top-N.
- When the user asks for a total, sum, or aggregate, do NOT add a LIMIT clause that would exclude data. Only use LIMIT when the user asks for "top N" or the result set would be unreasonably large (>100 rows).

## Data Dictionary: Key Field Definitions
- expenditure_type values: "Operating" / "Metro Government Operations" (day-to-day costs: salaries, supplies, services), "Capital" / "Metro Government Capital" (long-term investments: infrastructure, equipment, construction). The "Metro Government" prefix appears in 2008-2017 data; 2018+ uses shorter names. Treat "Operating" = "Metro Government Operations" and "Capital" = "Metro Government Capital".
- fund: "General Fund" / "1101 General Fund" = primary unrestricted revenue. "Grant Fund" = federal/state/private grants. "Capital Project Fund" = bonds and dedicated capital revenue. "CAP KACA Funding" / "CAA" = Community Action Agency anti-poverty programs. "Pass Thru Federal Other" / "Federally Funded" = federal pass-through money. "Shelter Plus Care" = HUD homeless housing grants. "Municipal Aid" = state road fund. "CARES" funds = COVID-19 relief (2020-2021).
- spend_category: "Grant Utility Assistance" / "Utility Assistance Non-Reportable" = utility bill assistance for residents. "Professional Services" = contracted professional work. "External Agency Contractual Services" = payments to outside organizations. "Grant Community Assistance" / "Grant Emergency Relief" = direct aid programs.

- The `expenditures` table spans FY2008-FY2026. Columns available vary by era:
  - 2008-2017: has sub_agency, department, sub_department, stimulus_type, payment_amount, payment_void_date
  - 2018+: has cost_center, project, program, grant_, financing_source, region
  - Common columns: fiscal_year, invoice_date, invoice_number, invoice_amount, payee, payment_date, payment_number, agency, expenditure_type, expenditure_category, spend_category, fund, extended_amount

## Schema
{schema_desc}
"""

    interpret_system = """You are a data analytics assistant interpreting query results from Louisville Metro government data.
This data covers expenditures from FY2008-FY2026, employee salaries, capital projects, active contractors, staff demographics, and HR requisitions.

## Rules
- Give concise, insightful answers. Lead with the key finding.
- ALWAYS include dollar amounts when the data contains them. Every ranked list must show the value that drives the ranking (e.g., total spend, salary, allocation). Never list entities without their associated numbers.
- If results are empty, explain what that likely means.
- If the data shows something notable or unexpected, call it out.
- Keep responses under 200 words unless the user asked for detail.
- Use semantic column names (e.g., "Agency" not "agency", "Extended Amount" not "extended_amount").
- If the query used a specific fiscal year, mention which year the data covers in your response. If it covers all years, say so.

## Formatting Rules (CRITICAL — follow exactly)
- NEVER use markdown syntax. No bold markers (**), no headers (#), no bullet markers (*), no backticks. Output ONLY plain text.
- For ranked lists, use plain numbered lines with a dash separator, like:
  1. Public Works & Assets Department - $536.7M
  2. Facilities and Fleet Management - $368.8M
- Use dollar formatting consistently (e.g., "$536.7M", "$12,000.00").
- Separate the list from any commentary with a blank line.
- Put caveats or footnotes at the end as a short, plainly written note.
- Use plain line breaks between sections, not headers.
"""

    client = make_client()
    paid_client = make_paid_client()
    if paid_client:
        print(f"Model: {MODEL} (paid tier fallback available)")
    else:
        print(f"Model: {MODEL} (free tier only)")


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html") as f:
        return f.read()


@app.head("/api/health")
@app.get("/api/health")
async def health():
    with db_lock:
        tables = con.execute("SHOW TABLES").fetchall()
        stats = {}
        for (table_name,) in tables:
            count = con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            stats[table_name] = count
    errors = get_error_summary()
    # Status is "degraded" if >5 errors in the last hour, "ok" otherwise
    status = "degraded" if errors["errors_last_hour"] > 5 else "ok"
    return {
        "status": status,
        "tables": stats,
        "model": MODEL,
        "errors": errors,
    }


@app.get("/api/schema")
async def get_schema():
    return {"schema": schema_desc}


@app.get("/api/dictionary")
async def get_dictionary():
    return {"dictionary": get_data_dictionary_text()}


@app.get("/api/usage")
async def get_usage():
    return get_usage_summary()


@app.get("/api/cache")
async def get_cache_status():
    """Show cached questions and whether they have valid responses."""
    status = {}
    for key, events in response_cache.items():
        has_interp = any('"type": "interpretation"' in e for e in events)
        has_error = any('"type": "error"' in e for e in events)
        status[key] = {"events": len(events), "has_interpretation": has_interp, "has_error": has_error}
    return {"cached_questions": len(status), "entries": status}


@app.delete("/api/cache")
async def clear_cache(request: Request):
    """Clear specific or all cached responses. Pass {"question": "..."} to clear one, or no body to clear all."""
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    question = body.get("question", "").strip().lower()
    if question:
        if question in response_cache:
            del response_cache[question]
            _save_cache()
            return {"cleared": question}
        return {"error": "Not in cache"}
    else:
        response_cache.clear()
        _save_cache()
        return {"cleared": "all"}


# ── Response Cache ────────────────────────────────────────────────────────────
# Cache full SSE responses. Persisted to disk so it survives restarts.

CACHE_FILE = os.path.join(os.environ.get("STATS_DIR", os.environ.get("DATA_DIR", "data")), ".response_cache.json")


def _load_cache() -> dict[str, list[str]]:
    """Load cache from disk."""
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache():
    """Persist cache to disk."""
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(response_cache, f)
    except Exception as e:
        log.warning("Failed to save cache: %s", e)


response_cache: dict[str, list[str]] = _load_cache()
log.info("Response cache loaded: %d entries", len(response_cache))


@app.post("/api/ask")
async def ask(request: Request):
    body = await request.json()
    question = body.get("question", "").strip()
    dev_mode = body.get("dev_mode", False)
    history = body.get("history", [])  # list of {"role": "user"|"assistant", "content": "..."}
    if not question:
        return {"error": "No question provided"}

    client_ip = request.client.host if request.client else "unknown"
    if not check_ip_rate_limit(client_ip):
        log.warning("IP rate limited: %s", client_ip)
        return {"error": "Too many requests. Please wait a minute."}

    # Serve from cache if question is cached
    cache_key = question.lower().strip()
    if cache_key in response_cache:
        log.info("Cache hit: %s", question[:50])
        def cached_stream():
            for event in response_cache[cache_key]:
                yield event
        return StreamingResponse(cached_stream(), media_type="text/event-stream")

    # Track whether this response should be cached
    should_cache = not dev_mode and not history

    def event_stream():
        cache_events = []

        def send(event_type: str, data: dict):
            event = f"data: {json.dumps({'type': event_type, **data})}\n\n"
            if should_cache:
                cache_events.append(event)
            return event

        # Collect retry log events to yield inline during streaming
        retry_logs = []
        def on_retry(attempt, max_retries, delay):
            msg = f"Rate limited. Retry {attempt}/{max_retries} in {delay:.0f}s..."
            retry_logs.append(msg)

        def flush_retry_logs():
            """Yield any queued retry log events."""
            events = []
            while retry_logs:
                events.append(send("log", {"content": retry_logs.pop(0)}))
            return events

        # Reasoning step
        log.info("Question: %s", question)
        reasoning = None
        if dev_mode:
            yield send("log", {"content": "Analyzing question..."})
        t_reason_start = time.time()
        try:
            reasoning, reason_usage, reason_raw = reason_about_query(client, MODEL, sql_system, question, on_retry=on_retry if dev_mode else None, history=history, fallback_client=paid_client)
            track_usage(reason_usage.get("prompt_tokens", 0), reason_usage.get("completion_tokens", 0))
            update_limits_from_headers(reason_raw)
            # Extract and strip CHART suggestion from reasoning text
            reasoning_display = "\n".join(
                line for line in reasoning.split("\n")
                if not line.strip().upper().startswith("CHART:")
            ).strip()
            t_reason = time.time() - t_reason_start
            log.info("Reasoning complete in %.1fs (%d tokens)", t_reason, reason_usage.get("total_tokens", 0))
            if dev_mode:
                yield send("reasoning", {"content": reasoning_display})
                yield send("debug", {"content": f"Reasoning in {t_reason:.1f}s | {reason_usage.get('total_tokens', 0)} tokens | Tier: {get_last_tier_used()}"})
        except Exception as e:
            log.warning("Reasoning failed: %s — proceeding without", e)
            if dev_mode:
                yield send("log", {"content": f"Reasoning skipped: {type(e).__name__}"})

        # Check if reasoning determined the question can't be answered with data
        if reasoning and not any(kw in reasoning.lower() for kw in ["query", "table", "select", "join", "filter", "column", "group", "aggregate", "expenditure", "salary", "contractor", "fund"]):
            # Reasoning didn't mention any data concepts — likely an off-topic question
            yield send("interpretation", {"content": reasoning_display})
            yield send("done", {})
            return

        # Generate SQL
        if dev_mode:
            yield send("log", {"content": "Generating SQL query..."})
        t_start = time.time()
        try:
            sql, sql_usage, raw_resp = generate_sql(client, MODEL, sql_system, question, on_retry=on_retry if dev_mode else None, history=history, reasoning=reasoning, fallback_client=paid_client)
            track_usage(sql_usage.get("prompt_tokens", 0), sql_usage.get("completion_tokens", 0))
            update_limits_from_headers(raw_resp)
            log.info("SQL generated in %.1fs (%d tokens)", time.time() - t_start, sql_usage.get("total_tokens", 0))

            # Check if SQL is actually a query or just a comment
            sql_stripped = sql.strip().lstrip("-").strip()
            if not sql_stripped or sql_stripped.startswith("The question") or not any(kw in sql.upper() for kw in ["SELECT", "WITH", "SHOW", "DESCRIBE"]):
                log.info("Model returned non-SQL response, using reasoning as answer")
                if reasoning:
                    yield send("interpretation", {"content": reasoning_display})
                else:
                    yield send("interpretation", {"content": "This question doesn't appear to be answerable from the Louisville Metro expenditure data. Try asking about government spending, agency budgets, contractor payments, employee salaries, or capital projects."})
                yield send("done", {})
                return
        except Exception as e:
            log.error("SQL generation failed: %s", e)
            if is_rate_limit_error(e):
                track_error("rate_limit", "SQL generation")
                if dev_mode:
                    yield send("log", {"content": "Rate limit hit during SQL generation. Retries exhausted."})
                yield send("error", {"content": RATE_LIMIT_MSG})
            else:
                track_error("sql_gen", str(e)[:200])
                if dev_mode:
                    yield send("log", {"content": f"SQL generation error: {type(e).__name__}"})
                yield send("error", {"content": f"SQL generation failed: {e}"})
            return
        t_sql = time.time() - t_start

        if dev_mode:
            for evt in flush_retry_logs():
                yield evt
            yield send("sql", {"content": sql})
            yield send("debug", {"content": f"SQL generated in {t_sql:.1f}s | {sql_usage.get('total_tokens', 0)} tokens | Model: {MODEL} | Tier: {get_last_tier_used()}"})

        # Execute SQL
        if dev_mode:
            yield send("log", {"content": "Executing query against database..."})
        t_start = time.time()
        try:
            with db_lock:
                result_df, result_str = execute_sql_safe(con, sql)
        except Exception as e:
            log.warning("SQL execution failed: %s — retrying", e)
            if dev_mode:
                yield send("log", {"content": f"Query failed: {type(e).__name__}. Asking model to fix..."})
            try:
                fix_prompt = f"The following SQL failed with error: {e}\n\nOriginal SQL:\n{sql}\n\nFix the SQL query. Return ONLY the corrected SQL."
                sql, retry_usage, raw_resp = generate_sql(client, MODEL, sql_system, fix_prompt, on_retry=on_retry if dev_mode else None, history=history, fallback_client=paid_client)
                track_usage(retry_usage.get("prompt_tokens", 0), retry_usage.get("completion_tokens", 0))
                update_limits_from_headers(raw_resp)
                log.info("SQL retry generated")
                if dev_mode:
                    yield send("log", {"content": "Retrying with corrected SQL..."})
                    yield send("sql", {"content": sql})
                with db_lock:
                    result_df, result_str = execute_sql_safe(con, sql)
            except Exception as e2:
                if is_rate_limit_error(e2):
                    track_error("rate_limit", "SQL retry")
                    if dev_mode:
                        yield send("log", {"content": "Rate limit hit during SQL retry."})
                    yield send("error", {"content": RATE_LIMIT_MSG})
                else:
                    track_error("sql_exec", str(e2)[:200])
                    if dev_mode:
                        yield send("log", {"content": f"Retry also failed: {type(e2).__name__}"})
                    yield send("error", {"content": f"Query failed after retry: {e2}"})
                return
        t_exec = time.time() - t_start

        display_str = result_str if dev_mode else humanize_text(result_str)
        yield send("results", {"content": display_str, "row_count": len(result_df)})

        if dev_mode:
            yield send("debug", {"content": f"Query executed in {t_exec:.2f}s | {len(result_df)} rows returned"})

        # Chart visualization
        if len(result_df) >= 2:
            # Parse chart suggestion from reasoning
            chart_type = None
            if reasoning:
                for line in reasoning.split("\n"):
                    if line.strip().upper().startswith("CHART:"):
                        suggested = line.split(":", 1)[1].strip().lower()
                        if suggested in ("bar", "line", "pie"):
                            chart_type = suggested
                        break

            cols = result_df.columns.tolist()
            numeric_cols = [c for c in cols if result_df[c].dtype in ("float64", "int64", "Int64", "float32")]
            # Treat year/date/fiscal columns as labels even if numeric
            label_keywords = ("year", "month", "date", "fiscal", "name", "agency", "payee", "type", "category", "fund")
            label_col = None
            value_col = None
            for c in cols:
                if any(kw in c.lower() for kw in label_keywords) and label_col is None:
                    label_col = c
                elif c in numeric_cols and value_col is None and c != label_col:
                    value_col = c

            # Fall back: first col = labels, second col = values
            if label_col is None and len(cols) >= 2:
                label_col = cols[0]
            if value_col is None:
                for c in numeric_cols:
                    if c != label_col:
                        value_col = c
                        break

            # Auto-detect chart type if reasoning didn't suggest — default to bar
            if chart_type is None and label_col and value_col and len(result_df) <= 50:
                if len(result_df) <= 5:
                    chart_type = "pie"
                else:
                    chart_type = "bar"

            if chart_type and label_col and value_col and len(result_df) >= 2:
                try:
                    labels = result_df[label_col].astype(str).tolist()[:30]
                    values = result_df[value_col].tolist()[:30]
                    title = humanize_text(value_col)
                    label_axis = humanize_text(label_col)
                    yield send("chart", {
                        "chart_type": chart_type,
                        "labels": labels,
                        "values": [float(v) if v == v else 0 for v in values],
                        "title": title,
                        "label_axis": label_axis,
                    })
                except Exception as e:
                    log.warning("Chart generation failed: %s", e)

        # Brief pause to avoid back-to-back RPM hits
        time.sleep(3)

        # Interpret results (streaming)
        if len(result_df) == 0:
            # Ask the model to explain why and suggest alternatives
            empty_prompt = f"""The user asked: "{question}"

The SQL query returned 0 rows:
{sql}

Explain in plain text (no markdown) why this likely returned no results based on what you know about the data structure. Then suggest 1-2 rephrased questions that would likely return results. Keep it under 100 words."""
            try:
                for chunk in interpret_results_stream(
                    client, MODEL, interpret_system, empty_prompt, sql, "No rows returned", history=history, fallback_client=paid_client
                ):
                    yield send("interpretation", {"content": humanize_text(chunk)})
            except Exception:
                yield send("interpretation", {"content": "I wasn't able to find any data matching that question. Try broadening your search or rephrasing."})
            yield send("done", {})
            return

        if dev_mode:
            yield send("log", {"content": "Interpreting results..."})
        t_start = time.time()
        interp_tokens = 0
        stream_timeout = 90  # max seconds for entire interpretation stream
        try:
            for chunk in interpret_results_stream(
                client, MODEL, interpret_system, question, sql, result_str, on_retry=on_retry if dev_mode else None, history=history, fallback_client=paid_client
            ):
                yield send("interpretation", {"content": humanize_text(chunk)})
                interp_tokens += 1
                if time.time() - t_start > stream_timeout:
                    log.warning("Interpretation stream timed out after %ds", stream_timeout)
                    track_error("interpretation", f"Stream timeout after {stream_timeout}s")
                    yield send("interpretation", {"content": "\n\n(Response truncated due to timeout)"})
                    break
        except GeneratorExit:
            log.info("Client disconnected during interpretation stream")
            return
        except Exception as e:
            log.error("Interpretation failed: %s", e)
            if is_rate_limit_error(e):
                track_error("rate_limit", "Interpretation")
                if dev_mode:
                    yield send("log", {"content": "Rate limit hit during interpretation. Retries exhausted."})
                yield send("error", {"content": RATE_LIMIT_MSG})
            else:
                track_error("interpretation", str(e)[:200])
                if dev_mode:
                    yield send("log", {"content": f"Interpretation error: {type(e).__name__}"})
                yield send("interpretation", {"content": f"\n\n(Interpretation error: {e})"})
        track_usage(0, interp_tokens)
        log.info("Request complete — %d chunks streamed", interp_tokens)

        if dev_mode:
            for evt in flush_retry_logs():
                yield evt
            t_interp = time.time() - t_start
            u = get_usage_summary()
            yield send("debug", {"content": f"Interpretation streamed in {t_interp:.1f}s | ~{interp_tokens} chunks | Tier: {get_last_tier_used()}"})
            # Per-request token count (SQL gen tokens + estimated interpretation tokens)
            request_tokens = sql_usage.get("total_tokens", 0) + interp_tokens
            tpd = u["limits"]["tpd"] or 1000000
            tpd_remaining = tpd - u["tokens_today"]
            yield send("usage", {
                "requests_today": u["requests_today"],
                "rpd_remaining": u["rpd_remaining"],
                "rpd_pct": u["rpd_pct"],
                "rpm_used": u["requests_per_minute"],
                "rpm_remaining": u["rpm_remaining"],
                "tokens_today": u["tokens_today"],
                "tokens_remaining": max(0, tpd_remaining),
                "tokens_limit": tpd,
                "request_tokens": request_tokens,
                "local_prompt_tokens_today": u["local_prompt_tokens_today"],
                "local_completion_tokens_today": u["local_completion_tokens_today"],
            })

        yield send("done", {})

        # Cache the response only if it has a valid interpretation (not errors/empty)
        if should_cache and cache_events:
            has_interpretation = any('"type": "interpretation"' in e for e in cache_events)
            has_error = any('"type": "error"' in e for e in cache_events)
            if has_interpretation and not has_error:
                response_cache[cache_key] = cache_events
                _save_cache()
                log.info("Cached response for: %s", question[:50])
            else:
                log.info("Skipped caching (error or empty): %s", question[:50])

    return StreamingResponse(event_stream(), media_type="text/event-stream")
