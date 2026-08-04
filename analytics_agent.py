#!/usr/bin/env python3
"""
Core analytics engine for ArcGIS-sourced CSV data.

Uses DuckDB for SQL execution and any OpenAI-compatible API for natural language -> SQL.
Designed for datasets pulled via pull_arcgis.py.

Can be used as:
  - Importable module (used by app.py for web deployment)
  - Standalone CLI (python analytics_agent.py data/file.csv -q "question")
"""

import argparse
import json
import logging
import os
import re
import sys
import textwrap
import threading
import time

import duckdb
import openai
import pandas as pd

log = logging.getLogger("analytics")

MAX_RETRIES = 3
RETRY_BASE_DELAY = 16  # seconds, matches Gemini's suggested retry delay


# Track which tier was used on the last call (for dev mode display)
_last_tier_used = "free"


def get_last_tier_used() -> str:
    """Return which tier was used on the most recent LLM call."""
    return _last_tier_used


def _call_with_retry(fn, on_retry=None, fallback_fn=None):
    """Retry on 429 rate limit errors. Always tries free tier first, falls back to paid only when confirmed limited."""
    global _last_tier_used
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = fn()
            _last_tier_used = "free"
            return result
        except openai.RateLimitError as e:
            delay = RETRY_BASE_DELAY
            match = re.search(r'retry in ([\d.]+)s', str(e))
            if match:
                delay = min(float(match.group(1)) + 1, 60)

            # First retry: wait and try free tier again (per-minute limit may have cleared)
            if attempt == 1:
                log.info("Rate limited (attempt 1/%d), retrying free tier in %.0fs", MAX_RETRIES, delay)
                if on_retry:
                    on_retry(attempt, MAX_RETRIES, delay)
                time.sleep(delay)
                try:
                    result = fn()
                    _last_tier_used = "free"
                    return result
                except openai.RateLimitError:
                    # Free tier still limited — fall back to paid if available
                    if fallback_fn:
                        log.info("Free tier confirmed limited, using paid tier")
                        if on_retry:
                            on_retry(attempt + 1, MAX_RETRIES, 0)
                        try:
                            result = fallback_fn()
                            _last_tier_used = "paid"
                            return result
                        except Exception as fallback_err:
                            log.warning("Paid tier fallback failed: %s", fallback_err)

            if attempt == MAX_RETRIES:
                log.warning("Rate limit: all %d retries exhausted (free and paid)", MAX_RETRIES)
                raise

            log.info("Rate limited (attempt %d/%d), retrying in %.0fs", attempt, MAX_RETRIES, delay)
            if on_retry:
                on_retry(attempt, MAX_RETRIES, delay)
            time.sleep(delay)
        except Exception as e:
            log.error("LLM call failed (attempt %d): %s", attempt, e)
            raise

DEFAULT_MODEL = "qwen-3-235b-a22b-instruct-2507"
DEFAULT_BASE_URL = "https://api.cerebras.ai/v1"

# Rows rendered into the result table the model interprets and the UI shows.
# pandas splits this budget between the head and the tail, so a longer result
# is served with a gap in the middle — see execute_sql_safe.
MAX_DISPLAY_ROWS = 50

# Appended to a truncated result table. pandas renders the head and the tail
# with a bare "..." between them, and a model reading that enumerates the
# visible head and presents it as the whole answer: 102 grant funds became
# "here are the 24 sources", silently dropping everything past the gap.
#
# Deliberately order-NEUTRAL. order_for_display sorts by the measure only when
# the SQL contains no ORDER BY at all; anything carrying one is served as
# built, which includes ASC and keys like a month or a payee name. Calling the
# head "the largest" would be a reversal for exactly those queries — the same
# failure class the ordering rules were stripped back to avoid, just committed
# in prose instead of in the frame.
#
# Hashed into CACHE_VERSION (see app.py): it is model-visible input, so a
# change here must orphan answers written under the previous wording.
# The counts clause interpolated into the note. Module-level for the same
# reason as the note itself: it is the sentence the interpretation prompt tells
# the model to quote ("using the data row count from the note"), so editing it
# changes what the model reads and must invalidate cached answers. Kept beside
# TRUNCATION_NOTE so the pair is hashed together.
TRUNCATION_COUNTS = "this table has {rows} rows"
TRUNCATION_COUNTS_WITH_TOTALS = (
    "this table has {rows} rows, of which {entities} are data rows and the "
    "rest are totals/subtotals"
)

