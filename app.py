"""
FastAPI web backend for the Louisville Open Data analytics agent.

Serves a chat interface that translates natural language questions into SQL,
executes them against DuckDB, and streams interpreted results via SSE.
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime

import openai
from fastapi import FastAPI, Request

LOG_DIR = os.environ.get("LOG_DIR", "/logs")
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Console handler
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATEFMT,
)

# File handler — persistent logs that survive container recreations
try:
    os.makedirs(LOG_DIR, exist_ok=True)
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "louisville-bot.log"),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
    file_handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(file_handler)
except Exception as e:
    print(f"Warning: could not set up file logging: {e}")

log = logging.getLogger("app")
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from analytics_agent import (
    execute_sql_safe,
    generate_sql,
    get_active_model,
    get_last_tier_used,
    get_model_fallback_event,
    interpret_results_stream,
    make_client,
    make_paid_client,
    refine_events_with_fallback,
    refine_interpretation_stream,
    REFINE_SYSTEM_PROMPT,
)
from data_model import (
    CONFIG,
    DATA_DICTIONARY,
    drop_total_rows,
    get_compact_schema_description,
    get_data_dictionary_text,
    get_full_schema_description,
    humanize_text,
    infer_chart,
    load_all_data,
    year_context,
)

# ── Config ───────────────────────────────────────────────────────────────────

DATA_DIR = os.environ.get("DATA_DIR", "data")
MODEL = os.environ.get("MODEL", "gpt-oss-120b")  # Cerebras model; override via MODEL env

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


def is_service_error(e: Exception) -> bool:
    """LLM service/config failure that the user cannot fix by rewording.

    Examples: the configured model was removed or is inaccessible (404
    model_not_found), bad/expired API key (401/403), the provider is down or
    unreachable (5xx / connection / timeout). These are problems on our end, so
    we should say so plainly rather than blame the question or imply a rate limit.
    """
    if isinstance(e, (
        openai.AuthenticationError, openai.PermissionDeniedError,
        openai.NotFoundError, openai.APIConnectionError,
        openai.APITimeoutError, openai.InternalServerError,
    )):
        return True
    # String fallback ONLY for LLM-specific phrasings. Deliberately narrow: the
    # SQL-retry path can hand us DuckDB errors like 'Table "x" does not exist',
    # which must NOT be treated as a service error (they're bad-query errors the
    # user can rephrase). The openai exception types above already catch real
    # LLM 404/auth failures; this just adds the unambiguous Cerebras wording.
    s = str(e).lower()
    return "model_not_found" in s or "does not exist or you do not have access" in s


# Shown for is_service_error cases: honest about it being our problem, no futile "reword".
SERVICE_ERROR_MSG = (
    "Lou is having trouble reaching its language model right now. This is a problem on "
    "our end, not your question, so please try again in a little while."
)


# ── Startup ──────────────────────────────────────────────────────────────────

def _salary_status(yc: dict) -> str:
    """One operator-facing line describing what salary guidance was derived.

    Driven by year_context's single `salary_state` value so the six cases
    stay exclusive (they were previously a nested ternary over flags that
    could both be true).
    """
    state = yc.get("salary_state")
    if state == "error":
        return "derivation failed — see warning above"
    if state == "no_table":
        return "no salary table"
    if state == "no_years":
        return "salary_data has no usable CalYear values"
    if state == "single_year":
        return f"CalYear {yc['newest_cal_year']} only; no complete year to cite"
    if state == "ok":
        return f"CalYear partial, latest complete {yc['salary']['last_complete_year']}"
    if state == "unknown":
        return "not evaluated"
    # Drift between data_model's enum and this helper must be visible in the
    # log, not disguised as the legitimate "unknown" case.
    return f"not evaluated (unrecognized salary_state {state!r})"


@app.on_event("startup")
def startup():
    global con, schema_desc, sql_system, interpret_system, client, paid_client

    con = load_all_data(DATA_DIR)
    # Compact schema for the system prompt (sent on every LLM call, so token
    # size matters); the full verbose version is still available at /api/schema.
    schema_desc = get_compact_schema_description(con)

    # Year facts are derived from the loaded data, never hardcoded — and
    # "partial" is decided by DATA COVERAGE, not by assuming the newest year
    # is unfinished: we compare how far payments actually run against that
    # fiscal year's end date. A refresh that completes the newest year
    # therefore promotes it to "complete" instead of leaving the prompt
    # asserting a stale falsehood.
    yc = year_context(con, (CONFIG.city or {}).get("fiscal_year_start_month", 1))
    if not yc["values"]:
        # The prompts are built around fiscal years; without any there is
        # nothing to serve, so fail loudly here rather than as a KeyError.
        raise RuntimeError(
            f"No usable fiscal_year values in the expenditures table loaded from {DATA_DIR!r} — "
            "check the data files and the city pack's expenditure sources."
        )
    year_rules = yc["rules"]
    # Only point at the CalYear rule when one was actually derived, so the
    # prompt never references guidance that isn't in it.
    salary_year_clause = (
        f", filtered to CalYear = {yc['salary']['last_complete_year']} (see the CalYear rule above)"
        if yc["salary"] else ""
    )
    years = yc["values"]
    first_year = years["first_year"]
    newest_year = years["newest_year"]
    last_complete_year = years["last_complete_year"]
    log.info(
        "Year coverage: FY%s %s (through %s); latest complete FY = %s; salary: %s",
        newest_year,
        "PARTIAL" if yc["expenditures"]["is_partial"] else "complete",
        yc["expenditures"]["covered_through"],
        last_complete_year,
        _salary_status(yc),
    )

    sql_system = f"""You are a data analytics assistant. You translate natural language questions into SQL queries
