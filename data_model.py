"""Generic city data engine.

Loads a city's datasets into DuckDB driven entirely by a city config pack
(see city_config.py and docs/canonical-model.md): expenditure sources with
per-era column mappings and coercions, canonicalization maps, data-quality
flags, enrichment tables, pre-computed summary tables, and the curated data
dictionary. Nothing city-specific lives in this module.

Module-level DATA_DICTIONARY / ALL_LABELS / EXPENDITURE_LABELS mirror the
active config for backward compatibility with app.py and the tests.
"""

import os
import re

import duckdb
import pandas as pd

from city_config import CityConfig, load_city_config

# ── Active config (module-level, selected via CITY_CONFIG env var) ───────────

CONFIG: CityConfig = load_city_config()

DATA_DICTIONARY = CONFIG.data_dictionary
ALL_LABELS = CONFIG.labels
EXPENDITURE_LABELS = ALL_LABELS.get("expenditures", {})


# ── Loader ───────────────────────────────────────────────────────────────────

def _sql_quote(s: str) -> str:
    return s.replace("'", "''")


def _ident_quote(s: str) -> str:
    """Quote a SQL identifier (column name), escaping embedded double quotes."""
    return '"' + s.replace('"', '""') + '"'


def _source_files(source: dict, data_dir: str) -> list:
    """Resolve a source's file pattern to existing (year, path) pairs.

    With a "years" range the pattern is formatted per year (Louisville's
    per-FY files); without one it's a literal filename or glob (single-file
    cities like Cincinnati), yielding year=None entries.
    """
    import glob as _glob
    pattern = source["files"]
    if "years" in source:
        lo, hi = source["years"]
        files = []
        for year in range(lo, hi + 1):
            path = os.path.join(data_dir, pattern.format(year=year))
            if os.path.exists(path):
                files.append((year, path))
        return files
    return [(None, p) for p in sorted(_glob.glob(os.path.join(data_dir, pattern)))]


def _coerce(df: pd.DataFrame, rules: dict) -> pd.DataFrame:
    for col, rule in rules.items():
        if col not in df.columns:
            continue
        if rule == "int":
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
        elif rule == "epoch_ms_date":
            df[col] = pd.to_datetime(df[col], unit="ms", errors="coerce").dt.strftime("%Y-%m-%d")
        elif rule == "text":
            df[col] = df[col].astype("string")
    return df


def _table_exists(con, table: str) -> bool:
    return table in [t[0] for t in con.execute("SHOW TABLES").fetchall()]


def _load_expenditures(con, cfg: CityConfig, data_dir: str) -> None:
    table = cfg.expenditures.get("table", "expenditures")
    loaded_years = []

    for source in cfg.expenditures.get("sources", []):
        reader = source.get("reader", "duckdb_union")
        if reader not in ("duckdb_union", "pandas_mapped"):
            raise ValueError(f"Unknown reader '{reader}' for source {source.get('id')}")
        files = _source_files(source, data_dir)
        if not files:
            continue

        if reader == "duckdb_union":
            file_list = ", ".join(f"'{_sql_quote(p)}'" for _, p in files)
            # Optional source->canonical renames applied in-query (fast path
            # for cities whose CSVs need mapping but no pandas-level coercion)
            column_map = source.get("column_map", {})
            select = "*"
            if column_map:
                renames = ", ".join(
                    f"{_ident_quote(s)} AS {_ident_quote(d)}" for s, d in column_map.items() if s != d
                )
                if renames:
                    select = f"* RENAME ({renames})"
            if not _table_exists(con, table):
                con.execute(f"""
                    CREATE TABLE {table} AS
                    SELECT {select} FROM read_csv_auto([{file_list}], union_by_name=true)
                """)
            else:
                con.execute(f"""
                    INSERT INTO {table} BY NAME
                    SELECT {select} FROM read_csv_auto([{file_list}], union_by_name=true)
                """)
            loaded_years.extend(str(y) for y, _ in files if y is not None)

        else:  # pandas_mapped
            column_map = source.get("column_map", {})
            drop = source.get("drop", [])
            coerce = source.get("coerce", {})
            for year, path in files:
                df = pd.read_csv(path)
                df = df.rename(columns=column_map)
                df = df.drop(columns=drop, errors="ignore")
                df = _coerce(df, coerce)
                if not _table_exists(con, table):
                    con.execute(f"CREATE TABLE {table} AS SELECT * FROM df")
                else:
                    # Align columns to the existing table schema
                    existing_cols = [c[0] for c in con.execute(f"DESCRIBE {table}").fetchall()]
                    for col in existing_cols:
                        if col not in df.columns:
                            df[col] = None
                    df = df[existing_cols]
                    con.execute(f"INSERT INTO {table} SELECT * FROM df")
                if year is not None:
                    loaded_years.append(str(year))

    total = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    years_note = f" across {len(loaded_years)} years ({', '.join(sorted(loaded_years))})" if loaded_years else ""
    print(f"{table}: {total:,} rows{years_note}")