TRUNCATION_NOTE = (
    "[TRUNCATED: {counts}. Shown above are the first {half} rows and the last "
    "{half}, in the order the query produced them; the {omitted} rows in "
    "between are NOT shown. Do not describe the shown rows as the largest or "
    "the smallest unless the query's own ordering says so. Any list you give "
    "from this is partial — say how many you are naming, and quote the DATA "
    "row count when you say how many exist.]"
)


# ── Model fallback ───────────────────────────────────────────────────────────
# Providers deprecate models without notice (qwen-3-235b 404'd in prod while
# the container env still named it). On a model_not_found error we resolve a
# replacement from MODEL_FALLBACKS (then anything the account offers), record
# it module-wide so every later request uses it directly, and retry — no
# container rebuild or env change needed. /api/health surfaces the switch.

DEFAULT_MODEL_FALLBACKS = ["gpt-oss-120b", "zai-glm-4.7", "llama-3.3-70b", "llama3.1-8b"]

_active_model = None          # replacement model once a fallback has engaged
_model_fallback_event = None  # {"from", "to", "time"} of the last fallback
_fallback_lock = threading.Lock()  # SSE requests run on threadpool threads


def _fallback_preferences() -> list:
    env = os.environ.get("MODEL_FALLBACKS", "")
    if env.strip():
        return [m.strip() for m in env.split(",") if m.strip()]
    return list(DEFAULT_MODEL_FALLBACKS)


def get_active_model(configured: str) -> str:
    """The model actually in use: the configured one unless a fallback engaged."""
    return _active_model or configured


def get_model_fallback_event() -> dict | None:
    """Details of the last model fallback, or None if none has occurred."""
    return _model_fallback_event


def _is_model_not_found(exc: Exception) -> bool:
    if not isinstance(exc, openai.NotFoundError):
        return False
    # Prefer the structured error code (Cerebras/OpenAI send code=model_not_found);
    # fall back to a substring match only when no code is present.
    code = getattr(exc, "code", None)
    if code is None and isinstance(getattr(exc, "body", None), dict):
        # The openai SDK populates .code from a dict body itself; this branch
        # only guards non-standard OpenAI-compatible providers / hand-rolled
        # exceptions where that didn't happen.
        code = exc.body.get("code")
    if code is not None:
        return code == "model_not_found"
    return "model" in str(exc).lower()


def _resolve_fallback_model(client: openai.OpenAI, bad_model: str):
    """Pick a replacement: first preference the provider offers, else anything."""
    try:
        available = [m.id for m in client.models.list().data]
    except Exception as e:
        log.error("Model fallback: could not list provider models: %s", e)
        return None
    for pref in _fallback_preferences():
        if pref != bad_model and pref in available:
            return pref
    return next((m for m in available if m != bad_model), None)