and interpret results. You work with Louisville Metro government open data.

## Rules
- Write DuckDB-compatible SQL (similar to PostgreSQL syntax).
- The primary table is `expenditures`. Enrichment tables are: `salary_data`, `capital_projects`, `active_contractors`, `staff_demographics`, `hr_requisitions`, `contractor_profiles`.
- Return ONLY the SQL query, no explanation, no markdown fences, no preamble.
- If a question is ambiguous, make reasonable assumptions.
- IMPORTANT: When a question asks for a single value related to a time period (e.g., "how much did agency X spend?" or "what is the biggest payment?") and does NOT specify "all time" or "total", default to the most recent year with complete data ({last_complete_year}). Only use all fiscal years if the question explicitly says "all time", "across all years", "historically", or asks for a trend/comparison. If the intent is genuinely unclear, note in a SQL comment which year you assumed.
{year_rules}
- Use appropriate aggregations, GROUP BY, ORDER BY, and LIMIT clauses.
- When a question asks about quantitative values (spend, cost, salary, amount), always include the relevant numbers in the SELECT. If ranking entities by a numeric value, include that value in the results. Not every query needs dollar amounts — only include them when relevant to the question.
- ALWAYS filter out NULL values from display columns. Use WHERE column IS NOT NULL or COALESCE(column, 'N/A'). Never return rows with blank or null values in key fields — they confuse users.
- CONSISTENCY: When answering follow-up questions, use the same tables and groupings as the original query. If the original used payee_canonical, the follow-up must too. If the original used summary_top_contractors, reference it consistently.
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
- NEVER invent filters the user did not ask for: no fiscal-year filter, no agency filter, no fund filter unless the question names them explicitly OR a summary-table bullet below directs a specific filter for that question type. Questions like "how much X has Louisville received/spent" mean ALL years and ALL agencies — "Louisville" is the whole government, never an agency value to filter on.
- The `is_offsetting` column flags rows that are part of zero-sum offsetting pairs. The `is_data_artifact` column flags extreme outliers with offsetting counterparts (e.g., $224M SUSTEEN entry that nets to zero). Exclude these when looking for individual large transactions.