def _apply_canonicalization(con, cfg: CityConfig) -> None:
    for spec in cfg.canonicalization:
        table = spec.get("table", "expenditures")
        src = spec["source_column"]
        target = spec["target_column"]
        upper = spec.get("case_insensitive", False)

        cases = []
        exact = cfg.load_map(spec["exact_map"]) if spec.get("exact_map") else {}
        for k, v in exact.items():
            key = _sql_quote(k.upper() if upper else k)
            lhs = f"UPPER({src})" if upper else src
            cases.append(f"WHEN {lhs} = '{key}' THEN '{_sql_quote(v)}'")
        prefix = cfg.load_map(spec["prefix_map"]) if spec.get("prefix_map") else {}
        for p, v in prefix.items():
            key = _sql_quote(p.upper() if upper else p)
            lhs = f"UPPER({src})" if upper else src
            cases.append(f"WHEN {lhs} LIKE '{key}%' THEN '{_sql_quote(v)}'")

        # An unseeded (empty) map is a valid onboarding state: the canonical
        # column just mirrors the source column until curation fills the map.
        expr = f"CASE {' '.join(cases)} ELSE {src} END" if cases else src
        con.execute(f"""
            ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {target} VARCHAR;
            UPDATE {table} SET {target} = {expr};
        """)
        before = con.execute(
            f"SELECT COUNT(DISTINCT {src}) FROM {table} WHERE {src} IS NOT NULL"
        ).fetchone()[0]
        after = con.execute(
            f"SELECT COUNT(DISTINCT {target}) FROM {table} WHERE {target} IS NOT NULL"
        ).fetchone()[0]
        print(f"{src} normalization: {before:,} variants -> {after:,} canonical names")


def _apply_data_quality(con, cfg: CityConfig) -> None:
    dq = cfg.data_quality
    if not dq:
        return
    table = dq.get("table", "expenditures")
    amount = dq["amount_column"]

    off = dq.get("offsetting")
    if off:
        key = off["group_key"]
        tol = off.get("tolerance", 0.01)
        con.execute(f"""
            ALTER TABLE {table} ADD COLUMN IF NOT EXISTS is_offsetting BOOLEAN DEFAULT FALSE;
            UPDATE {table} SET is_offsetting = TRUE
            WHERE {key} IN (
                SELECT {key} FROM {table}
                WHERE {amount} IS NOT NULL
                GROUP BY {key}
                HAVING ABS(SUM({amount})) < {tol} AND COUNT(*) > 1
            );
        """)

    art = dq.get("artifact")
    if art:
        key = art["group_key"]
        threshold = art["threshold"]
        con.execute(f"""
            ALTER TABLE {table} ADD COLUMN IF NOT EXISTS is_data_artifact BOOLEAN DEFAULT FALSE;
            UPDATE {table} SET is_data_artifact = TRUE
            WHERE ABS({amount}) > {threshold}
            AND {key} IN (
                SELECT {key} FROM {table}
                GROUP BY {key}
                HAVING ABS(SUM({amount})) < ABS(MAX({amount}))
            );
        """)

    offset_count = con.execute(f"SELECT COUNT(*) FROM {table} WHERE is_offsetting").fetchone()[0] if off else 0
    artifact_count = con.execute(f"SELECT COUNT(*) FROM {table} WHERE is_data_artifact").fetchone()[0] if art else 0
    print(f"Data quality: {offset_count} offsetting rows flagged, {artifact_count} data artifacts flagged")


