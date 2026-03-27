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
    interpret_results_stream,
    make_client,
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

# ── Usage Tracking ───────────────────────────────────────────────────────────
# Tracks usage from API response headers (Cerebras provides x-ratelimit-* headers)
# Falls back to local counting if headers aren't available.

usage_stats = {
    "requests_today": 0,
    "tokens_today": 0,
    "prompt_tokens_today": 0,
    "completion_tokens_today": 0,
    # Real limits from API headers (updated on each response)
    "api_limits": {
        "rpm": None,
        "rpd": None,
        "tpm": None,
        "rpm_remaining": None,
        "rpd_remaining": None,
        "tpm_remaining": None,
    },
}


def track_usage(prompt_tokens: int = 0, completion_tokens: int = 0):
    """Record an LLM call's token usage."""
    usage_stats["requests_today"] += 1
    usage_stats["prompt_tokens_today"] += prompt_tokens
    usage_stats["completion_tokens_today"] += completion_tokens
    usage_stats["tokens_today"] += prompt_tokens + completion_tokens


def update_limits_from_headers(response):
    """Extract rate limit info from API response headers if available."""
    headers = getattr(response, "headers", {}) or {}
    mapping = {
        "rpm": "x-ratelimit-limit-requests-minute",
        "rpd": "x-ratelimit-limit-requests-day",
        "tpm": "x-ratelimit-limit-tokens-minute",
        "rpm_remaining": "x-ratelimit-remaining-requests-minute",
        "rpd_remaining": "x-ratelimit-remaining-requests-day",
        "tpm_remaining": "x-ratelimit-remaining-tokens-minute",
    }
    for key, header in mapping.items():
        val = headers.get(header)
        if val is not None:
            try:
                usage_stats["api_limits"][key] = int(val)
            except ValueError:
                pass


def get_usage_summary() -> dict:
    """Return current usage stats and proximity to limits."""
    limits = usage_stats["api_limits"]
    rpd = limits.get("rpd") or 14400
    rpd_remaining = limits.get("rpd_remaining")
    rpm = limits.get("rpm") or 30
    rpm_remaining = limits.get("rpm_remaining")

    # Calculate used from remaining if available, otherwise from local count
    rpd_used = (rpd - rpd_remaining) if rpd_remaining is not None else usage_stats["requests_today"]
    rpm_used = (rpm - rpm_remaining) if rpm_remaining is not None else 0
    rpd_pct = round(rpd_used / rpd * 100, 1) if rpd else 0

    return {
        "requests_today": rpd_used,
        "requests_per_minute": rpm_used,
        "tokens_today": usage_stats["tokens_today"],
        "prompt_tokens_today": usage_stats["prompt_tokens_today"],
        "completion_tokens_today": usage_stats["completion_tokens_today"],
        "limits": {"rpm": rpm, "rpd": rpd, "tpm": limits.get("tpm") or 0},
        "rpd_remaining": rpd_remaining if rpd_remaining is not None else max(0, rpd - usage_stats["requests_today"]),
        "rpm_remaining": rpm_remaining if rpm_remaining is not None else rpm,
        "rpd_pct": rpd_pct,
    }


def is_rate_limit_error(e: Exception) -> bool:
    """Check if an exception is a rate limit error (after retries exhausted)."""
    return isinstance(e, openai.RateLimitError) or "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)


# ── Startup ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    global con, schema_desc, sql_system, interpret_system, client

    con = load_all_data(DATA_DIR)
    schema_desc = get_full_schema_description(con)

    sql_system = f"""You are a data analytics assistant. You translate natural language questions into SQL queries
and interpret results. You work with Louisville Metro government open data.

## Rules
- Write DuckDB-compatible SQL (similar to PostgreSQL syntax).
- The primary table is `expenditures`. Enrichment tables are: `salary_data`, `capital_projects`, `active_contractors`, `staff_demographics`, `hr_requisitions`.
- Return ONLY the SQL query, no explanation, no markdown fences, no preamble.
- If a question is ambiguous, make reasonable assumptions.
- Use appropriate aggregations, GROUP BY, ORDER BY, and LIMIT clauses.
- For monetary columns, use ROUND() in summaries.
- Date columns may be strings in YYYY-MM-DD format. Use string comparisons or CAST to DATE.
- NULL values are common — use COALESCE or IS NOT NULL where appropriate.
- When joining tables, be aware that agency/department names may differ slightly between tables. Use LIKE or fuzzy matching when needed.
- The `expenditures` table spans FY2008-FY2026. Columns available vary by era:
  - 2008-2017: has sub_agency, department, sub_department, stimulus_type, payment_amount, payment_void_date
  - 2018+: has cost_center, project, program, grant_, financing_source, region
  - Common columns: fiscal_year, invoice_date, invoice_number, invoice_amount, payee, payment_date, payment_number, agency, expenditure_type, expenditure_category, spend_category, fund, extended_amount

## Schema
{schema_desc}
"""

    interpret_system = f"""You are a data analytics assistant interpreting query results from Louisville Metro government data.

## Rules
- Give concise, insightful answers. Lead with the key finding.
- Mention specific numbers and comparisons.
- If results are empty, explain what that likely means.
- If the data shows something notable or unexpected, call it out.
- Keep responses under 200 words unless the user asked for detail.
- Use semantic column names in your response (e.g., "Agency" not "agency", "Extended Amount" not "extended_amount").

## Schema context
{schema_desc}
"""

    client = make_client()
    print(f"Model: {MODEL}")


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html") as f:
        return f.read()


