"""Generic city data engine.

Loads a city's datasets into DuckDB driven entirely by a city config pack
(see city_config.py and docs/canonical-model.md): expenditure sources with
per-era column mappings and coercions, canonicalization maps, data-quality
flags, enrichment tables, pre-computed summary tables, and the curated data
dictionary. Nothing city-specific lives in this module.

Module-level DATA_DICTIONARY / ALL_LABELS / EXPENDITURE_LABELS mirror the
active config for backward compatibility with app.py and the tests.
"""

import logging
import os
import re
from datetime import date, timedelta

import duckdb
import pandas as pd

from city_config import CityConfig, load_city_config

log = logging.getLogger("data_model")

# ── Active config (module-level, selected via CITY_CONFIG env var) ───────────

CONFIG: CityConfig = load_city_config()

DATA_DICTIONARY = CONFIG.data_dictionary
ALL_LABELS = CONFIG.labels
EXPENDITURE_LABELS = ALL_LABELS.get("expenditures", {})


# ── Loader ───────────────────────────────────────────────────────────────────

def _sql_quote(s: str) -> str:
    return s.replace("'", "''")


def _like_escape(s: str) -> str:
    """Escape LIKE metacharacters so a prefix map key is matched literally.

    A curated prefix key can legitimately contain % or _ (e.g. a vendor code);
    without escaping, '%' is 'any run' and '_' is 'any char', so a prefix like
    'A_B ' would match 'AXB ...' and silently merge unrelated payees. Use with
    ESCAPE '\\'. Backslash is escaped first so it doesn't double-escape."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
        # A years range with a {year}-less pattern would .format() to the SAME
        # path every iteration and load one file once per year — N-fold totals,
        # silently. That is a config error, not a load to attempt.
        if "{year}" not in pattern:
            raise ValueError(
                f"source {source.get('id', pattern)!r} has a 'years' range but its "
                f"'files' pattern {pattern!r} contains no '{{year}}' placeholder — "
                "it would load the same file once per year and multiply the data."
            )
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
            raw = p.upper() if upper else p
            # LIKE-escape THEN quote: the literal prefix must match as text, with
            # only the trailing % acting as a wildcard.
            key = _sql_quote(_like_escape(raw))
            lhs = f"UPPER({src})" if upper else src
            cases.append(f"WHEN {lhs} LIKE '{key}%' ESCAPE '\\' THEN '{_sql_quote(v)}'")

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

    # group_key may be a single column or a LIST of columns. A composite key
    # (e.g. [payee, invoice_number]) is safer than invoice_number alone, which
    # is not unique across payees: one vendor's payment could coincidentally
    # match another vendor's refund by number and amount and wrongly net to zero.
    def _key_sql(key):
        cols = key if isinstance(key, list) else [key]
        sel = ", ".join(cols)                       # for SELECT / GROUP BY
        lhs = sel if len(cols) == 1 else f"({sel})"  # row-value for WHERE ... IN
        return sel, lhs

    off = dq.get("offsetting")
    if off:
        sel, lhs = _key_sql(off["group_key"])
        tol = off.get("tolerance", 0.01)
        con.execute(f"""
            ALTER TABLE {table} ADD COLUMN IF NOT EXISTS is_offsetting BOOLEAN DEFAULT FALSE;
            UPDATE {table} SET is_offsetting = TRUE
            WHERE {lhs} IN (
                SELECT {sel} FROM {table}
                WHERE {amount} IS NOT NULL
                GROUP BY {sel}
                HAVING ABS(SUM({amount})) < {tol} AND COUNT(*) > 1
            );
        """)

    art = dq.get("artifact")
    if art:
        sel, lhs = _key_sql(art["group_key"])
        threshold = art["threshold"]
        con.execute(f"""
            ALTER TABLE {table} ADD COLUMN IF NOT EXISTS is_data_artifact BOOLEAN DEFAULT FALSE;
            UPDATE {table} SET is_data_artifact = TRUE
            WHERE ABS({amount}) > {threshold}
            AND {lhs} IN (
                SELECT {sel} FROM {table}
                GROUP BY {sel}
                HAVING ABS(SUM({amount})) < ABS(MAX({amount}))
            );
        """)

    # Impossible dates. The source extract carries invoice_date typos (years
    # 2102, 2502, 7202) that sort to the newest end of every month series —
    # the position a reader trusts most. A date outside the pack's window is
    # nulled: the row keeps its amount for totals, it just no longer claims
    # a date. Bounds: `min` (a literal date) and `max_years_after_newest`
    # (years beyond the newest fiscal year present, default 1).
    ds = dq.get("date_sanity")
    if ds:
        year_col = ds.get("year_column", "fiscal_year")
        newest = con.execute(f"SELECT MAX({year_col}) FROM {table}").fetchone()[0]
        years_after = int(ds.get("max_years_after_newest", 1))
        max_date = f"{int(newest) + years_after}-12-31" if newest is not None else None
        min_date = ds.get("min")
        for col in ds.get("columns", []):
            q = _ident_quote(col)
            conds = []
            if min_date:
                conds.append(f"{q} < DATE '{min_date}'")
            if max_date:
                conds.append(f"{q} > DATE '{max_date}'")
            if not conds:
                continue
            where = " OR ".join(conds)
            n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
            if n:
                con.execute(f"UPDATE {table} SET {q} = NULL WHERE {where}")
                print(f"Date sanity: nulled {n:,} impossible {col} value(s) outside "
                      f"{min_date or '-inf'}..{max_date or '+inf'}")

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
            # An advertised-but-empty summary is worse than a missing one: the
            # prompt still recommends it, so every question routed there
            # returns nothing with no explanation.
            try:
                n = con.execute(f"SELECT COUNT(*) FROM {spec['table']}").fetchone()[0]
            except duckdb.Error as e:
                # A diagnostics probe must never abort data loading. duckdb.Error
                # is the PEP-249 base: a `table:` key that names nothing raises
                # CatalogException, one that isn't a bare identifier (spaces,
                # hyphens, a reserved word) raises Parser/BinderException.
                log.warning(
                    "cannot check whether summary table %r is empty (%s: %s) — check that "
                    "the spec's `table:` key matches the table its SQL creates and is a "
                    "plain identifier", spec["table"], type(e).__name__, e,
                )
            else:
                if n == 0:
                    log.warning(
                        "%s materialized EMPTY — the prompt still recommends it; "
                        "check this city pack's source data for the years it filters on",
                        spec["table"],
                    )
        else:
            print(f"{spec['table']}: skipped (requires {requires})")
    print(f"Summary tables created: {', '.join(built)}")


def _trim_text_columns(con, tables: list) -> None:
    """Strip leading/trailing whitespace from every VARCHAR column that has any.

    The source CSVs carry padded variants of categorical values — Louisville
    has 68,602 expenditure rows whose spend_category is 'Automotive Fuel' with
    trailing spaces, next to the clean value — so an equality filter on the
    clean string silently dropped a third of a category. Run BEFORE
    canonicalization so the curated maps match the clean values, and only on
    columns that actually need it (an UPDATE over 2.2M rows is not free).
    """
    for table in tables:
        if not _table_exists(con, table):
            continue
        qt = _ident_quote(table)
        padded = []
        for name, typ, *_ in con.execute(f"DESCRIBE {qt}").fetchall():
            if "VARCHAR" not in typ.upper():
                continue
            q = _ident_quote(name)
            n = con.execute(f"SELECT COUNT(*) FROM {qt} WHERE {q} <> TRIM({q})").fetchone()[0]
            if n:
                padded.append((name, n))
        if padded:
            con.execute(f"UPDATE {qt} SET " + ", ".join(
                f"{_ident_quote(c)} = TRIM({_ident_quote(c)})" for c, _ in padded))
            print(f"{table}: trimmed whitespace in " + ", ".join(f"{c} ({n:,} rows)" for c, n in padded))


def _ingest(con: duckdb.DuckDBPyConnection, cfg: CityConfig, data_dir: str) -> None:
    """Build every table from the city pack's CSVs into an open connection.

    Shared by the in-memory path (load_all_data) and the artifact build
    (build_database), so the two can never drift into producing different
    databases.
    """
    _load_expenditures(con, cfg, data_dir)
    _trim_text_columns(con, [cfg.expenditures.get("table", "expenditures")])
    _apply_canonicalization(con, cfg)
    _apply_data_quality(con, cfg)
    _load_enrichment(con, cfg, data_dir)
    _trim_text_columns(con, list(cfg.enrichment_tables.keys()))
    _build_summaries(con, cfg)
    # The vocabulary the prompt cannot carry (see grounding.py). Built last so
    # it sees canonical columns and the data-quality flags.
    from grounding import build_value_index
    build_value_index(con, cfg)


def _lock_down(con: duckdb.DuckDBPyConnection) -> None:
    """Disable external file access, so LLM-generated SQL cannot reach the
    filesystem (read_csv of an arbitrary path, COPY TO, etc.).

    IRREVERSIBLE for the life of the connection: once set, even ATTACH of a
    new file fails with PermissionException. It must therefore be applied only
    on a *serving* connection — never on the build path, which has to write the
    artifact. Verified to still apply, and still block both escapes, on a
    read-only connection opened over a prebuilt file.
    """
    con.execute("SET enable_external_access = false")


def load_all_data(data_dir: str = "data", config: CityConfig | None = None) -> duckdb.DuckDBPyConnection:
    """Load all of a city's datasets into an in-memory DuckDB per its config pack.

    The original path: rebuilds everything from CSV on every call (~6s and
    ~1.9GB for Louisville). Kept for local dev, the tests, and refresh_data.py.
    Production serving should prefer a prebuilt artifact — see load_prebuilt.
    """
    cfg = config or CONFIG
    con = duckdb.connect()
    _ingest(con, cfg, data_dir)
    _lock_down(con)
    return con


def build_database(out_path: str, data_dir: str = "data",
                   config: CityConfig | None = None) -> str:
    """Build the analytical database from CSVs into a DuckDB file at out_path.

    The offline half of the split: run once in CI, ship the result. Writes to a
    temporary sibling and renames, so an interrupted build cannot leave a
    half-populated artifact that would load and serve wrong answers.

    Deliberately does NOT lock the connection down — see _lock_down.
    """
    cfg = config or CONFIG
    tmp = f"{out_path}.building"
    for stale in (tmp, f"{tmp}.wal"):
        if os.path.exists(stale):
            os.remove(stale)

    con = duckdb.connect(tmp)
    try:
        _ingest(con, cfg, data_dir)
    except BaseException:
        # Clean up the partial build before propagating. Without this a failed
        # or interrupted run leaves a multi-hundred-MB .building file on disk —
        # which the next build would silently delete, hiding that anything went
        # wrong, while a container just loses the space.
        con.close()
        for junk in (tmp, f"{tmp}.wal"):
            if os.path.exists(junk):
                os.remove(junk)
        raise
    con.close()

    os.replace(tmp, out_path)
    log.info("Built %s (%.0f MB) from %s", out_path,
             os.path.getsize(out_path) / 1e6, data_dir)
    return out_path


def load_prebuilt(db_path: str) -> duckdb.DuckDBPyConnection:
    """Open a prebuilt artifact read-only, locked down for serving.

    The serving half of the split. Two independent protections: read_only=True
    stops any write to the artifact, and _lock_down stops the filesystem
    escapes that read-only alone would still permit.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(
            f"No prebuilt database at {db_path!r}. Build one with: "
            f"python data_model.py --materialize {db_path}"
        )
    con = duckdb.connect(db_path, read_only=True)
    _lock_down(con)
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

            # Add sample values for string columns. Column names are quoted
            # (_ident_quote): a Socrata/CSV export column with a space or a
            # reserved word would otherwise throw here, get swallowed by the bare
            # except, and silently lose that column's enrichment with no log.
            qcol = _ident_quote(col_name)
            if "VARCHAR" in col_type.upper():
                try:
                    distincts = con.execute(
                        f"SELECT DISTINCT {qcol} FROM {table} "
                        f"WHERE {qcol} IS NOT NULL ORDER BY 1 LIMIT 5"
                    ).fetchall()
                    vals = [str(r[0]) for r in distincts]
                    if vals:
                        total_distinct = con.execute(
                            f"SELECT COUNT(DISTINCT {qcol}) FROM {table}"
                        ).fetchone()[0]
                        line += f"  e.g. {', '.join(repr(v) for v in vals[:4])} [{total_distinct} distinct]"
                except Exception:
                    pass
            elif "INT" in col_type.upper() or "DOUBLE" in col_type.upper():
                try:
                    stats = con.execute(
                        f"SELECT MIN({qcol}), MAX({qcol}) FROM {table}"
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
            qcol = _ident_quote(col_name)  # tolerate spaced/reserved column names
            if "VARCHAR" in col_type.upper():
                try:
                    n_distinct = con.execute(
                        f"SELECT COUNT(DISTINCT {qcol}) FROM {table}"
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
                                f"SELECT DISTINCT {qcol} FROM {table} "
                                f"WHERE {qcol} IS NOT NULL ORDER BY 1 LIMIT 12"
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


# How many points the chart actually renders. The caller truncates to this,
# and a truncated chart says so in its title.
CHART_MAX_POINTS = 30
# Ceiling on how large a result may be and still be worth charting. It exists
# only to keep a raw multi-thousand-row dump from being represented by its
# first 30 rows; it must stay well ABOVE CHART_MAX_POINTS, because a result
# that merely needs truncating is the normal case for a ranked "top spenders"
# question. When this was 50 — under the render cap's own reach — a 61-row
# agency ranking produced no chart at all while a 50-row one charted fine.
CHART_MAX_ROWS = 300
CHART_TIME_KEYWORDS = ("year", "fiscal", "month", "date")
CHART_LABEL_KEYWORDS = CHART_TIME_KEYWORDS + ("name", "agency", "payee", "type", "category", "fund")

# Whether a chart's y-values are dollars or a plain count, so the frontend axis
# formats "1,500 employees" as 1,500 rather than "$1.5K".
CHART_COUNT_KEYWORDS = ("count", "number", "num_", "_num", "n_distinct", "distinct",
                        "transactions", "payments_just_under", "employees", "_count")
# Only UNAMBIGUOUS money words. Ambiguous ones (total, paid, fund, comp, rate)
# were dropped on purpose: they also appear in count aliases an LLM writes
# (total_vendors, vendors_paid, funded_projects, companies), so relying on them
# would mislabel those integer counts as currency. Everything not caught here
# or by a count keyword falls to the integer-dtype signal, which is reliable in
# this schema (dollar sums are ROUND()ed floats, counts are COUNT(*) integers).
CHART_MONEY_KEYWORDS = ("spend", "amount", "salary", "invoice", "revenue",
                        "cost", "dollar", "extended")


def measure_kind(col_name, series=None) -> str:
    """Classify a chart's value column as 'currency' or 'count' for axis formatting.

    Check order (matters): count keywords -> UNAMBIGUOUS money keywords ->
    integer dtype -> currency default.

    The safety of this order depends on the money list staying unambiguous.
    Money keywords are checked BEFORE the integer-dtype signal ON PURPOSE, so a
    rare integer dollar column (`invoice_amount` as an int) is still currency.
    That is only safe because ambiguous words (`total`, `paid`, `fund`, `comp`,
    `rate`) were DROPPED from the list — they also appear in integer count
    aliases an LLM writes (`total_vendors`, `vendors_paid`, `funded_projects`,
    `companies`), which must fall through to the integer-dtype check and read as
    counts, not `$1.5K`. Do NOT re-add a weak word here expecting "dtype wins on
    integers": it would re-break exactly those count aliases. Explicit count
    keywords win over everything; a float with no money word defaults to currency
    (dollar sums are ROUND()ed floats here, counts are COUNT(*) integers).
    """
    name = (col_name or "").lower()
    if any(k in name for k in CHART_COUNT_KEYWORDS):
        return "count"
    if any(k in name for k in CHART_MONEY_KEYWORDS):
        return "currency"
    try:
        if series is not None and pd.api.types.is_integer_dtype(series) \
                and not pd.api.types.is_bool_dtype(series):
            return "count"
    except Exception:
        pass
    return "currency"


# YYYY, YYYY-MM, YYYY-MM-DD. Zero-padded ISO parts sort lexicographically in
# chronological order, so a string column of these is a legitimate time axis.
# Restricting this to bare \d{4} sent "spending by month" (SUBSTR(date,1,7) ->
# '2021-01') down the categorical path, where it was charted from the wrong end.
_ISO_DATE_PART = re.compile(r"\d{4}(-\d{2}){0,2}")


def is_time_named(col: str) -> bool:
    return any(k in col.lower() for k in CHART_TIME_KEYWORDS)


def is_chronological(series, require_sorted: bool = True) -> bool:
    """Is this column a time axis whose order is meaningful?

    require_sorted=False asks only "could this be sorted chronologically?",
    which is what chart-type inference needs; the default also demands the
    frame actually BE in ascending order, which is what deciding which end to
    truncate needs."""
    # Nulls are dropped for BOTH kinds before the ordering test, because
    # is_monotonic_increasing is False whenever a series contains NaN/NaT. When
    # only the string branch dropped them, one null month bucket made a
    # genuinely chronological axis answer False in both directions, and
    # chart_window fell through to head() — the wrong-end truncation again, by
    # a third route. Nulls here are real: the Louisville pack filters
    # `fiscal_year IS NOT NULL` because the raw rows carry them, and generated
    # SQL will not reliably do the same.
    s = series.dropna()
    if s.empty:
        return False
    if s.dtype.kind in "iufcM":
        return bool(s.is_monotonic_increasing) if require_sorted else True
    try:
        t = s.astype(str)
        if not bool(t.str.fullmatch(_ISO_DATE_PART).all()):
            return False
        return bool(t.is_monotonic_increasing) if require_sorted else True
    except Exception:
        return False


_ORDINAL_FN_RE = re.compile(
    r"\b(?:row_number|rank|dense_rank|percent_rank|ntile)\s*\(", re.I)
# A quoted alias is not exotic here: `rank` collides with a function name, so
# `AS "rank"` is exactly what a generator reaches for.
_ALIAS_RE = re.compile(r'\s*(?:as\s+)?(?:"([^"]+)"|([A-Za-z_]\w*))', re.I)
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")


def _blank_literals(sql: str) -> str:
    """``sql`` with string literals and comments replaced by spaces.

    Same length, so offsets still line up. Quoted identifiers are deliberately
    KEPT — an alias may legitimately be quoted, which is the whole point of
    reading them — while a stray paren inside a literal would otherwise
    truncate a scan that counts brackets.
    """
    out = list(sql)
    i, n = 0, len(sql)

    def blank(start, end):
        for k in range(start, min(end, n)):
            out[k] = " "

    while i < n:
        c = sql[i]
        if c == "'":                                  # string literal
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            blank(i, j)
            i = j
        elif c == '"':                                # quoted identifier: keep
            j = i + 1
            while j < n and sql[j] != '"':
                j += 1
            i = min(j + 1, n)
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            blank(i, j)
            i = j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            blank(i, j)
            i = j
        else:
            i += 1
    return "".join(out)


def _skip_parens(s: str, i: int) -> int:
    """Index just past the balanced group starting at ``s[i] == '('``."""
    n = len(s)
    if i >= n or s[i] != "(":
        return i
    depth = 0
    while i < n:
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def sql_ordinal_columns(sql: str) -> set:
    """Result columns that are window ordinals rather than measures.

    A top-N-per-group query keeps its ROW_NUMBER() column, and `ORDER BY rn`
    then names it as the ranked measure. A row number is not one: charting it
    draws a 1,2,3 staircase with the axis titled after the rank.

    Read from the SQL, because the VALUES cannot settle it. Two earlier attempts
    tried: any gapless run from 0/1 in row order looks like a rank, and so does
    a genuine measure — a spend of 6,5,4,3,2,1 across six categories, or a
    top-5-by-count whose counts really are 5,4,3,2,1. Rejecting those cost one
    frame its only chart and the other its correct axis, both silently. The
    SELECT list already says which is which, so ask it instead of guessing.
    """
    out = set()
    if not sql:
        return out
    scrubbed = _blank_literals(sql)
    n = len(scrubbed)
    for m in _ORDINAL_FN_RE.finditer(scrubbed):
        i = _skip_parens(scrubbed, m.end() - 1)      # the call's own arguments
        j = i
        while j < n and scrubbed[j].isspace():
            j += 1
        if scrubbed[j:j + 4].lower() == "over":
            j += 4
            while j < n and scrubbed[j].isspace():
                j += 1
            if j < n and scrubbed[j] == "(":
                j = _skip_parens(scrubbed, j)        # inline window spec
            else:
                w = _IDENT_RE.match(scrubbed, j)     # or a named window
                j = w.end() if w else j
            i = j
        alias = _ALIAS_RE.match(scrubbed, i)
        if not alias:
            continue
        name = alias.group(1) or alias.group(2) or ""
        # A bare ROW_NUMBER() with no alias names no column, and what follows it
        # is the next keyword rather than a name.
        if name.lower() not in ("", "as", "from", "over", "window"):
            out.add(name)
    return out


def infer_chart(df, sql: str = None) -> tuple:
    """Pick (chart_type, label_col, value_col) for a result DataFrame.

    Pure function (no DB), so it is unit-testable in isolation. ``sql`` is
    optional and read only to see which measure the query ranked by.

    - value (y): a numeric measure that varies and isn't a year/time id. The
      measure the query's ORDER BY keyed on, when there is one; else prefer a
      float (dollar) measure over an integer count; otherwise the last such
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

    # is_time_named is the module-level helper (same definition); no local copy.

    # Value (y): varying numeric, not a year/time id. Prefer a float (dollar)
    # measure over an integer count so "SELECT payee, SUM(amt), COUNT(*)" charts
    # dollars, not the invoice count.
    numeric_cands = [c for c in cols if is_numeric(c) and ndist(c) > 1 and not is_time_named(c)]
    # Drop ROW_NUMBER()-style columns the SQL identifies as ordinals, but never
    # the last candidate: an ordinal is a poor y-axis, and no y-axis is worse.
    ordinals = sql_ordinal_columns(sql)
    value_cands = [c for c in numeric_cands if c not in ordinals] or numeric_cands
    value_col = None
    if value_cands:
        floats = [c for c in value_cands if pd.api.types.is_float_dtype(df[c])]
        value_col = floats[-1] if floats else value_cands[-1]

        # When the result carries several measures, chart the one the query
        # ranked by. "job_title, department, max_total_comp, avg_total_comp
        # ORDER BY max_total_comp DESC" ends in the AVERAGE, so the last-float
        # rule drew average pay in max-pay order: a frame that IS sorted,
        # plotted through a column that isn't, which reads as no sort at all.
        # The ranked measure is both the ordered one and the one the question
        # asked about. Restricted to existing candidates — which exclude row
        # ordinals, so `ORDER BY rn` on a kept ROW_NUMBER() column cannot
        # displace the dollars with a staircase.
        if sql:
            ranked = sql_order_measure(sql, df)
            if ranked in value_cands:
                value_col = ranked

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
    if label_col and value_col and 2 <= n <= CHART_MAX_ROWS:
        is_time = is_time_named(label_col)
        clean_series = ndist(label_col) == n  # one row per distinct time point
        if is_time and clean_series:
            chart_type = "line"
        elif n <= 5:
            chart_type = "pie"
        else:
            chart_type = "bar"

    # A line axis must be chronologically sortable (the caller sorts by it):
    # numeric, datetime, or zero-padded ISO date parts, which sort
    # lexicographically in chronological order. A non-numeric string axis
    # (e.g. month NAMES) would mis-sort, so downgrade to a categorical chart.
    if chart_type == "line":
        if not is_chronological(df[label_col], require_sorted=False):
            chart_type = "pie" if n <= 5 else "bar"

    return chart_type, label_col, value_col


# Reordering a huge frame costs more than it can possibly be worth, and
# infer_chart's per-column nunique() is what makes it expensive.
_MAX_REORDER_ROWS = 50_000

_ORDER_BY_RE = re.compile(r"order\s+by\b", re.I)


def sql_orders_result(sql: str) -> bool:
    """Does this statement contain ANY ordering instruction?

    True if the words ORDER BY appear anywhere outside a string literal, a
    quoted identifier or a comment. Nothing finer: not paren depth, not window
    specs, not ranking functions, not which column was keyed.

    That bluntness is the conclusion of seven attempts at something smarter,
    each of which shipped a REVERSED result. The distinction being chased is
    "did this clause select rows or merely decorate them", and every proxy for
    it leaked:

      - top-level only          -> flipped bottom-N in a CTE
      - borrow the direction    -> flipped a slice keyed on a date
      - require a paired LIMIT  -> flipped when the LIMIT sat one level out
      - gate on the outer body  -> flipped through CTE chains, derived tables,
                                   comma joins and UNION
      - exempt window specs     -> flipped rank-filtered ASC windows
      - disqualify ranking fns  -> suppressed the de-duplication idiom, and
                                   still could not see a bottom-N whose rank
                                   key is a plain column rather than an
                                   aggregate

    A wrong order is worse than no order: it presents "the ten payees who
    received the LEAST" as a descending ranking, in the table, the chart, and
    the prose the model writes from it. So the only results reordered are
    those about which the query said nothing whatsoever.

    The cost is real and accepted: a query carrying a purely cosmetic ORDER BY
    — a de-duplicating ROW_NUMBER() window, a CTE whose order a join discards
    — is served as the engine returns it, which may look arbitrary. The fix
    for those belongs in the SQL prompt, which asks for an explicit ORDER BY
    on every multi-row result; that is the layer that can tell what the
    question meant."""
    return bool(_order_by_keys(sql))


def _order_by_keys(sql: str) -> list:
    """Offsets just past each ORDER BY that is real code, in source order.

    One scanner, so "is there an order" and "what was it keyed on" can never
    disagree about which ORDER BYs exist. Skips string literals, quoted
    identifiers and comments; understands nothing else about SQL structure, for
    all the reasons in sql_orders_result.
    """
    if not sql:
        return []
    out = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":                                  # string literal ('' escapes)
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    break
                i += 1
            i += 1
        elif c == '"':                                # quoted identifier
            i += 1
            while i < n and sql[i] != '"':
                i += 1
            i += 1
        elif sql.startswith("--", i):
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl + 1
        elif sql.startswith("/*", i):
            e = sql.find("*/", i + 2)
            i = n if e == -1 else e + 2
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            m = _ORDER_BY_RE.match(sql, i) if sql[i:j].lower() == "order" else None
            if m:
                out.append(m.end())
                i = m.end()
            else:
                i = j
        else:
            i += 1
    return out


def sql_order_measure(sql: str, df):
    """The result column the query ranked by, or None.

    Answers only "which column", never "in which direction" and never "should
    these rows move" — nothing here reorders anything. That is the whole reason
    it is safe to parse ORDER BY here at all: the seven reversed results
    catalogued in sql_orders_result all came from letting a parse decide row
    order or direction. The worst a bad parse can do here is chart a different
    measure than intended, which is exactly what happens today anyway.

    Scoped further by the caller, which accepts the answer only if the column
    was already an eligible value candidate. So this can re-rank the measures
    infer_chart was choosing between, and can introduce nothing new.
    """
    keys = _order_by_keys(sql)
    if not keys or df is None or not len(df.columns):
        return None
    # The last ORDER BY: in generated SQL the outermost SELECT comes last, so
    # its ordering is the one the reader sees.
    # First key only; a secondary key is a tie-break, not the ranking.
    key = re.split(r"[,;)]", sql[keys[-1]:])[0].strip()
    if key.startswith('"'):                       # quoted, possibly with spaces
        end = key.find('"', 1)
        key = key[1:end] if end != -1 else key[1:]
    else:
        # The bare first token, which drops every trailing modifier at once —
        # DESC, NULLS LAST, USING <op>, a trailing LIMIT — without enumerating
        # them.
        key = key.split()[0] if key.split() else ""
    if not key:
        return None

    # ORDER BY 3 -> the third selected column.
    if key.isdigit():
        idx = int(key) - 1
        return df.columns[idx] if 0 <= idx < len(df.columns) else None

    key = key.split(".")[-1].strip('"').strip()
    for col in df.columns:
        if str(col).lower() == key.lower():
            return col
    return None


def order_for_display(df, sql: str):
    """Put the largest values first when the query expressed no order at all.

    Generated SQL often omits ORDER BY entirely — a UNION ALL of a "Mayor"
    filter and a "Police Chief" filter, say — and DuckDB then returns scan
    order. The reader gets a table in no discernible order and a bar chart
    whose bars jump around, when the question was plainly "who earns the
    most". Sorting by the measure descending is what the question meant.

    Applied only to results whose order is definitionally arbitrary (see
    sql_orders_result), and only ever DESCENDING. The direction is never
    inferred from the query: a borrowed direction is how this function
    repeatedly turned "which ten payees received the least" into
    largest-of-the-ten-first. Time-keyed results are left alone too —
    chronology is their order, and the chart layer sorts them by axis.
    """
    if df is None or sql is None or len(df) < 2 or len(df) > _MAX_REORDER_ROWS:
        return df
    if sql_orders_result(sql):
        return df
    try:
        _, label_col, value_col = infer_chart(df)
        if not value_col or (label_col and is_time_named(label_col)):
            return df
        # mergesort is stable, so rows tied on the measure keep the order the
        # query produced rather than being shuffled.
        return df.sort_values(value_col, ascending=False,
                              kind="mergesort").reset_index(drop=True)
    except Exception:
        return df


def chart_window(chart_df, chart_type: str, label_col: str, value_col: str) -> tuple:
    """Narrow a chartable frame to CHART_MAX_POINTS, and say which slice it is.

    Returns (windowed_df, note) where note is None if nothing was dropped.

    Which END to keep is decided from the FRAME, not from chart_type. Keying it
    on chart_type == "line" looked right and was not: a month axis of '2021-01'
    strings is classified "bar" whenever it fails the line-sortability check,
    so a 61-month series still lost its recent end through the categorical
    path. What matters is whether the axis is a time axis and how it is
    ordered, which is a property of the data.

    - a time axis ascending  -> keep the tail (the newest months)
    - a time axis descending -> keep the head; it is already the newest
    - anything else          -> keep the head

    The note is worded to what the frame actually is. "Top N" asserts a
    ranking, so it is claimed only when the values really do descend; a result
    the SQL ordered by name is just N of M, and a time series is the last N."""
    # A null bucket has no place on an axis. It renders as a bar literally
    # labelled "NaT"/"None" carrying that bucket's real total, and because both
    # pandas and DuckDB sort nulls LAST it lands at the newest end of a time
    # axis — where a reader takes it for the current month. Dropped before the
    # window is chosen so `total` counts only what can actually be charted, and
    # the note stays true to the points rendered.
    if label_col in chart_df:
        chart_df = chart_df[chart_df[label_col].notna()]

    total = len(chart_df)
    if total <= CHART_MAX_POINTS:
        return chart_df, None

    axis = chart_df[label_col] if label_col in chart_df else None
    if chart_type == "line" or (axis is not None and is_time_named(label_col)):
        if axis is None or is_chronological(axis):
            return chart_df.tail(CHART_MAX_POINTS), f"last {CHART_MAX_POINTS} of {total:,}"
        if is_chronological(axis[::-1]):
            # Newest first already: the head IS the recent end.
            return chart_df.head(CHART_MAX_POINTS), f"last {CHART_MAX_POINTS} of {total:,}"

    head = chart_df.head(CHART_MAX_POINTS)
    ranked = bool(head[value_col].is_monotonic_decreasing)
    return head, (f"top {CHART_MAX_POINTS} of {total:,}" if ranked
                  else f"{CHART_MAX_POINTS} of {total:,}")


def fiscal_year_end(year: int, fy_start_month: int = 1):
    """Last calendar day of fiscal `year` for a fiscal year starting in
    `fy_start_month` (Louisville: 7 -> FY2026 ends 2026-06-30)."""
    if fy_start_month == 1:
        return date(year, 12, 31)
    return date(year, fy_start_month, 1) - timedelta(days=1)


def derive_year_facts(newest_year, fy_start_month=1, max_covered=None, grace_days=7, today=None) -> dict:
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
        fy_end = fiscal_year_end(newest_year, fy_start_month)
        cutoff = fy_end - timedelta(days=grace_days)
        # The grace window only forgives the gap between the last business day
        # and the last calendar day — it must never promote a fiscal year that
        # is still running, where spending is genuinely still to come.
        year_is_over = (today or date.today()) > fy_end
        is_partial = covered_through < cutoff.isoformat() or not year_is_over

    last_complete_year = newest_year - 1 if is_partial else newest_year

    if is_partial:
        through = f" — payments are only loaded through {covered_through}" if covered_through else ""
        rules = (
            f"- CRITICAL: FY{newest_year} data is INCOMPLETE{through}, so its totals are "
            f"partial and must not be compared like a full year. When presenting FY{newest_year} "
            f"always say the data is partial. For \"current\" or \"latest\" SPENDING use "
            f"fiscal_year = {last_complete_year}, the most recent year with complete expenditure "
            f"data, unless the user asks about {newest_year}."
        )
        loaded = f" (loaded through {covered_through})" if covered_through else ""
        fact = (
            f"Expenditure data for fiscal year {newest_year} is incomplete{loaded}, so its "
            f"spending totals are partial. FY{last_complete_year} is the most recent fiscal "
            f"year with complete expenditure data — never describe FY{last_complete_year} or "
            "any earlier fiscal year's spending as partial or in progress."
        )
    else:
        rules = (
            f"- FY{newest_year} expenditure data is complete; use fiscal_year = {newest_year} "
            "for \"current\" or \"latest\" SPENDING."
        )
        fact = (
            f"FY{newest_year} is the most recent fiscal year and its expenditure data is "
            "complete — never describe its spending, or any earlier fiscal year's, as partial "
            "or in progress."
        )

    return {
        "is_partial": is_partial,
        "covered_through": covered_through,
        "last_complete_year": last_complete_year,
        "in_progress_year": newest_year if is_partial else None,
        "rules": rules,
        "fact": fact,
    }


def derive_salary_year_facts(newest_cal_year, prior_cal_year) -> dict | None:
    """Salary completeness, judged from the LOADED DATA.

    salary_data carries year-to-date totals with no coverage date, so the
    newest CalYear present is by definition a YTD snapshot — the calendar
    rolling over does not make a stale snapshot complete.

    `prior_cal_year` must be the highest CalYear actually present BELOW the
    newest one. Without it there is no complete year to point at, so this
    returns None and the caller emits no salary guidance — better silent than
    directing the model at a year the table doesn't contain.
    """
    if prior_cal_year is None:
        return None
    return {
        "is_partial": True,
        "last_complete_year": prior_cal_year,
        "rules": (
            f"- CalYear {newest_cal_year} in salary_data is a YEAR-TO-DATE partial year. For "
            f"\"current\" or \"latest\" SALARIES use CalYear = {prior_cal_year}, the most recent "
            f"complete calendar year in the data, unless the user asks about {newest_cal_year}."
        ),
        "fact": (
            f"Salary figures for CalYear {newest_cal_year} are year-to-date and partial; "
            f"{prior_cal_year} is the most recent complete year of salary data."
        ),
    }


def year_context(con, fy_start_month=1, today=None) -> dict:
    """Query year coverage once and build everything the prompts need.

    Shared by the web app and the CLI so the two can't drift apart. Returns
    the substitution values, the derived rule text, and the year fact.
    """
    first_year, newest_year = con.execute(
        "SELECT MIN(fiscal_year), MAX(fiscal_year) FROM expenditures WHERE fiscal_year IS NOT NULL"
    ).fetchone()
    if newest_year is None:
        # No usable fiscal years (empty table / all NULL): say nothing rather
        # than deriving from None. Callers get no rules and no facts.
        log.warning("year_context: no usable fiscal_year values; skipping year guidance")
        return {"values": {}, "rules": "", "facts": [], "expenditures": None,
                "salary": None, "newest_cal_year": None, "salary_error": False,
                # salary_data was never queried on this path — "unknown", not
                # a claim that the pack has no salary table.
                "salary_table_present": None, "salary_state": "unknown"}

    max_covered = con.execute(
        "SELECT MAX(payment_date) FROM expenditures WHERE fiscal_year = ?", [newest_year]
    ).fetchone()[0]
    yf = derive_year_facts(newest_year, fy_start_month, max_covered, today=today)

    rules, facts = [yf["rules"]], [yf["fact"]]
    salary, newest_cal, prior_cal, salary_error = None, None, None, False
    salary_table_present = False

    # Only the QUERIES are guarded, so nothing after a successful read can
    # land in the error state and leave a half-applied rule behind.
    try:
        newest_cal = con.execute("SELECT MAX(CalYear) FROM salary_data").fetchone()[0]
        # The read succeeded, so the table exists — even if it holds no usable
        # CalYear (empty/all-NULL), which is a truncated-CSV symptom and NOT
        # the same thing as a pack without salary data.
        salary_table_present = True
        if newest_cal is not None:
            # The prior year must actually be loaded — pointing the model at a
            # CalYear the table lacks returns zero rows for every salary question.
            prior_cal = con.execute(
                "SELECT MAX(CalYear) FROM salary_data WHERE CalYear < ?", [newest_cal]
            ).fetchone()[0]
    except duckdb.CatalogException:
        pass  # city pack has no salary table — nothing to say about salaries
    except Exception as e:
        # Anything else (renamed column, type error) would silently strip the
        # salary guidance the prompt refers to. Record it as its OWN state:
        # collapsing it into newest_cal=None alone would report "no salary
        # table" for a table that exists and was read a moment ago.
        newest_cal, prior_cal, salary_error = None, None, True
        log.warning("year_context: could not derive salary year facts: %s", e)

    if newest_cal is not None:
        salary = derive_salary_year_facts(newest_cal, prior_cal)
        if salary:
            rules.append(salary["rules"])
            facts.append(salary["fact"])
        else:
            log.warning(
                "salary_data holds only CalYear %s; no complete year to cite, "
                "so no salary year guidance is emitted", newest_cal,
            )

    return {
        "values": {
            "first_year": first_year,
            "newest_year": newest_year,
            "in_progress_year": yf["in_progress_year"],
            "last_complete_year": yf["last_complete_year"],
        },
        "rules": "\n".join(rules),
        "facts": facts,
        "expenditures": yf,
        "salary": salary,
        # salary_state is the single authoritative value — exactly one of
        # "ok" | "error" | "no_table" | "no_years" | "single_year", plus
        # "unknown" from the no-usable-fiscal-year early return above — so
        # consumers never have to combine flags (which were not mutually
        # exclusive) to work out what happened. The keys below remain for
        # detail, not for classification.
        "salary_state": (
            "error" if salary_error
            else "no_table" if not salary_table_present
            else "no_years" if newest_cal is None
            else "single_year" if salary is None
            else "ok"
        ),
        "newest_cal_year": newest_cal,
        "salary_error": salary_error,
        "salary_table_present": salary_table_present,
    }


_TOTAL_LABEL_SHAPES = re.compile(
    r"^(GRAND\s+TOTAL|TOTAL)(\s*[-–:]\s|\s+(ALL|SPENDING|AMOUNT|YEARS)\b|$)"
)


def total_row_mask(df, label_col: str, value_col: str = None):
    """Boolean mask of grand-total rows (e.g. a GROUP BY ROLLUP row).

    One detector, so every surface agrees on what a total is: the chart drops
    them, the row/entity count subtracts them, and the displayed table moves
    them to the end. Two complementary rules, both needed:

    - unambiguous total label shapes ("TOTAL", "TOTAL - ALL GRANT FUNDS",
      "GRAND TOTAL", "TOTAL ALL YEARS") always match;
    - any label merely *containing* "total" matches only when its value also
      equals the sum of the remaining rows — so real payees such as TOTAL TOOL
      SUPPLY INC and TOTAL ACCESS GROUP INC are left alone.

    Pure function (no DB), unit-tested alongside infer_chart.
    """
    if label_col not in df.columns or df.empty:
        return pd.Series(False, index=df.index)
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

    return drop


def drop_total_rows(df, label_col: str, value_col: str = None):
    """``df`` without its grand-total rows. A total bar equals the sum of every
    other bar, so charting it doubles the axis and flattens the real values."""
    if label_col not in df.columns or df.empty:
        return df
    return df[~total_row_mask(df, label_col, value_col)]


def move_totals_to_end(df, label_col: str, value_col: str = None):
    """``(frame with any grand-total rows last, how many were moved)``.

    A ROLLUP total carries the largest number in the frame, so a query that
    ranks by that measure descending puts it in row one — where it reads as the
    biggest item rather than the sum of the others ("the top funding source is
    TOTAL - ALL GRANT FUNDS"). Relocating it is not second-guessing the query's
    ordering the way order_for_display refuses to: a total is not one of the
    ranked rows, it is their sum. Every real row keeps its place relative to
    every other, which is what the ordering actually asserts.

    The count comes back with the frame so a caller cannot describe a move that
    did not happen. It is 0 when EVERY row matched too: reordering is a no-op
    then, and there would be no rows above for the totals to be sums of. A frame
    of nothing but totals is the detector reading labels like "TOTAL - POLICE"
    and "TOTAL - FIRE" as sums when they are really a two-row ranking.
    """
    if label_col not in df.columns or len(df) < 2:
        return df, 0
    mask = total_row_mask(df, label_col, value_col)
    n = int(mask.sum())
    if n == 0 or n == len(df):
        return df, 0
    return pd.concat([df[~mask], df[mask]]).reset_index(drop=True), n


def totals_last(df, label_col: str, value_col: str = None):
    """``df`` with any grand-total rows moved to the end, order otherwise kept."""
    return move_totals_to_end(df, label_col, value_col)[0]


def humanize_text(text: str, table: str = "expenditures", prose: bool = False) -> str:
    """Replace column names with semantic labels in display text.
    Falls back to converting snake_case to Title Case for unknown columns.

    prose=True for running English (an answer), where a column name shaped
    like an ordinary word is overwhelmingly more likely to BE one: the salary
    table's `Other` column turned "Other notable spends" into "Other Pay
    notable spends", and `fund`, `region`, `program`, `project`, `Chief`,
    `Allocation` and `Description` are all the same trap.

    The exemption is by SHAPE, not by "has no underscore" — that earlier
    proxy also spared genuine jargon like `jobTitle`, `CalYear` and
    `LICENSENO`, which then streamed into answers raw ("the LICENSENO for
    that contractor"), the very thing this function exists to remove. Only
    single-case single words (`fund`, `Other`) are left alone; camelCase,
    ALL-CAPS and underscored names are still substituted. Result tables keep
    the full mapping — there a bare `Other` IS the column."""
    # All tables' labels are merged (cross-table results reference several), so
    # `table` selects nothing here today; it is kept only for call-site
    # compatibility. `re` is the module-level import.
    all_flat = {}
    for tbl_labels in ALL_LABELS.values():
        all_flat.update(tbl_labels)
    for col, label in sorted(all_flat.items(), key=lambda x: -len(x[0])):
        if prose and re.fullmatch(r"[A-Za-z][a-z]*", col):
            continue
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


def _main() -> int:
    """CLI for the offline build step: materialize the analytical DB to a file.

    Run in CI (and before a container build) so the serving process opens a
    finished artifact instead of rebuilding from CSV. See LOU_MIGRATION_COMPAT.md.
    """
    import argparse
    import time

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--materialize", metavar="PATH", required=True,
                    help="write the built DuckDB database to this path")
    ap.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "data"),
                    help="directory holding the city pack's CSVs (default: %(default)s)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    t0 = time.time()
    path = build_database(args.materialize, args.data_dir)
    build_s = time.time() - t0

    # Prove the artifact serves before calling the build good: an empty or
    # truncated database opens fine and then answers every question wrong.
    t0 = time.time()
    con = load_prebuilt(path)
    rows = con.execute("SELECT COUNT(*) FROM expenditures").fetchone()[0]
    tables = len(con.execute("SHOW TABLES").fetchall())
    open_s = time.time() - t0
    con.close()

    if not rows:
        print(f"FAILED: {path} has 0 expenditure rows", flush=True)
        return 1
    print(f"Built {path} — {os.path.getsize(path) / 1e6:.0f} MB, {tables} tables, "
          f"{rows:,} expenditure rows (build {build_s:.1f}s, open {open_s:.2f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