def _load_enrichment(con, cfg: CityConfig, data_dir: str) -> None:
    for table_name, filename in cfg.enrichment_tables.items():
        csv_path = os.path.join(data_dir, filename)
        if os.path.exists(csv_path):
            con.execute(
                f"CREATE TABLE {table_name} AS SELECT * FROM read_csv_auto('{_sql_quote(csv_path)}')"
            )
            count = con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            print(f"{table_name}: {count:,} rows")
        else:
            print(f"{table_name}: not found ({csv_path})")


def _build_summaries(con, cfg: CityConfig) -> None:
    built = []
    for spec in cfg.summaries:
        requires = spec.get("requires", [])
        if all(_table_exists(con, t) for t in requires):
            con.execute(spec["sql"])
            built.append(spec["table"])
        else:
            print(f"{spec['table']}: skipped (requires {requires})")
    print(f"Summary tables created: {', '.join(built)}")


def load_all_data(data_dir: str = "data", config: CityConfig | None = None) -> duckdb.DuckDBPyConnection:
    """Load all of a city's datasets into DuckDB per its config pack."""
    cfg = config or CONFIG
    con = duckdb.connect()

    _load_expenditures(con, cfg, data_dir)
    _apply_canonicalization(con, cfg)
    _apply_data_quality(con, cfg)
    _load_enrichment(con, cfg, data_dir)
    _build_summaries(con, cfg)

    # Lock down DuckDB — disable external file access and make read-only safe
    con.execute("SET enable_external_access = false")
    return con


def get_full_schema_description(con: duckdb.DuckDBPyConnection) -> str:
    """Build a comprehensive schema description for all loaded tables."""
    tables = [t[0] for t in con.execute("SHOW TABLES").fetchall()]
    lines = []

    for table in tables:
        dd = DATA_DICTIONARY.get(table, {})
        desc = dd.get("description", "")
        col_docs = dd.get("columns", {})
        joins = dd.get("joins", "")
        labels = ALL_LABELS.get(table, {})

        row_count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        columns = con.execute(f"DESCRIBE {table}").fetchall()

        lines.append(f"## Table: {table}")
        if desc:
            lines.append(f"{desc}")
        lines.append(f"Rows: {row_count:,}")
        if joins:
            lines.append(f"Joins: {joins}")
        lines.append("")
        lines.append("Columns:")

        for col_name, col_type, *_ in columns:
            semantic = labels.get(col_name, col_name)
            doc = col_docs.get(col_name, "")
            line = f"  - {col_name} ({col_type}) — \"{semantic}\""
            if doc:
                line += f": {doc}"

            # Add sample values for string columns
            if "VARCHAR" in col_type.upper():
                try:
                    distincts = con.execute(
                        f"SELECT DISTINCT {col_name} FROM {table} "
                        f"WHERE {col_name} IS NOT NULL ORDER BY 1 LIMIT 5"
                    ).fetchall()
                    vals = [str(r[0]) for r in distincts]
                    if vals:
                        total_distinct = con.execute(
                            f"SELECT COUNT(DISTINCT {col_name}) FROM {table}"
                        ).fetchone()[0]
                        line += f"  e.g. {', '.join(repr(v) for v in vals[:4])} [{total_distinct} distinct]"
                except Exception:
                    pass
            elif "INT" in col_type.upper() or "DOUBLE" in col_type.upper():
                try:
                    stats = con.execute(
                        f"SELECT MIN({col_name}), MAX({col_name}) FROM {table}"
                    ).fetchone()
                    line += f"  range: {stats[0]} to {stats[1]}"
                except Exception:
                    pass

            lines.append(line)
        lines.append("")

    return "\n".join(lines)