## Pre-computed Summary Tables (use these for common questions — they are pre-validated and faster)
- `summary_agency_spend` — total spend by agency (canonical names), transaction count, year range. Use for "which agencies spend the most".
- `summary_annual_spend` — total spend by fiscal year. Use for "how has spending changed over time".
- `summary_largest_payments` — all payments ranked by invoice_amount with payee, agency, date. Use for "largest single payments".
- `summary_top_salaries` — (job_title, department) groups for a single calendar year (see its calendar_year column), ranked by avg total pay, with DISTINCT-employee counts. The same job title can appear in several departments — select department alongside job_title. Use for "highest paid positions". NOTE: For salary queries about specific people or titles, query the `salary_data` table directly{salary_year_clause}. The salary_data table has Employee_Name, jobTitle, Department, CalYear, YTD_Total, Annual_Rate, Regular_Rate, Overtime_Rate, Incentive_Allowance, Other columns.
- IMPORTANT for salary queries: When asked about a specific role like "Mayor" or "Police Chief", show INDIVIDUAL employee records (Employee_Name, jobTitle, YTD_Total) rather than grouping by jobTitle. Multiple people may share a title (e.g., 6 Deputy Mayors). SUM by jobTitle would be misleading — show each person's individual compensation.
- Follow-up questions about a previous answer ("is that true?", "are you sure?", "what does that include?", "can you verify that?") ARE answerable — never treat them as off-topic. Write SQL that verifies or decomposes the earlier claim using the conversation history. Example: to check what a compensation total includes, SELECT Employee_Name, Annual_Rate, Regular_Rate, Overtime_Rate, Incentive_Allowance, Other, YTD_Total FROM salary_data for the relevant people/year — the components show exactly what the total is made of (pay only; the data contains no benefits figures).
- `summary_expenditure_type` — spending by type (Operating/Capital) per fiscal year. Use for "spending by type".
- `summary_agency_contractors` — agencies ranked by number of licensed contractors used. Use for "which agencies use the most contractors".
- The `capital_projects` table already covers "what capital projects exist" directly.
- `contractor_profiles` — top 200 payees by total spend, enriched with KY Secretary of State data. IMPORTANT: this table only contains the 200 highest-spending vendors. For questions about small vendors, low-spend contractors, or the full universe of payees, query the `expenditures` table directly (GROUP BY payee_canonical).
- `summary_top_contractors` — pre-filtered list of contractors WITH registered agents, ranked by total spend. Use this for "who are the registered agents" or "who runs the top contractors" questions. Already excludes government entities and null agents. ALWAYS SELECT payee, total_spend, AND sos_registered_agent together — never omit total_spend. Example: SELECT payee, total_spend, sos_registered_agent FROM summary_top_contractors ORDER BY total_spend DESC LIMIT 10.
- Government entities (JEFFERSON COUNTY CLERK, LOUISVILLE METRO AFFORDABLE HOUSING TRUST FUND, FLEETONE, KENTUCKY STATE TREASURER) are NOT contractors — exclude them from contractor queries.
- `summary_grant_funding` — grant/federal funding by fund source with total amounts, transaction counts, and year ranges. For grant-funding TOTALS and source lists ("how much grant funding", "from which sources"), use EXACTLY this query with no additions, no WHERE clause, and no other table: SELECT COALESCE(fund, 'TOTAL - ALL GRANT FUNDS') AS fund, ROUND(SUM(total_amount), 2) AS total_amount FROM summary_grant_funding GROUP BY ROLLUP(fund) ORDER BY total_amount DESC NULLS LAST. It returns the grand total row plus every source. For BREAKDOWNS within grant money (by agency, payee, or year — e.g. "which agencies received CARES money"), query `expenditures` filtered on the SPECIFIC fund names (fund = 'CARES Coronavirus Relief Fund (CRF)', fund LIKE 'CDBG%', etc. — the fund values in summary_grant_funding). Never approximate grant money with fund LIKE '%grant%' — that misses federal, CARES, CDBG, HOME, stimulus and other grant funds that don't contain the word "grant".
- Use summary tables for quick overviews and the starter questions. For questions asking about specific entities, full breakdowns, "all" of something, outliers, filtering, or any detailed analysis, query the raw `expenditures` table directly. When computing totals or sums, NEVER limit the query to a subset — include all matching rows unless the user explicitly asks for a top-N.
- When the user asks for a total, sum, or aggregate, do NOT add a LIMIT clause that would exclude data. Only use LIMIT when the user asks for "top N" or the result set would be unreasonably large (>100 rows).

## Data Dictionary: Key Field Definitions
- expenditure_type values: "Operating" / "Metro Government Operations" (day-to-day costs: salaries, supplies, services), "Capital" / "Metro Government Capital" (long-term investments: infrastructure, equipment, construction). The "Metro Government" prefix appears in 2008-2017 data; 2018+ uses shorter names. Treat "Operating" = "Metro Government Operations" and "Capital" = "Metro Government Capital".
- fund: "General Fund" / "1101 General Fund" = primary unrestricted revenue. "Grant Fund" = federal/state/private grants. "Capital Project Fund" = bonds and dedicated capital revenue. "CAP KACA Funding" / "CAA" = Community Action Agency anti-poverty programs. "Pass Thru Federal Other" / "Federally Funded" = federal pass-through money. "Shelter Plus Care" = HUD homeless housing grants. "Municipal Aid" = state road fund. "CARES" funds = COVID-19 relief (2020-2021).
- spend_category: "Grant Utility Assistance" / "Utility Assistance Non-Reportable" = utility bill assistance for residents. "Professional Services" = contracted professional work. "External Agency Contractual Services" = payments to outside organizations. "Grant Community Assistance" / "Grant Emergency Relief" = direct aid programs.