@app.get("/api/health")
async def health():
    with db_lock:
        tables = con.execute("SHOW TABLES").fetchall()
        stats = {}
        for (table_name,) in tables:
            count = con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            stats[table_name] = count
    return {
        "status": "ok",
        "tables": stats,
        "model": MODEL,
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


@app.post("/api/ask")
async def ask(request: Request):
    body = await request.json()
    question = body.get("question", "").strip()
    dev_mode = body.get("dev_mode", False)
    if not question:
        return {"error": "No question provided"}

    client_ip = request.client.host if request.client else "unknown"
    if not check_ip_rate_limit(client_ip):
        log.warning("IP rate limited: %s", client_ip)
        return {"error": "Too many requests. Please wait a minute."}

    def event_stream():
        def send(event_type: str, data: dict):
            return f"data: {json.dumps({'type': event_type, **data})}\n\n"

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

        # Generate SQL
        log.info("Question: %s", question)
        if dev_mode:
            yield send("log", {"content": "Generating SQL query..."})
        t_start = time.time()
        try:
            sql, sql_usage, raw_resp = generate_sql(client, MODEL, sql_system, question, on_retry=on_retry if dev_mode else None)
            track_usage(sql_usage.get("prompt_tokens", 0), sql_usage.get("completion_tokens", 0))
            update_limits_from_headers(raw_resp)
            log.info("SQL generated in %.1fs (%d tokens)", time.time() - t_start, sql_usage.get("total_tokens", 0))
        except Exception as e:
            log.error("SQL generation failed: %s", e)
            if is_rate_limit_error(e):
                if dev_mode:
                    yield send("log", {"content": "Rate limit hit during SQL generation. Retries exhausted."})
                yield send("error", {"content": RATE_LIMIT_MSG})
            else:
                if dev_mode:
                    yield send("log", {"content": f"SQL generation error: {type(e).__name__}"})
                yield send("error", {"content": f"SQL generation failed: {e}"})
            return
        t_sql = time.time() - t_start

        if dev_mode:
            for evt in flush_retry_logs():
                yield evt
            yield send("sql", {"content": sql})
            yield send("debug", {"content": f"SQL generated in {t_sql:.1f}s | {sql_usage.get('total_tokens', 0)} tokens | Model: {MODEL}"})

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
                sql, retry_usage, raw_resp = generate_sql(client, MODEL, sql_system, fix_prompt, on_retry=on_retry if dev_mode else None)
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
                    if dev_mode:
                        yield send("log", {"content": "Rate limit hit during SQL retry."})
                    yield send("error", {"content": RATE_LIMIT_MSG})
                else:
                    if dev_mode:
                        yield send("log", {"content": f"Retry also failed: {type(e2).__name__}"})
                    yield send("error", {"content": f"Query failed after retry: {e2}"})
                return
        t_exec = time.time() - t_start

        display_str = result_str if dev_mode else humanize_text(result_str)
        yield send("results", {"content": display_str, "row_count": len(result_df)})

        if dev_mode:
            yield send("debug", {"content": f"Query executed in {t_exec:.2f}s | {len(result_df)} rows returned"})

        # Brief pause to avoid back-to-back RPM hits on Gemini free tier
        time.sleep(3)

        # Interpret results (streaming)
        if len(result_df) == 0:
            yield send("interpretation", {"content": "No results found for this query."})
            yield send("done", {})
            return

        if dev_mode:
            yield send("log", {"content": "Interpreting results..."})
        t_start = time.time()
        interp_tokens = 0
        try:
            for chunk in interpret_results_stream(
                client, MODEL, interpret_system, question, sql, result_str, on_retry=on_retry if dev_mode else None
            ):
                yield send("interpretation", {"content": chunk})
                interp_tokens += 1
        except Exception as e:
            log.error("Interpretation failed: %s", e)
            if is_rate_limit_error(e):
                if dev_mode:
                    yield send("log", {"content": "Rate limit hit during interpretation. Retries exhausted."})
                yield send("error", {"content": RATE_LIMIT_MSG})
            else:
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
            yield send("debug", {"content": f"Interpretation streamed in {t_interp:.1f}s | ~{interp_tokens} chunks"})
            yield send("usage", {
                "requests_today": u["requests_today"],
                "rpd_remaining": u["rpd_remaining"],
                "rpd_pct": u["rpd_pct"],
                "rpm_used": u["requests_per_minute"],
                "rpm_remaining": u["rpm_remaining"],
                "tokens_today": u["tokens_today"],
                "prompt_tokens": u["prompt_tokens_today"],
                "completion_tokens": u["completion_tokens_today"],
            })

        yield send("done", {})

    return StreamingResponse(event_stream(), media_type="text/event-stream")