def get_compact_schema_description(con: duckdb.DuckDBPyConnection) -> str:
    """Token-efficient schema for the LLM system prompt.

    Same facts the model needs to write correct SQL, in a compact format:
    one line per column (name + short type + curated doc), enum values listed
    ONLY for low-cardinality categoricals (the ones the model filters on), and
    no per-column sample dumps or numeric ranges (the bulk of the verbose
    version's tokens, and the least useful for query generation). Roughly a
    third the size of get_full_schema_description with no loss of the
    filtering/joining facts that drive accuracy.
    """
    tables = [t[0] for t in con.execute("SHOW TABLES").fetchall()]
    out = []
    for table in tables:
        # Skip internal helper tables (e.g. _payee_to_canonical); the model
        # should never query them, and they only add prompt tokens.
        if table.startswith("_"):
            continue
        dd = DATA_DICTIONARY.get(table, {})
        desc = dd.get("description", "")
        col_docs = dd.get("columns", {})
        joins = dd.get("joins", "")
        row_count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        columns = con.execute(f"DESCRIBE {table}").fetchall()

        header = f"## {table} ({row_count:,} rows)"
        if desc:
            header += f": {desc}"
        out.append(header)
        if joins:
            out.append(f"joins: {joins}")

        for col_name, col_type, *_ in columns:
            short_type = col_type.split("(")[0].lower()
            line = f"- {col_name} {short_type}"
            if "VARCHAR" in col_type.upper():
                try:
                    n_distinct = con.execute(
                        f"SELECT COUNT(DISTINCT {col_name}) FROM {table}"
                    ).fetchone()[0]
                    # Enumerate only genuinely categorical columns the model
                    # filters on; high-cardinality ones (payees, etc.) are handled
                    # by the canonical-column rules. Cap the list so a ~12-value
                    # column doesn't bloat the prompt.
                    if 0 < n_distinct <= 12:
                        # ORDER BY is load-bearing: without it DuckDB returns
                        # distinct values in parallel-scan order, so the schema
                        # text (and every prompt built from it, plus the
                        # prompt-hash cache version) would differ per process.
                        vals = [
                            str(r[0]) for r in con.execute(
                                f"SELECT DISTINCT {col_name} FROM {table} "
                                f"WHERE {col_name} IS NOT NULL ORDER BY 1 LIMIT 12"
                            ).fetchall()
                        ]
                        if vals:
                            line += " {" + ", ".join(vals) + "}"
                except Exception:
                    pass
            doc = col_docs.get(col_name, "")
            if doc:
                line += f" — {doc}"
            out.append(line)
        out.append("")

    return "\n".join(out)


CHART_TIME_KEYWORDS = ("year", "fiscal", "month", "date")
CHART_LABEL_KEYWORDS = CHART_TIME_KEYWORDS + ("name", "agency", "payee", "type", "category", "fund")


def infer_chart(df) -> tuple:
    """Pick (chart_type, label_col, value_col) for a result DataFrame.

    Pure function (no DB), so it is unit-testable in isolation.

    - value (y): a numeric measure that varies and isn't a year/time id. Prefer
      a float (dollar) measure over an integer count; otherwise the last such
      column (aggregates are conventionally last in the SELECT).
    - label (x): the most-varying categorical/time dimension. A constant column
      (<=1 distinct, e.g. fiscal_year = 2025 for a "top 5 in 2025" result) is
      never a useful axis and is skipped.
    - chart_type: ``line`` only for a genuine time series (time-named axis with
      one row per distinct, chronologically sortable time point); ``pie`` for a
      few proportional slices; ``bar`` otherwise; ``None`` when no chart fits.
    """
    cols = list(df.columns)
    n = len(df)

    def ndist(c):
        try:
            return int(df[c].nunique(dropna=True))
        except Exception:
            return 0

    def is_numeric(c):
        return pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c])

    def is_time_named(c):
        return any(k in c.lower() for k in CHART_TIME_KEYWORDS)

    # Value (y): varying numeric, not a year/time id. Prefer a float (dollar)
    # measure over an integer count so "SELECT payee, SUM(amt), COUNT(*)" charts
    # dollars, not the invoice count.
    value_cands = [c for c in cols if is_numeric(c) and ndist(c) > 1 and not is_time_named(c)]
    value_col = None
    if value_cands:
        floats = [c for c in value_cands if pd.api.types.is_float_dtype(df[c])]
        value_col = floats[-1] if floats else value_cands[-1]

    # Label (x): most-varying dimension; skip constants and the value column.
    label_col = None
    best = 0
    for c in cols:
        if c == value_col or ndist(c) <= 1:
            continue
        is_dim = (
            pd.api.types.is_string_dtype(df[c])
            or any(k in c.lower() for k in CHART_LABEL_KEYWORDS)
        )
        if is_dim and ndist(c) > best:
            best = ndist(c)
            label_col = c
    if label_col is None:
        label_col = next((c for c in cols if c != value_col and ndist(c) > 1), None)

    chart_type = None
    if label_col and value_col and 2 <= n <= 50:
        is_time = is_time_named(label_col)
        clean_series = ndist(label_col) == n  # one row per distinct time point
        if is_time and clean_series:
            chart_type = "line"
        elif n <= 5:
            chart_type = "pie"
        else:
            chart_type = "bar"

    # A line axis must be chronologically sortable (the caller sorts by it):
    # numeric, datetime, or 4-digit-year strings. A non-numeric string axis
    # (e.g. month NAMES) would mis-sort, so downgrade to a categorical chart.
    if chart_type == "line":
        col = df[label_col]
        sortable = col.dtype.kind in "iufcM"
        if not sortable:
            try:
                sortable = bool(col.dropna().astype(str).str.fullmatch(r"\d{4}").all())
            except Exception:
                sortable = False
        if not sortable:
            chart_type = "pie" if n <= 5 else "bar"

    return chart_type, label_col, value_col