def _record_model_fallback(from_model: str, to_model: str) -> None:
    """Idempotent: concurrent requests that raced through the same deprecation
    record (and ERROR-log) the switch exactly once."""
    global _active_model, _model_fallback_event
    with _fallback_lock:
        if _active_model == to_model:
            return
        _active_model = to_model
        _model_fallback_event = {
            "from": from_model,
            "to": to_model,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
    log.error("MODEL FALLBACK: '%s' no longer exists at provider; now using '%s'", from_model, to_model)


def _call_with_model_fallback(make_call, client, model, on_retry=None, fallback_client=None):
    """Run an LLM call, surviving provider model deprecation.

    make_call(client, model) must return a zero-arg callable. On a
    model_not_found error the replacement is resolved once, recorded
    module-wide, and the call retried with it.
    """
    model = get_active_model(model)
    fallback_fn = make_call(fallback_client, model) if fallback_client else None
    try:
        return _call_with_retry(make_call(client, model), on_retry=on_retry, fallback_fn=fallback_fn)
    except openai.NotFoundError as e:
        if not _is_model_not_found(e):
            raise
        # Another thread may have already switched while this call was in flight.
        already = get_active_model(model)
        replacement = already if already != model else _resolve_fallback_model(client, model)
        if not replacement:
            raise
        log.warning("Model '%s' not found at provider; retrying with '%s'", model, replacement)
        fallback_fn = make_call(fallback_client, replacement) if fallback_client else None
        result = _call_with_retry(make_call(client, replacement), on_retry=on_retry, fallback_fn=fallback_fn)
        # Record only after the replacement actually worked, so /api/health
        # never advertises a fallback that failed.
        _record_model_fallback(model, replacement)
        return result


def init_database(csv_path: str) -> tuple[duckdb.DuckDBPyConnection, str]:
    """Load CSV into DuckDB and return connection + schema description."""
    con = duckdb.connect()
    con.execute(f"CREATE TABLE data AS SELECT * FROM read_csv_auto('{csv_path}')")
    schema_desc = build_schema_description(con, "data")
    return con, schema_desc


def build_schema_description(con: duckdb.DuckDBPyConnection, table: str) -> str:
    """Generate a schema description from the loaded DuckDB table."""
    columns = con.execute(f"DESCRIBE {table}").fetchall()
    sample = con.execute(f"SELECT * FROM {table} LIMIT 3").fetchdf()
    row_count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    lines = [f"Table: {table}", f"Row count: {row_count:,}", "", "Columns:"]
    for col_name, col_type, *_ in columns:
        line = f"  - {col_name} ({col_type})"
        if "VARCHAR" in col_type.upper():
            try:
                distincts = con.execute(
                    f"SELECT DISTINCT {col_name} FROM {table} WHERE {col_name} IS NOT NULL LIMIT 8"
                ).fetchall()
                vals = [str(r[0]) for r in distincts]
                if vals:
                    line += f"  — e.g. {', '.join(repr(v) for v in vals[:5])}"
                    total_distinct = con.execute(
                        f"SELECT COUNT(DISTINCT {col_name}) FROM {table}"
                    ).fetchone()[0]
                    line += f"  [{total_distinct} distinct]"
            except Exception:
                pass
        elif "INT" in col_type.upper() or "DOUBLE" in col_type.upper() or "FLOAT" in col_type.upper():
            try:
                stats = con.execute(
                    f"SELECT MIN({col_name}), MAX({col_name}), AVG({col_name}) FROM {table}"
                ).fetchone()
                line += f"  — range: {stats[0]} to {stats[1]}, avg: {stats[2]:.2f}"
            except Exception:
                pass
        lines.append(line)

    lines.append("\nSample rows:")
    lines.append(sample.to_string(index=False))
    return "\n".join(lines)


def build_system_prompt(schema_desc: str, metadata_context: str = "") -> str:
    """Build the system prompt for SQL generation."""
    meta_section = ""
    if metadata_context:
        meta_section = f"\n## Dataset Metadata\n{metadata_context}\n"

    return textwrap.dedent(f"""\
        You are a data analytics assistant. You translate natural language questions into SQL queries
        and interpret results. You work with government open data from ArcGIS FeatureServer endpoints.

        ## Rules
        - Write DuckDB-compatible SQL (similar to PostgreSQL syntax).
        - The data is loaded into a table called `data`. Always query from `data`.
        - Return ONLY the SQL query, no explanation, no markdown fences, no preamble.
        - If a question is ambiguous, make reasonable assumptions and note them.
        - Use appropriate aggregations, GROUP BY, ORDER BY, and LIMIT clauses.
        - For monetary columns (invoice_amount, extended_amount), format with ROUND() when showing summaries.
        - Date columns are strings in YYYY-MM-DD format. Use string comparisons or CAST to DATE.
        - NULL values are common — use COALESCE or IS NOT NULL where appropriate.

        ## Schema
        {schema_desc}
        {meta_section}""")


def build_interpret_prompt(schema_desc: str, extra_facts=None) -> str:
    """Build system prompt for result interpretation.

    extra_facts: per-city data facts (from the city config pack) to enforce —
    mirrors refine_interpretation_stream's channel so the CLI path can carry
    city facts too."""
    return textwrap.dedent(f"""\
        You are a data analytics assistant interpreting query results from government expenditure data.

        ## Rules
        - Give concise, insightful answers. Lead with the key finding.
        - Mention specific numbers and comparisons.
        - NEVER rescale numbers. Repeat values at the magnitude shown in the results:
          192,770.57 is about $192.8K (thousands), NOT $192.77M. Only write M or B
          if the digits in the results actually reach millions/billions.
        - Only state facts that appear in the schema context or the results. Do NOT
          claim what years a dataset covers or what a compensation/amount figure
          includes unless the schema context says so explicitly for THAT table —
          never borrow another table's coverage.
        - If results are empty, explain what that likely means.
        - If the data shows something notable or unexpected, call it out.
        - A "Related city legislation" block may follow the results. It is
          retrieved by keyword, so some entries will be irrelevant — judge
          each one. When a document explains what the money was for, why it
          was appropriated, or a figure in the results, add one short sentence
          of context and name its file number inline (e.g. "Council set the
          priorities for this money in R-083-21"). Ignore the rest in silence.
          The results are always the source of every number: never attribute a
          figure to a document, never let a document override the results, and
          never list documents you did not use.
        - Keep responses under 200 words unless the user asked for detail.

        ## Schema context
        {schema_desc}""") + (
        "\n\n## Facts about this city's data (enforce these)\n"
        + "\n".join(f"- {f}" for f in extra_facts)
        if extra_facts else ""
    )


def load_metadata_context(metadata_path: str) -> str:
    """Extract useful context from ArcGIS metadata JSON."""
    try:
        with open(metadata_path) as f:
            meta = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return ""

    parts = []
    if meta.get("name"):
        parts.append(f"Dataset name: {meta['name']}")
    if meta.get("description"):
        parts.append(f"Description: {meta['description']}")
    if meta.get("copyrightText"):
        parts.append(f"Source: {meta['copyrightText']}")

    for field in meta.get("fields", []):
        domain = field.get("domain")
        if domain and domain.get("type") == "codedValue":
            vals = ", ".join(f"{cv['code']}={cv['name']}" for cv in domain.get("codedValues", []))
            parts.append(f"Field '{field['name']}' coded values: {vals}")

    return "\n".join(parts)


def strip_sql_fences(sql: str) -> str:
    """Remove markdown code fences from SQL output."""
    sql = sql.strip()
    if sql.startswith("```"):
        lines = sql.split("\n")
        sql = "\n".join(line for line in lines if not line.strip().startswith("```")).strip()
    return sql


REASONING_PROMPT = """Analyze this user question and plan the SQL query. Consider:
1. Which table(s) to query (expenditures, summary tables, contractor_profiles, salary_data, etc.)
2. Whether this is a follow-up that references prior context
3. Whether to use agency_canonical or agency, and whether to filter by fiscal year
4. Any data quality concerns (offsetting entries, artifacts)
5. What columns and aggregations are needed
6. Whether the results would benefit from a chart visualization. If yes, on the LAST line write exactly: CHART: type (where type is bar, line, or pie). Use line for time series, bar for comparisons/rankings, pie for proportional breakdowns. If no chart is appropriate (single values, text-heavy, individual records), write: CHART: none

Return a short analysis (3-5 sentences max) of your query plan, ending with the CHART line. Do NOT write SQL."""


def reason_about_query(client: openai.OpenAI, model: str, system_prompt: str, question: str, on_retry=None, history: list = None, fallback_client: openai.OpenAI = None) -> tuple[str, dict, object]:
    """Think about the query before generating SQL. Returns (reasoning, usage_dict, raw_response)."""
    def _make_call(c, m):
        def _call():
            messages = [{"role": "system", "content": system_prompt + "\n\n" + REASONING_PROMPT}]
            if history:
                messages.extend(history[-6:])
            messages.append({"role": "user", "content": question})
            return c.with_raw_response.chat.completions.create(
                model=m,
                messages=messages,
                temperature=0.2,
                max_tokens=300,
            )
        return _call
    raw = _call_with_model_fallback(_make_call, client, model, on_retry=on_retry, fallback_client=fallback_client)
    response = raw.parse()
    usage = {}
    if hasattr(response, "usage") and response.usage:
        usage = {
            "prompt_tokens": response.usage.prompt_tokens or 0,
            "completion_tokens": response.usage.completion_tokens or 0,
            "total_tokens": response.usage.total_tokens or 0,
        }
    return response.choices[0].message.content.strip(), usage, raw


def generate_sql(client: openai.OpenAI, model: str, system_prompt: str, question: str, on_retry=None, history: list = None, reasoning: str = None, fallback_client: openai.OpenAI = None) -> tuple[str, dict, object]:
    """Ask the model to generate SQL. Returns (sql, usage_dict, raw_response)."""
    def _make_call(c, m):
        def _call():
            messages = [{"role": "system", "content": system_prompt}]
            if history:
                messages.extend(history[-6:])
            user_content = question
            if reasoning:
                user_content = f"Question: {question}\n\nQuery plan:\n{reasoning}\n\nNow write ONLY the SQL query based on this plan."
            messages.append({"role": "user", "content": user_content})
            return c.with_raw_response.chat.completions.create(
                model=m,
                messages=messages,
                temperature=0.1,
                max_tokens=2048,
            )
        return _call
    raw = _call_with_model_fallback(_make_call, client, model, on_retry=on_retry, fallback_client=fallback_client)
    response = raw.parse()
    usage = {}
    if hasattr(response, "usage") and response.usage:
        usage = {
            "prompt_tokens": response.usage.prompt_tokens or 0,
            "completion_tokens": response.usage.completion_tokens or 0,
            "total_tokens": response.usage.total_tokens or 0,
        }
    return strip_sql_fences(response.choices[0].message.content), usage, raw


def interpret_results(
    client: openai.OpenAI, model: str, system_prompt: str, question: str, sql: str, results: str
) -> str:
    """Ask the model to interpret SQL results in plain English."""
    user_msg = f"Question: {question}\n\nSQL executed:\n{sql}\n\nResults:\n{results}"
    def _make_call(c, m):
        def _call():
            return c.chat.completions.create(
                model=m,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.3,
                max_tokens=4096,
            )
        return _call
    response = _call_with_model_fallback(_make_call, client, model)
    return response.choices[0].message.content.strip()


def interpret_results_stream(
    client: openai.OpenAI, model: str, system_prompt: str, question: str, sql: str, results: str, on_retry=None, history: list = None, fallback_client: openai.OpenAI = None, documents: str = ""
):
    """Stream interpretation chunks as a generator.

    documents: retrieved city-document context (rag.format_context). Appended
    to the user message rather than the system prompt because it varies per
    question — putting it in the system prompt would change the prompt hash
    (and so invalidate the whole response cache) on every question."""
    user_msg = f"Question: {question}\n\nSQL executed:\n{sql}\n\nResults:\n{results}"
    if documents:
        user_msg += f"\n\n{documents}"
    def _make_call(c, m):
        def _call():
            messages = [{"role": "system", "content": system_prompt}]
            if history:
                messages.extend(history[-6:])
            messages.append({"role": "user", "content": user_msg})
            return c.chat.completions.create(
                model=m,
                messages=messages,
                temperature=0.3,
                max_tokens=4096,
                stream=True,
            )
        return _call
    stream = _call_with_model_fallback(_make_call, client, model, on_retry=on_retry, fallback_client=fallback_client)
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


REFINE_SYSTEM_PROMPT = textwrap.dedent("""\
    You are the final editor for a civic data assistant that answers questions
    about city government spending for the general public.

    Rewrite the DRAFT ANSWER following every rule:
    - Plain, non-technical language: no SQL, database, or column-name jargon
      (say "department", never "agency_canonical").
    - Lead with the direct answer to the question in the first sentence.
    - Consistent number style: dollar amounts with $ and thousands separators;
      rounding for readability is fine ($19.6M, $267,811) but NEVER change a
      number's magnitude — check every figure against the RESULTS table
      (192,770.57 is about $192.8K, not $192.77M).
    - Every number and claim must come from the RESULTS table or be directly
      computable from it. Delete anything the results don't support,
      including any sentence describing what a figure includes or what years
      it covers when the results don't state that.
    - ONE narrow exception to that deletion rule: a RELATED CITY LEGISLATION
      block may follow the results. Keep a sentence saying what a document
      authorized or what it was for, with its file number spelled exactly as
      the draft wrote it. Never use a document to state what a figure
      includes, which years it covers, or how it was computed — those claims
      still come only from the results, and the bullet above still deletes
      them. Never introduce a citation the draft did not make, and never let a
      document change, explain away, or override a figure.
    - NEVER total or net a long list yourself: arithmetic is only allowed
      over a handful of values you can verify digit by digit. If the results
      have no total row, do not state an overall total — describe individual
      rows instead (a wrong grand total is worse than none). Only say where in
      a ranking a row sits when the SQL's own ORDER BY establishes it: a
      truncated table may show either end.
    - Never add together rows that are different VIEWS of the same spending
      (e.g. a department total and a category total, where one purchase can
      appear in both). Report such figures separately; summing them
      double-counts. Only add rows that are mutually exclusive slices.
    - Short numbered lines for lists; keep the whole answer under 180 words
      unless the draft genuinely needs more.
    - Plain text only — no markdown headers or tables.

    Return ONLY the rewritten answer, nothing else.""")


def refine_interpretation_stream(client, model, question, sql, results, draft, on_retry=None, fallback_client=None, extra_facts=None, documents=""):
    """Stream a refined (plain-language, consistency- and accuracy-checked)
    rewrite of a draft interpretation.

    Lean context by design: rubric + question + SQL + results + draft, plus a
    bounded document block when one was retrieved — but never the schema (the
    results table is the accuracy anchor). That keeps the pass near ~1-2K
    tokens instead of the ~7K a schema-bearing prompt would cost; the document
    block adds at most k hits x 600 chars (see rag.format_context and the
    pack's rag.k).
    extra_facts: per-city data facts (from the city config pack) the rewrite
    must enforce — city specifics never live in this shared rubric.
    documents: the same retrieved-document block the draft saw. Without it the
    refiner deletes every citation, because its own rule is that anything the
    results table doesn't support must go — and a file number never appears in
    a results table.
    """
    system_prompt = REFINE_SYSTEM_PROMPT
    if extra_facts:
        system_prompt += "\n\n## Facts about this city's data (enforce these)\n" + \
            "\n".join(f"- {f}" for f in extra_facts)
    user_msg = (
        f"QUESTION: {question}\n\nSQL EXECUTED:\n{sql}\n\n"
        f"RESULTS:\n{results}\n\n"
        + (f"{documents}\n\n" if documents else "")
        + f"DRAFT ANSWER:\n{draft}"
    )
    def _make_call(c, m):
        def _call():
            return c.chat.completions.create(
                model=m,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.2,
                max_tokens=4096,
                stream=True,
            )
        return _call
    stream = _call_with_model_fallback(_make_call, client, model, on_retry=on_retry, fallback_client=fallback_client)
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


def refine_events_with_fallback(refine_iter, draft, send, transform=None, on_fail=None, timeout=90, counter=None, sink=None):
    """Yield SSE events for the refine pass with the no-lost-answer invariant:
    if the refiner fails before producing anything, the draft is served
    verbatim; if it fails mid-stream, a visible truncation note is appended
    (never the full draft after partial refined text).

    Pure orchestration (no app globals) so it is unit-testable: `send` builds
    the SSE frame, `transform` post-processes text (e.g. humanize), `on_fail`
    receives the exception, `counter['n']` counts streamed chunks, and `sink`
    (a list) collects the text actually served — the caller needs that to know
    what the answer says, e.g. which documents it ended up citing.
    """
    transform = transform or (lambda s: s)
    t0 = time.time()
    refined_any = False
    try:
        for chunk in refine_iter:
            refined_any = True
            if counter is not None:
                counter["n"] += 1
            text = transform(chunk)
            if sink is not None:
                sink.append(text)
            yield send("interpretation", {"content": text})
            if time.time() - t0 > timeout:
                log.warning("Refinement stream timed out after %ds", timeout)
                yield send("interpretation", {"content": "\n\n(Response truncated due to timeout)"})
                break
    except GeneratorExit:
        raise
    except Exception as e:
        log.warning("Refinement failed (%s); serving draft", e)
        if on_fail:
            on_fail(e)
        yield send("debug", {"content": f"Refinement failed ({type(e).__name__}); serving the draft answer"})
        if refined_any:
            yield send("interpretation", {"content": "\n\n(Response truncated.)"})
    if not refined_any:
        text = transform(draft)
        if sink is not None:
            sink.append(text)
        yield send("interpretation", {"content": text})
    else:
        yield send("debug", {"content": f"Refined in {time.time() - t0:.1f}s"})


BLOCKED_SQL = re.compile(
    r'\b(COPY|EXPORT|ATTACH|DETACH|LOAD|INSTALL|CREATE\s+MACRO|DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE|'
    r'read_csv|read_parquet|read_json|read_csv_auto|write_csv|httpfs|postgres_scan|sqlite_scan)\b',
    re.IGNORECASE,
)


def fix_sql(sql: str) -> str:
    """Post-process generated SQL to enforce rules the LLM sometimes ignores."""
    fixed = sql

    # Replace raw column names with canonical versions
    # Only replace when used as a column reference (after SELECT, GROUP BY, WHERE, ORDER BY, JOIN ON)
    # Use word boundary matching to avoid replacing inside strings
    if re.search(r'\bGROUP BY\b.*\bagency\b', fixed, re.IGNORECASE | re.DOTALL):
        if 'agency_canonical' not in fixed.lower():
            fixed = re.sub(r'\bagency\b(?!\s*_canonical)', 'agency_canonical', fixed)
            log.info("SQL fix: replaced 'agency' with 'agency_canonical'")

    if re.search(r'\bGROUP BY\b.*\bpayee\b', fixed, re.IGNORECASE | re.DOTALL):
        if 'payee_canonical' not in fixed.lower():
            fixed = re.sub(r'\bpayee\b(?!\s*_canonical)', 'payee_canonical', fixed)
            log.info("SQL fix: replaced 'payee' with 'payee_canonical'")

    return fixed


def execute_sql_safe(con: duckdb.DuckDBPyConnection, sql: str) -> tuple[pd.DataFrame, str]:
    """Execute SQL and return (dataframe, formatted string). Raises on failure."""
    if BLOCKED_SQL.search(sql):
        raise ValueError("Query contains blocked operations")
    sql = fix_sql(sql)
    pd.set_option("display.float_format", lambda x: f"{x:,.2f}")
    result_df = con.execute(sql).fetchdf()
    # Ordered here rather than at render time so the table, the chart and the
    # text the model interprets all see the same rows in the same order — the
    # summary should lead with the largest figure too.
    from data_model import order_for_display
    result_df = order_for_display(result_df, sql)
    result_str = result_df.to_string(index=False, max_rows=MAX_DISPLAY_ROWS)
    if len(result_df) > MAX_DISPLAY_ROWS:
        # How many rows are actual entities, excluding a ROLLUP grand total —
        # the same subtraction the chart makes, so the two agree. The grant
        # query returns 103 rows for 102 funds, and quoting 103 next to a
        # chart titled "top 30 of 102" is the mismatch this note exists to
        # avoid re-creating.
        entities = None
        try:
            from data_model import infer_chart, drop_total_rows
            _, lbl, val = infer_chart(result_df)
            if lbl and val:
                n_real = len(drop_total_rows(result_df, lbl, val))
                if n_real != len(result_df):
                    entities = n_real
        except Exception:
            pass
        counts = (TRUNCATION_COUNTS_WITH_TOTALS.format(
                      rows=f"{len(result_df):,}", entities=f"{entities:,}")
                  if entities is not None else
                  TRUNCATION_COUNTS.format(rows=f"{len(result_df):,}"))
        half = MAX_DISPLAY_ROWS // 2
        result_str += "\n\n" + TRUNCATION_NOTE.format(
            counts=counts, half=half, omitted=f"{len(result_df) - MAX_DISPLAY_ROWS:,}")
    return result_df, result_str


def make_client(base_url: str = None, api_key: str = None) -> openai.OpenAI:
    """Create an OpenAI-compatible client. Disables built-in retries — we handle them in _call_with_retry."""
    import httpx
    return openai.OpenAI(
        api_key=api_key or os.environ.get("CEREBRAS_API_KEY") or os.environ.get("GEMINI_API_KEY", ""),
        base_url=base_url or os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL),
        max_retries=0,
        timeout=httpx.Timeout(60.0, connect=10.0),
    )