## Topic Vocabulary (the words users say vs. the values in the data)
Users ask in everyday terms that rarely appear verbatim in the data. Filter only on values named in this prompt, or on a broad pattern (ILIKE '%word%') likely to match several real values — a narrow guess at an exact value you have not seen usually matches nothing. Prefer agency_canonical when the topic is a department's remit.
- technology / IT / computers / software / cybersecurity: the department is agency_canonical = 'Metro Technology Services'; the categories are named for the thing bought, not the topic (Computer Software, Computer Hardware & Equipment, Computer Software License Owned, Software Maintenance, Enterprise Software Licenses (MELA), Cloud Computing Services). NOT every software category is Computer-prefixed, so match both patterns. Department and category are two OVERLAPPING views: ANDing them collapses to a small intersection and understates the answer ~5x. Use EXACTLY this query shape, substituting the year asked about, and DROPPING both fiscal_year predicates when the question covers all time / all years / a trend: SELECT 'Metro Technology Services department' AS spend_view, ROUND(SUM(extended_amount), 2) AS total_spend FROM expenditures WHERE fiscal_year = {last_complete_year} AND agency_canonical = 'Metro Technology Services' AND is_data_artifact = FALSE UNION ALL SELECT 'Computer, software and cloud purchases (all departments)', ROUND(SUM(extended_amount), 2) FROM expenditures WHERE fiscal_year = {last_complete_year} AND (spend_category LIKE 'Computer%' OR spend_category ILIKE '%Software%' OR spend_category = 'Cloud Computing Services') AND is_data_artifact = FALSE. The two returned figures OVERLAP (a software purchase by that department appears in both), so present them as two separate views and NEVER add them into a combined total. The words "technology" and "cybersecurity" appear in NO spend_category value; filtering on them returns zero rows.
- police / law enforcement: agency_canonical = 'Louisville Metro Police Department'. fire: 'Louisville Fire'. parks: 'Parks & Recreation'. roads/paving/infrastructure: 'Public Works & Assets'.
- An empty result from a topical filter means the filter was wrong, not that the city spends nothing. Re-query using the department (agency_canonical) or a broader category pattern before drawing any conclusion about spending levels.

- The `expenditures` table spans FY{first_year}-FY{newest_year}. Columns available vary by era:
  - 2008-2017: has sub_agency, department, sub_department, stimulus_type, payment_amount, payment_void_date
  - 2018+: has cost_center, project, program, grant_, financing_source, region
  - Common columns: fiscal_year, invoice_date, invoice_number, invoice_amount, payee, payment_date, payment_number, agency, expenditure_type, expenditure_category, spend_category, fund, extended_amount

## Schema
{schema_desc}
"""

    interpret_system = f"""You are a data analytics assistant interpreting query results from Louisville Metro government data.
This data covers expenditures from FY{first_year}-FY{newest_year}, employee salaries, capital projects, active contractors, staff demographics, and HR requisitions.

## Rules
- Give concise, insightful answers. Lead with the key finding.
- When the question asks about quantitative values or when entities are ranked by a numeric metric, include those numbers in the response. Not every answer needs dollar amounts — only include them when they're relevant to what was asked.
- Never add together rows that are different VIEWS of the same spending (e.g. a department total and a category total, where one purchase can appear in both). Report such figures separately; summing them double-counts. Only add rows that are mutually exclusive slices.
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