def fiscal_year_end(year: int, fy_start_month: int = 1):
    """Last calendar day of fiscal `year` for a fiscal year starting in
    `fy_start_month` (Louisville: 7 -> FY2026 ends 2026-06-30)."""
    from datetime import date as _date, timedelta as _timedelta
    if fy_start_month == 1:
        return _date(year, 12, 31)
    return _date(year, fy_start_month, 1) - _timedelta(days=1)


def derive_year_facts(newest_year, fy_start_month=1, max_covered=None, grace_days=7) -> dict:
    """Decide whether the newest year's DATA is complete, and write the
    prompt rule + city fact that say so.

    Partial-ness is a statement about coverage, not the calendar: we compare
    how far payments actually run against the fiscal year's end, allowing
    `grace_days` because the last business day often precedes the last
    calendar day (FY2018 ends 2018-06-29 — June 30 was a Saturday).

    Unknown coverage (no date at all) is treated as PARTIAL: understating
    confidence is safe, asserting a partial year is complete is not.

    Pure function so both branches are testable (the web app and the CLI
    share it).
    """
    covered_through = None
    if max_covered is not None:
        text = str(max_covered)[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            covered_through = text

    if covered_through is None:
        is_partial = True
    else:
        from datetime import timedelta as _timedelta
        cutoff = fiscal_year_end(newest_year, fy_start_month) - _timedelta(days=grace_days)
        is_partial = covered_through < cutoff.isoformat()

    last_complete_year = newest_year - 1 if is_partial else newest_year

    if is_partial:
        through = f" — payments are only loaded through {covered_through}" if covered_through else ""
        rules = (
            f"- CRITICAL: FY{newest_year} data is INCOMPLETE{through}, so its totals are "
            f"partial and must not be compared like a full year. When presenting FY{newest_year} "
            f"always say the data is partial. For \"current\" or \"latest\" spending or salaries "
            f"use {last_complete_year}, the most recent year with complete data, unless the user "
            f"asks about {newest_year}. This applies to fiscal_year in expenditures AND CalYear "
            "in salary_data."
        )
        loaded = f" (loaded through {covered_through})" if covered_through else ""
        fact = (
            f"Data for fiscal/calendar year {newest_year} is incomplete{loaded}, so its totals "
            f"are partial. {last_complete_year} is the most recent year with complete data — "
            f"never describe {last_complete_year} or any earlier year as partial or in progress."
        )
    else:
        rules = (
            f"- FY{newest_year} data is complete; use it for \"current\" or \"latest\" spending "
            "and salaries. This applies to fiscal_year in expenditures AND CalYear in salary_data."
        )
        fact = (
            f"{newest_year} is the most recent year and its data is complete — never describe "
            "it or any earlier year as partial or in progress."
        )

    return {
        "is_partial": is_partial,
        "covered_through": covered_through,
        "last_complete_year": last_complete_year,
        "in_progress_year": newest_year if is_partial else None,
        "rules": rules,
        "fact": fact,
    }


_TOTAL_LABEL_SHAPES = re.compile(
    r"^(GRAND\s+TOTAL|TOTAL)(\s*[-–:]\s|\s+(ALL|SPENDING|AMOUNT|YEARS)\b|$)"
)


def drop_total_rows(df, label_col: str, value_col: str = None):
    """Remove grand-total rows (e.g. a GROUP BY ROLLUP row) from chart data.

    A total bar equals the sum of every other bar, so charting it doubles the
    axis and flattens the real values. Two complementary rules, both needed:

    - unambiguous total label shapes ("TOTAL", "TOTAL - ALL GRANT FUNDS",
      "GRAND TOTAL", "TOTAL ALL YEARS") are always dropped;
    - any label merely *containing* "total" is dropped only when its value
      also equals the sum of the remaining rows — so real payees such as
      TOTAL TOOL SUPPLY INC and TOTAL ACCESS GROUP INC keep their bars.

    Pure function (no DB), unit-tested alongside infer_chart.
    """
    if label_col not in df.columns or df.empty:
        return df
    labels = df[label_col].astype(str).str.strip().str.upper()
    drop = labels.str.match(_TOTAL_LABEL_SHAPES)

    if value_col and value_col in df.columns and len(df) >= 3:
        vals = pd.to_numeric(df[value_col], errors="coerce")
        # Measure against rows still in play — a shape-matched total already
        # dropped must not inflate the baseline. Compare magnitudes so an
        # all-negative result set (credits/offsets) is covered too. Any label
        # CONTAINING "total" is a candidate (SUBTOTAL, TOTALS, TOTAL_SPEND);
        # the value test is what protects real payees named TOTAL ....
        for idx in df.index[labels.str.contains("TOTAL") & ~drop]:
            v = vals.get(idx)
            if pd.isna(v):
                continue
            others = vals[~drop].sum() - v
            # Same sign required: a total shares the sign of what it sums. In a
            # mixed-sign chart a positive payee can numerically equal the
            # magnitude of negative credits without being a total.
            if v * others > 0 and abs(abs(v) - abs(others)) <= max(1.0, abs(others) * 0.005):
                drop.loc[idx] = True

    return df[~drop]


def humanize_text(text: str, table: str = "expenditures") -> str:
    """Replace column names with semantic labels in display text.
    Falls back to converting snake_case to Title Case for unknown columns."""
    import re
    labels = ALL_LABELS.get(table, EXPENDITURE_LABELS)
    # Also include all labels from all tables for cross-table results
    all_flat = {}
    for tbl_labels in ALL_LABELS.values():
        all_flat.update(tbl_labels)
    for col, label in sorted(all_flat.items(), key=lambda x: -len(x[0])):
        text = re.sub(rf'\b{re.escape(col)}\b', label, text)
    # Convert any remaining snake_case words to Title Case
    text = re.sub(r'\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b', lambda m: m.group(1).replace('_', ' ').title(), text)
    return text


def get_data_dictionary_text() -> str:
    """Return a human-readable data dictionary."""
    lines = [f"# {CONFIG.title} — Data Dictionary", ""]
    for table, info in DATA_DICTIONARY.items():
        labels = ALL_LABELS.get(table, {})
        lines.append(f"## {table}")
        lines.append(info.get("description", ""))
        lines.append(f"Scope: {info.get('record_scope', '')}")
        if info.get("joins"):
            lines.append(f"Joins: {info['joins']}")
        lines.append("")
        lines.append("| Column | Semantic Name | Description |")
        lines.append("|--------|--------------|-------------|")
        for col, doc in info.get("columns", {}).items():
            semantic = labels.get(col, col)
            lines.append(f"| `{col}` | {semantic} | {doc} |")
        lines.append("")
    return "\n".join(lines)