def make_paid_client(base_url: str = None, api_key: str = None) -> openai.OpenAI:
    """Create a paid-tier client as fallback. Returns None if no paid key is configured."""
    import httpx
    paid_key = api_key or os.environ.get("CEREBRAS_PAID_API_KEY", "")
    if not paid_key:
        return None
    return openai.OpenAI(
        api_key=paid_key,
        base_url=base_url or os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL),
        max_retries=0,
        timeout=httpx.Timeout(60.0, connect=10.0),
    )


# ── CLI ──────────────────────────────────────────────────────────────────────


def run_query(con, client, model, sql_system, interpret_system, question, interpret=True):
    """Full CLI pipeline: question -> SQL -> execute -> interpret."""
    print(f"\n{'─' * 60}")
    print(f"Q: {question}")
    print(f"{'─' * 60}")

    sql, _, _ = generate_sql(client, model, sql_system, question)
    print(f"\nSQL:\n  {sql}\n")

    try:
        result_df, result_str = execute_sql_safe(con, sql)
        print(f"Results ({len(result_df)} rows):")
        print(result_df.to_string(index=False, max_rows=30))
    except Exception as e:
        print(f"Query error: {e}")
        print("Attempting to fix...")
        fix_prompt = f"The following SQL failed with error: {e}\n\nOriginal SQL:\n{sql}\n\nFix the SQL query. Return ONLY the corrected SQL."
        sql, _ = generate_sql(client, model, sql_system, fix_prompt)
        print(f"\nRetry SQL:\n  {sql}\n")
        try:
            result_df, result_str = execute_sql_safe(con, sql)
            print(f"Results ({len(result_df)} rows):")
            print(result_df.to_string(index=False, max_rows=30))
        except Exception as e2:
            print(f"Still failing: {e2}")
            return

    if interpret and len(result_df) > 0:
        print(f"\n{'─' * 40}")
        interpretation = interpret_results(client, model, interpret_system, question, sql, result_str)
        print(interpretation)