## Accuracy Rules (CRITICAL)
- NEVER rescale numbers: repeat values at the magnitude shown in the results (a value like 192,770.57 is about $192.8K, not millions).
- Only state facts that appear in the results or the question. Do not describe what a figure includes or what years a dataset covers unless the results show it.
"""
    # Pack facts (placeholders resolved by the pack itself) plus the
    # data-derived year fact computed above.
    global CITY_FACTS
    CITY_FACTS = CONFIG.data_facts_for(years) + yc["facts"]
    if CITY_FACTS:
        interpret_system += "\n## Facts about this city's data (enforce these)\n" + \
            "\n".join(f"- {f}" for f in CITY_FACTS) + "\n"

    # Cache entries are keyed with a version derived from the prompts, so any
    # prompt change automatically invalidates stale cached answers (re-warm
    # the starter questions after deploys that change prompts).
    global CACHE_VERSION
    CACHE_VERSION = hashlib.sha1(
        (sql_system + interpret_system + REFINE_SYSTEM_PROMPT + json.dumps(CITY_FACTS)).encode()
    ).hexdigest()[:8]
    stale = [k for k in response_cache if not k.startswith(CACHE_VERSION + ":")]
    if stale:
        for k in stale:
            del response_cache[k]
        _save_cache()
        log.info("Pruned %d cache entries from older prompt versions", len(stale))

    client = make_client()
    paid_client = make_paid_client()
    if paid_client:
        log.info("Model: %s (paid tier fallback available)", MODEL)
    else:
        log.info("Model: %s (free tier only)", MODEL)
    log.info("Logs writing to: %s", LOG_DIR)


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
        # "model" is the model actually in use; if a provider deprecation
        # triggered a runtime fallback, model != model_configured and
        # model_fallback carries the {from, to, time} of the switch.
        "model": get_active_model(MODEL),
        "model_configured": MODEL,
        "model_fallback": get_model_fallback_event(),
        "errors": errors,
    }


@app.get("/api/config")
async def get_config():
    """Frontend branding from the active city pack, so the UI carries no
    hardcoded city identity. A pack that omits fields gets neutral defaults
    derived from its own city name — never another city's bot name."""
    b = dict(CONFIG.branding or {})
    city = ((CONFIG.city or {}).get("name") or "").strip()
    who = city or "this city"
    # Every key the frontend reads gets a value, so a pack without a branding
    # section renders ITS OWN neutral copy rather than inheriting whatever the
    # page happened to ship with.
    b.setdefault("bot_name", city or "Open Data Bot")
    b.setdefault("tab_title", CONFIG.title)
    # Suppress the whole sentence when there is no city name rather than
    # emitting a dangling "The publicly shared data from".
    b.setdefault("subtitle", f"The publicly shared data from {city}" if city else "")
    b.setdefault("hero_heading", f"Ask me about {who}'s public spending data.")
    b.setdefault("hero_blurb", f"Natural language queries run against {who}'s published open data.")
    b.setdefault("input_placeholder", f"Enter your question about {who}'s data here...")
    b.setdefault("input_aria_label", f"Ask a question about {who}'s data")
    # Default to the same neutral copy the markup ships: an empty value would
    # wipe the placeholder and leave the About affordance an empty box, losing
    # the as-is/not-affiliated disclaimer that is the reason it exists.
    b.setdefault("about_html", (
        "<strong>About this data</strong><br>"
        f"Sourced from {who}'s public open data portal. Data is provided as-is "
        "without warranty. This tool is an independent project and is not "
        "affiliated with or endorsed by the city."
    ))
    b.setdefault("starter_groups", [])
    return b


@app.get("/api/schema")
async def get_schema():
    # The prompt uses the compact schema; expose both here for debugging.
    with db_lock:
        full = get_full_schema_description(con)
    return {"schema": schema_desc, "schema_full": full}


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
    # cache_version lets tooling tell a current entry from a stale one — keys
    # are "<version>:<question>" and only current-version entries are served.
    return {"cached_questions": len(status), "cache_version": CACHE_VERSION, "entries": status}


@app.delete("/api/cache")
async def clear_cache(request: Request):
    """Clear specific or all cached responses. Pass {"question": "..."} to clear one, or no body to clear all."""
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    question = body.get("question", "").strip().lower()
    if question:
        key = _cache_key(question)
        if key in response_cache:
            del response_cache[key]
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

# Set at startup from a hash of the assembled prompts: a prompt edit changes
# the version, orphaning (and pruning) every previously cached answer, so a
# fix can never be shadowed by a stale cache entry.
CACHE_VERSION = "unversioned"

# City data facts with year placeholders resolved (set at startup).
CITY_FACTS: list[str] = []


