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
import time

import duckdb
import openai
import pandas as pd

log = logging.getLogger("analytics")

MAX_RETRIES = 3
RETRY_BASE_DELAY = 16  # seconds, matches Gemini's suggested retry delay


def _call_with_retry(fn, on_retry=None):
    """Retry a function on 429 rate limit errors. Optional on_retry callback for status updates."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        except openai.RateLimitError as e:
            if attempt == MAX_RETRIES:
                log.warning("Rate limit: all %d retries exhausted", MAX_RETRIES)
                raise
            delay = RETRY_BASE_DELAY
            match = re.search(r'retry in ([\d.]+)s', str(e))
            if match:
                delay = float(match.group(1)) + 1
            log.info("Rate limited (attempt %d/%d), retrying in %.0fs", attempt, MAX_RETRIES, delay)
            if on_retry:
                on_retry(attempt, MAX_RETRIES, delay)
            time.sleep(delay)
        except Exception as e:
            log.error("LLM call failed (attempt %d): %s", attempt, e)
            raise

DEFAULT_MODEL = "qwen-3-235b-a22b-instruct-2507"
DEFAULT_BASE_URL = "https://api.cerebras.ai/v1"


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


def build_interpret_prompt(schema_desc: str) -> str:
    """Build system prompt for result interpretation."""
    return textwrap.dedent(f"""\
        You are a data analytics assistant interpreting query results from government expenditure data.

        ## Rules
        - Give concise, insightful answers. Lead with the key finding.
        - Mention specific numbers and comparisons.
        - If results are empty, explain what that likely means.
        - If the data shows something notable or unexpected, call it out.
        - Keep responses under 200 words unless the user asked for detail.

        ## Schema context
        {schema_desc}""")


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

Return a short analysis (3-5 sentences max) of your query plan. Do NOT write SQL."""


def reason_about_query(client: openai.OpenAI, model: str, system_prompt: str, question: str, on_retry=None, history: list = None) -> tuple[str, dict, object]:
    """Think about the query before generating SQL. Returns (reasoning, usage_dict, raw_response)."""
    def _call():
        messages = [{"role": "system", "content": system_prompt + "\n\n" + REASONING_PROMPT}]
        if history:
            messages.extend(history[-6:])
        messages.append({"role": "user", "content": question})
        return client.with_raw_response.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.2,
            max_tokens=300,
        )
    raw = _call_with_retry(_call, on_retry=on_retry)
    response = raw.parse()
    usage = {}
    if hasattr(response, "usage") and response.usage:
        usage = {
            "prompt_tokens": response.usage.prompt_tokens or 0,
            "completion_tokens": response.usage.completion_tokens or 0,
            "total_tokens": response.usage.total_tokens or 0,
        }
    return response.choices[0].message.content.strip(), usage, raw


def generate_sql(client: openai.OpenAI, model: str, system_prompt: str, question: str, on_retry=None, history: list = None, reasoning: str = None) -> tuple[str, dict, object]:
    """Ask the model to generate SQL. Returns (sql, usage_dict, raw_response)."""
    def _call():
        messages = [{"role": "system", "content": system_prompt}]
        if history:
            messages.extend(history[-6:])
        user_content = question
        if reasoning:
            user_content = f"Question: {question}\n\nQuery plan:\n{reasoning}\n\nNow write ONLY the SQL query based on this plan."
        messages.append({"role": "user", "content": user_content})
        return client.with_raw_response.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.1,
            max_tokens=512,
        )
    raw = _call_with_retry(_call, on_retry=on_retry)
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
    def _call():
        return client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=1024,
        )
    response = _call_with_retry(_call)
    return response.choices[0].message.content.strip()


def interpret_results_stream(
    client: openai.OpenAI, model: str, system_prompt: str, question: str, sql: str, results: str, on_retry=None, history: list = None
):
    """Stream interpretation chunks as a generator."""
    user_msg = f"Question: {question}\n\nSQL executed:\n{sql}\n\nResults:\n{results}"
    def _call():
        messages = [{"role": "system", "content": system_prompt}]
        if history:
            messages.extend(history[-6:])
        messages.append({"role": "user", "content": user_msg})
        return client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.3,
            max_tokens=1024,
            stream=True,
        )
    stream = _call_with_retry(_call, on_retry=on_retry)
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


BLOCKED_SQL = re.compile(
    r'\b(COPY|EXPORT|ATTACH|DETACH|LOAD|INSTALL|CREATE\s+MACRO|DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE|'
    r'read_csv|read_parquet|read_json|read_csv_auto|write_csv|httpfs|postgres_scan|sqlite_scan)\b',
    re.IGNORECASE,
)


def execute_sql_safe(con: duckdb.DuckDBPyConnection, sql: str) -> tuple[pd.DataFrame, str]:
    """Execute SQL and return (dataframe, formatted string). Raises on failure."""
    if BLOCKED_SQL.search(sql):
        raise ValueError("Query contains blocked operations")
    pd.set_option("display.float_format", lambda x: f"{x:,.2f}")
    result_df = con.execute(sql).fetchdf()
    result_str = result_df.to_string(index=False, max_rows=50)
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
    interpret_system = build_interpret_prompt(schema_desc)

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