def main():
    parser = argparse.ArgumentParser(description="Analytics agent for ArcGIS-sourced data")
    parser.add_argument("csv", help="Path to CSV data file")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model name (default: {DEFAULT_MODEL})")
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible API base URL")
    parser.add_argument("--api-key", default=None, help="API key (or set GEMINI_API_KEY env var)")
    parser.add_argument("--metadata", help="Path to ArcGIS metadata JSON for richer context")
    parser.add_argument("-q", "--question", help="One-shot question (skips interactive mode)")
    parser.add_argument("--no-interpret", action="store_true", help="Skip result interpretation")

    args = parser.parse_args()

    if not os.path.exists(args.csv):
        print(f"File not found: {args.csv}")
        sys.exit(1)

    print(f"Loading {args.csv}...")
    con, schema_desc = init_database(args.csv)
    row_count = con.execute("SELECT COUNT(*) FROM data").fetchone()[0]
    print(f"Loaded {row_count:,} rows.\n")

    meta_ctx = ""
    if args.metadata:
        meta_ctx = load_metadata_context(args.metadata)

    sql_system = build_system_prompt(schema_desc, meta_ctx)
    # Carry a city pack's data facts into the interpret prompt ONLY when a pack
    # was explicitly selected. The CLI's positional arg is an arbitrary CSV, and
    # load_city_config() would otherwise silently fall back to Louisville's
    # pack — asserting Louisville's facts about someone else's dataset.
    city_facts = None
    if os.environ.get("CITY_CONFIG"):
        try:
            from city_config import load_city_config
            from data_model import year_context
            cfg = load_city_config()
            # Same derivation the web app uses (one shared helper, so the two
            # can't drift) — but the CLI loads an arbitrary CSV into `data`,
            # so only run it when that table looks like expenditure data.
            years, year_facts = {}, []
            cols = {c[0] for c in con.execute("DESCRIBE data").fetchall()}
            if {"fiscal_year", "payment_date"} <= cols:
                con.execute("CREATE OR REPLACE TEMP VIEW expenditures AS SELECT * FROM data")
                yc = year_context(con, (cfg.city or {}).get("fiscal_year_start_month", 1))
                years, year_facts = yc["values"], yc["facts"]
            city_facts = cfg.data_facts_for(years) + year_facts
        except Exception as e:
            print(f"Warning: could not load CITY_CONFIG: {e}")
    interpret_system = build_interpret_prompt(schema_desc, extra_facts=city_facts)

    client = make_client(args.base_url, args.api_key)
    model = args.model
    print(f"Using model: {model}\n")

    if args.question:
        run_query(con, client, model, sql_system, interpret_system, args.question, not args.no_interpret)
        return

    print("Ask questions about your data. Type 'quit' to exit, 'schema' to see the table schema.")
    print(f"{'═' * 60}\n")

    while True:
        try:
            question = input(">>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not question:
            continue
        if question.lower() in ("quit", "exit", "q"):
            break
        if question.lower() == "schema":
            print(schema_desc)
            continue
        if question.lower().startswith("sql "):
            try:
                result = con.execute(question[4:]).fetchdf()
                print(result.to_string(index=False, max_rows=50))
            except Exception as e:
                print(f"Error: {e}")
            continue

        run_query(con, client, model, sql_system, interpret_system, question, not args.no_interpret)
        print()


if __name__ == "__main__":
    main()