def _cache_key(question: str) -> str:
    return f"{CACHE_VERSION}:{question.lower().strip()}"


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


def _sse_message(event_type: str, content: str) -> StreamingResponse:
    """Return a one-shot SSE stream carrying a single event + done.

    Used for early-exit cases (bad input, rate limit) so the client always
    receives a parseable SSE event instead of a plain-JSON body it can't
    render — a plain-JSON error leaves the UI spinning forever.
    """
    def gen():
        yield f"data: {json.dumps({'type': event_type, 'content': content})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/ask")
async def ask(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _sse_message("error", "Invalid request. Please refresh the page and try again.")
    question = body.get("question", "").strip()
    dev_mode = body.get("dev_mode", False)
    history = body.get("history", [])  # list of {"role": "user"|"assistant", "content": "..."}
    if not question:
        return _sse_message("error", "Please enter a question.")

    client_ip = request.client.host if request.client else "unknown"
    if not check_ip_rate_limit(client_ip):
        log.warning("IP rate limited: %s", client_ip)
        return _sse_message("error", "That's a lot of questions in a short time. Please wait a few seconds, then ask again.")

    # Serve from cache if question is cached
    cache_key = _cache_key(question)
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

        # The separate "reason" LLM call was removed: it re-sent the full system
        # prompt (~11K tokens) just to produce a small chart hint, roughly 45% of
        # per-question tokens. Chart type is now inferred from the result shape
        # (see the chart block); off-topic questions are caught by the non-SQL
        # check right after generation.
        log.info("Question: %s", question)
        reasoning = None

        # Generate SQL
        yield send("log", {"content": "Generating SQL query..."})
        yield send("status", {"content": "Writing the query…"})
        t_start = time.time()
        try:
            sql, sql_usage, raw_resp = generate_sql(client, MODEL, sql_system, question, on_retry=on_retry, history=history, reasoning=reasoning, fallback_client=paid_client)
            track_usage(sql_usage.get("prompt_tokens", 0), sql_usage.get("completion_tokens", 0))
            update_limits_from_headers(raw_resp)
            log.info("SQL generated in %.1fs (%d tokens)", time.time() - t_start, sql_usage.get("total_tokens", 0))

            # Check if SQL is actually a query or just a comment (off-topic guard)
            sql_stripped = sql.strip().lstrip("-").strip()
            if not sql_stripped or sql_stripped.startswith("The question") or not any(kw in sql.upper() for kw in ["SELECT", "WITH", "SHOW", "DESCRIBE"]):
                log.info("Model returned non-SQL response (likely off-topic)")
                yield send("interpretation", {"content": "This question doesn't appear to be answerable from the Louisville Metro expenditure data. Try asking about government spending, agency budgets, contractor payments, employee salaries, or capital projects."})
                yield send("done", {})
                return
        except Exception as e:
            log.error("SQL generation failed: %s", e)
            if is_rate_limit_error(e):
                track_error("rate_limit", "SQL generation")
                yield send("log", {"content": "Rate limit hit during SQL generation. Retries exhausted."})
                yield send("error", {"content": RATE_LIMIT_MSG})
            elif is_service_error(e):
                track_error("service", str(e)[:200])
                yield send("log", {"content": f"Service error: {type(e).__name__}"})
                yield send("debug", {"content": f"LLM service error detail: {e}"})
                yield send("error", {"content": SERVICE_ERROR_MSG})
            else:
                track_error("sql_gen", str(e)[:200])
                yield send("log", {"content": f"SQL generation error: {type(e).__name__}"})
                yield send("debug", {"content": f"SQL gen error detail: {e}"})
                yield send("error", {"content": "I couldn't turn that into a query. Try rewording it, or ask about spending, salaries, contractors, or capital projects."})
            return
        t_sql = time.time() - t_start

        for evt in flush_retry_logs():
            yield evt
        yield send("sql", {"content": sql})
        yield send("debug", {"content": f"SQL generated in {t_sql:.1f}s | {sql_usage.get('total_tokens', 0)} tokens | Model: {get_active_model(MODEL)} | Tier: {get_last_tier_used()}"})

        # Execute SQL
        yield send("log", {"content": "Executing query against database..."})
        yield send("status", {"content": "Querying the data…"})
        t_start = time.time()
        try:
            with db_lock:
                result_df, result_str = execute_sql_safe(con, sql)
        except Exception as e:
            log.warning("SQL execution failed: %s — retrying", e)
            yield send("log", {"content": f"Query failed: {type(e).__name__}. Asking model to fix..."})
            try:
                fix_prompt = f"The following SQL failed with error: {e}\n\nOriginal SQL:\n{sql}\n\nFix the SQL query. Return ONLY the corrected SQL."
                sql, retry_usage, raw_resp = generate_sql(client, MODEL, sql_system, fix_prompt, on_retry=on_retry, history=history, fallback_client=paid_client)
                track_usage(retry_usage.get("prompt_tokens", 0), retry_usage.get("completion_tokens", 0))
                update_limits_from_headers(raw_resp)
                log.info("SQL retry generated")
                yield send("log", {"content": "Retrying with corrected SQL..."})
                yield send("sql", {"content": sql})
                with db_lock:
                    result_df, result_str = execute_sql_safe(con, sql)
            except Exception as e2:
                if is_rate_limit_error(e2):
                    track_error("rate_limit", "SQL retry")
                    yield send("log", {"content": "Rate limit hit during SQL retry."})
                    yield send("error", {"content": RATE_LIMIT_MSG})
                elif is_service_error(e2):
                    track_error("service", str(e2)[:200])
                    yield send("log", {"content": f"Service error during retry: {type(e2).__name__}"})
                    yield send("debug", {"content": f"LLM service error detail: {e2}"})
                    yield send("error", {"content": SERVICE_ERROR_MSG})
                else:
                    track_error("sql_exec", str(e2)[:200])
                    yield send("log", {"content": f"Retry also failed: {type(e2).__name__}"})
                    yield send("debug", {"content": f"SQL exec error detail: {e2}"})
                    yield send("error", {"content": "That query couldn't be run against the data, even after a retry. Try simplifying or rephrasing your question."})
                return
        t_exec = time.time() - t_start

        display_str = result_str if dev_mode else humanize_text(result_str)
        yield send("results", {"content": display_str, "row_count": len(result_df), "humanized": not dev_mode})

        yield send("debug", {"content": f"Query executed in {t_exec:.2f}s | {len(result_df)} rows returned"})

        # Chart visualization
        if len(result_df) >= 2:
            # Axis/type inference extracted to a pure, unit-tested helper
            # (see data_model.infer_chart and tests/test_known_answers.py).
            chart_type, label_col, value_col = infer_chart(result_df)

            if chart_type and label_col and value_col and len(result_df) >= 2:
                try:
                    # A line chart implies a time axis, but the query may be
                    # ordered by value (e.g. "top 5 years by spend" -> amount DESC).
                    # Sort by the time/label column so the line reads chronologically
                    # instead of zig-zagging in rank order.
                    chart_df = result_df.sort_values(label_col) if chart_type == "line" else result_df
                    # Grand-total rows would double the axis scale and dwarf the
                    # real bars (see drop_total_rows: label shapes + a value check
                    # that spares real payees like TOTAL TOOL SUPPLY INC).
                    chart_df = drop_total_rows(chart_df, label_col, value_col)
                    if len(chart_df) < 2:
                        raise ValueError("too few chartable rows after dropping total rows")
                    labels = chart_df[label_col].astype(str).tolist()[:30]
                    values = chart_df[value_col].tolist()[:30]
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

        yield send("log", {"content": "Interpreting results..."})
        yield send("status", {"content": "Summarizing the results…"})
        t_start = time.time()
        interp_tokens = 0
        stream_timeout = 90  # max seconds per LLM stream
        # The draft interpretation accumulates server-side; the user-visible
        # stream is the refinement pass below (plain language, consistent
        # formatting, numbers checked against the results table). Periodic
        # keepalive frames flow during accumulation so the client's stall
        # watchdog keeps resetting and disconnects remain detectable.
        draft = ""
        draft_truncated = False
        draft_error = None
        last_beat = time.time()
        try:
            for chunk in interpret_results_stream(
                client, MODEL, interpret_system, question, sql, result_str, on_retry=on_retry, history=history, fallback_client=paid_client
            ):
                draft += chunk
                interp_tokens += 1
                now = time.time()
                if now - last_beat > 8:
                    last_beat = now
                    for evt in flush_retry_logs():
                        yield evt
                    yield send("status", {"content": "Summarizing the results…"})
                if now - t_start > stream_timeout:
                    log.warning("Interpretation stream timed out after %ds", stream_timeout)
                    track_error("interpretation", f"Stream timeout after {stream_timeout}s")
                    draft_truncated = True
                    break
        except GeneratorExit:
            log.info("Client disconnected during interpretation stream")
            return
        except Exception as e:
            draft_error = e
            log.error("Interpretation failed: %s", e)
            if is_rate_limit_error(e):
                track_error("rate_limit", "Interpretation")
                yield send("log", {"content": "Rate limit hit during interpretation. Retries exhausted."})
                yield send("error", {"content": RATE_LIMIT_MSG})
            elif is_service_error(e):
                track_error("service", str(e)[:200])
                yield send("log", {"content": f"Service error during interpretation: {type(e).__name__}"})
                yield send("debug", {"content": f"LLM service error detail: {e}"})
                yield send("error", {"content": SERVICE_ERROR_MSG})
            else:
                track_error("interpretation", str(e)[:200])
                yield send("log", {"content": f"Interpretation error: {type(e).__name__}"})
                yield send("debug", {"content": f"Interpretation error detail: {e}"})
                yield send("interpretation", {"content": "\n\n(I ran the query but had trouble summarizing the results. The data above is still accurate.)"})
        t_draft = time.time() - t_start

        if draft_error is not None:
            # The user already saw the error (or apology). Never refine a
            # partial draft into a complete-looking answer on top of it — and
            # never re-hit an already-exhausted API 2s later. Tokens streamed
            # before the failure were still consumed — account for them.
            track_usage(0, interp_tokens)
            yield send("done", {})
            return

        if draft and draft_truncated:
            # A timed-out draft is served as-is with a visible marker instead
            # of being polished into something that reads as complete.
            yield send("interpretation", {"content": humanize_text(draft)})
            yield send("interpretation", {"content": "\n\n(Response truncated due to timeout)"})
        elif draft:
            # Refinement pass: rewrite the draft for plain language,
            # consistency, and accuracy against the results table. A failure
            # here must NEVER lose the answer — the draft is the fallback.
            yield send("log", {"content": "Refining the answer..."})
            yield send("debug", {"content": f"Draft interpretation in {t_draft:.1f}s | ~{interp_tokens} chunks"})
            time.sleep(2)  # RPM pacing between back-to-back LLM calls
            refine_counter = {"n": 0}
            yield from refine_events_with_fallback(
                refine_interpretation_stream(
                    client, MODEL, question, sql, result_str, draft, on_retry=on_retry, fallback_client=paid_client,
                    extra_facts=CITY_FACTS,
                ),
                draft,
                send,
                transform=humanize_text,
                on_fail=lambda e: track_error("interpretation", f"Refine failed: {str(e)[:150]}"),
                timeout=stream_timeout,
                counter=refine_counter,
            )
            interp_tokens += refine_counter["n"]
        track_usage(0, interp_tokens)
        log.info("Request complete — %d chunks streamed", interp_tokens)

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

        # Cache the response only if it has a valid interpretation — never
        # errors, and never truncated/degraded answers (a one-off slow stream
        # must not become the permanent replay for every future asker).
        if should_cache and cache_events:
            has_interpretation = any('"type": "interpretation"' in e for e in cache_events)
            has_error = any('"type": "error"' in e for e in cache_events)
            has_truncation = any("Response truncated" in e for e in cache_events)
            if has_interpretation and not has_error and not has_truncation:
                response_cache[cache_key] = cache_events
                _save_cache()
                log.info("Cached response for: %s", question[:50])
            else:
                reason = "error" if has_error else ("truncated" if has_truncation else "no interpretation")
                log.info("Skipped caching (%s): %s", reason, question[:50])

    def safe_stream():
        """Wrap event_stream so any unhandled error still terminates the SSE
        stream with an error + done event. Without this, an exception raised
        before/between events would close the connection silently and leave
        the client's typing indicator spinning forever."""
        gen = event_stream()
        try:
            for event in gen:
                yield event
        except GeneratorExit:
            gen.close()
            raise
        except Exception as e:
            log.exception("Unhandled error in /api/ask stream: %s", e)
            track_error("unhandled", str(e)[:200])
            yield f"data: {json.dumps({'type': 'error', 'content': f'Something went wrong on the server ({type(e).__name__}). Please try again in a moment.'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(safe_stream(), media_type="text/event-stream")
