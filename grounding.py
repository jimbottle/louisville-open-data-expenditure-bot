"""Value grounding for NL->SQL: show the model the vocabulary it cannot see.

The compact schema enumerates a categorical column's values only when it has a
dozen or fewer. Louisville's `spend_category` has 979, `fund` 246, `jobTitle`
1,537 — so the model filters on guesses. Observed in production: "how much did
Louisville Fire spend on vehicles" became `spend_category ILIKE '%Vehicle%'`,
which matches nothing, and the answer was "no recorded vehicle spending" when
the real figure was ~$1.6M under 'Automotive Parts & Accessories',
'Automotive Fuel', 'Automotive Repair Services', ...

Two mechanisms, both driven by one small table built at load time
(`_value_index`: table, column, value, weight, rows):

1. Proactive — `grounding_block(con, question)`: the question's content words
   (plus synonyms) are looked up in the index and the matching REAL values are
   appended to the SQL-generation request. No extra LLM call.

2. Reactive — `diagnose_filters(con, sql)`: when a query comes back empty, the
   string literals in its WHERE clause are checked against the index. A literal
   that matches zero values gets a hint listing the closest real ones, and the
   caller regenerates the query once with that hint (the verify-and-repair
   step). A genuinely empty result — every filter matched something — is left
   alone, so the loop never invents data.

City-agnostic: the columns are chosen by a profile of the loaded tables, and
the synonym table is generic English that a city pack extends
(`grounding.synonyms` in city.yaml).
"""

import logging
import re

log = logging.getLogger("grounding")

VALUE_INDEX_TABLE = "_value_index"

# Model-visible wording lives in this module (the block preamble, the repair
# hint). app.py folds this into CACHE_VERSION so an edit here orphans cached
# answers the way a prompt edit does. Bump it when that wording changes.
GROUNDING_VERSION = "grounding-v1"

# Columns that are identifiers, contact details or free text, not vocabulary.
# Indexing an invoice number costs 1.6M useless rows; indexing an address or
# a 800-character project description costs prompt tokens for nothing.
EXCLUDED_COLUMN_PATTERN = re.compile(
    r"number|date|url|link|image|email|phone|address|licenseno|slot|comment|hris|approval|^id$|_id$",
    re.I,
)
DEFAULT_MAX_DISTINCT = 100_000
DEFAULT_MAX_AVG_LENGTH = 80
DEFAULT_MAX_TABLE_ROWS = 500_000

# Words that carry no vocabulary signal for a spending question. Kept broad on
# purpose: a false negative costs one term, a false positive fills the block
# with 'Department of ...' matches for every question that says "department".
STOPWORDS = frozenset("""
a an the and or of for to in on at by from with without into over under per
about across between during since until through within after before as than
that this these those it its is are was were be been being have has had do
does did how what which who whom whose when where why much many more most
less least top bottom largest biggest smallest highest lowest best worst
first last latest recent newest oldest all any each every some several few
list show give tell find get me my our we you your their there here
total totals sum amount amounts spend spent spending expense expenses
expenditure expenditures cost costs paid pay payment payments money dollar
dollars budget budgets fund funding funds year years fiscal fy annual annually
month months quarter time trend trends compare comparison versus vs change
changed changes breakdown break down rank ranking ranked number count
average avg mean median percent percentage share
city metro government govt agency agencies department departments office
public data record records question answer
went go goes going make makes made receive received receives get got gets earn
earns earned run runs ran use used uses using come came take took taken look
looks see know think want need needs work works worked buy buys bought hire
hired allocate allocated award awarded exist exists happen happened involve
true sure verify check include includes included mean means
vendor vendors contractor contractors company companies business businesses
employee employees people person staff position positions job jobs title
titles role roles name names individual individuals transaction transactions
invoice invoices purchase purchases category categories type types kind kinds
source sources item items entity entities organization organizations group
groups line lines detail details information info
""".split())

# Generic English vocabulary the way people ask versus the way ledgers are
# labeled. A city pack extends (never replaces) this via grounding.synonyms.
DEFAULT_SYNONYMS = {
    "vehicle": ["automotive", "fleet", "truck", "auto"],
    "car": ["automotive", "vehicle", "fleet"],
    "truck": ["vehicle", "automotive", "fleet"],
    "fleet": ["automotive", "vehicle"],
    "fuel": ["gasoline", "diesel"],
    "gas": ["fuel", "gasoline", "natural gas"],
    "computer": ["software", "hardware", "technology"],
    "software": ["license", "computer", "cloud"],
    "technology": ["computer", "software", "cloud"],
    "it": ["technology", "computer", "software"],
    "road": ["paving", "street", "asphalt", "pavement"],
    "street": ["road", "paving", "sidewalk"],
    "paving": ["asphalt", "pavement", "road"],
    "sidewalk": ["pavement", "street"],
    "police": ["law enforcement", "officer"],
    "cop": ["police", "officer"],
    "firefighter": ["fire"],
    "salary": ["wage", "pay", "compensation"],
    "wage": ["salary", "pay"],
    "overtime": ["ot"],
    "grant": ["federal", "state aid"],
    "federal": ["grant", "pass thru", "pass-through"],
    "park": ["recreation", "parks"],
    "library": ["libraries"],
    "housing": ["homeless", "shelter", "rental assistance", "affordable"],
    "homeless": ["housing", "shelter"],
    "health": ["medical", "clinic", "wellness"],
    "medical": ["health", "clinic", "ambulance", "ems"],
    "ambulance": ["ems", "medical"],
    "water": ["sewer", "storm", "drainage"],
    "sewer": ["water", "storm", "drainage"],
    "trash": ["garbage", "waste", "solid waste", "refuse", "sanitation"],
    "garbage": ["trash", "waste", "refuse", "sanitation"],
    "waste": ["garbage", "trash", "refuse", "recycling"],
    "recycling": ["waste", "recycle"],
    "bus": ["transit", "transportation"],
    "transit": ["bus", "transportation"],
    "jail": ["corrections", "inmate", "detention"],
    "prison": ["corrections", "inmate", "jail"],
    "inmate": ["corrections", "jail"],
    "court": ["judicial", "legal"],
    "lawyer": ["legal", "attorney", "counsel"],
    "attorney": ["legal", "counsel"],
    "legal": ["attorney", "counsel", "litigation"],
    "insurance": ["liability", "claim"],
    "electricity": ["electric", "utility", "utilities"],
    "electric": ["utility", "utilities"],
    "utility": ["utilities", "electric", "gas", "water"],
    "travel": ["mileage", "lodging", "airfare"],
    "training": ["education", "tuition", "conference"],
    "consultant": ["consulting", "professional services"],
    "construction": ["contractor", "building", "renovation"],
    "building": ["facility", "facilities", "construction"],
    "facility": ["facilities", "building"],
    "rent": ["lease", "rental"],
    "lease": ["rent", "rental"],
    "phone": ["telephone", "cellular", "wireless"],
    "cell": ["cellular", "telephone", "wireless"],
    "internet": ["telecom", "network", "broadband"],
    "supply": ["supplies", "material"],
    "uniform": ["uniforms", "protective gear", "apparel"],
    "equipment": ["machinery", "tools"],
    "tree": ["forestry", "landscaping"],
    "landscaping": ["lawn", "mowing", "grounds"],
    "snow": ["salt", "de-icing", "ice removal"],
    "animal": ["shelter", "veterinary", "vet"],
    "dog": ["animal", "canine"],
    "election": ["voting", "ballot"],
    "school": ["education"],
    "youth": ["children", "kids", "juvenile"],
    "senior": ["elderly", "older adult"],
    "art": ["arts", "cultural", "museum"],
    "tourism": ["convention", "visitor"],
    "airport": ["aviation"],
    "covid": ["cares", "coronavirus", "pandemic", "arp"],
    "pandemic": ["covid", "cares", "coronavirus", "arp"],
    "stimulus": ["arp", "cares", "recovery"],
    "printing": ["print", "copier", "copies"],
    "postage": ["mail", "shipping", "courier"],
    "advertising": ["marketing", "media", "promotion"],
    "security": ["guard", "surveillance"],
    "camera": ["surveillance", "video", "body worn"],
    "drone": ["unmanned", "aerial"],
    "gun": ["firearm", "ammunition", "weapon"],
    "ammunition": ["ammo", "firearm"],
    "hotel": ["lodging"],
    "food": ["meals", "catering", "provisions"],
    "cleaning": ["janitorial", "custodial"],
    "pest": ["exterminat"],
    "elevator": ["lift"],
    "hvac": ["heating", "cooling", "air conditioning"],
    "roof": ["roofing"],
    "bridge": ["bridges"],
    "traffic": ["signal", "signage"],
    "lighting": ["street light", "lights", "lamp"],
    "parking": ["garage", "meter"],
    "bike": ["bicycle", "cycling"],
    "pool": ["aquatic", "swimming"],
    "golf": ["golf course"],
    "cemetery": ["burial"],
    "zoo": ["zoological"],
}

# How many candidate terms a question contributes, and how much of the block
# each may fill. The block goes in every SQL request, so it is held to roughly
# the size of one schema table.
MAX_TERMS = 6
VALUES_PER_GROUP = 6
# A column with this many distinct values is an entity list (payees, employee
# names), not a vocabulary: a topic word matching hundreds of vendor names is
# noise, while a specific name matching a handful is exactly what is wanted.
ENTITY_COLUMN_DISTINCT = 5000
ENTITY_COLUMN_MAX_MATCHES = 12
# The block rides on every SQL request; hold it to about one schema table.
BLOCK_CHAR_BUDGET = 2400


# ── Index construction (runs in the loader, before the DB is locked) ─────────

def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _weight_sql(table: str, amount_column: str | None, columns: set) -> str:
    """The ranking weight for a table's values: its total dollars when it has
    an amount column, else its row count."""
    if amount_column and amount_column in columns:
        expr = f"COALESCE(SUM({_ident(amount_column)}), 0)"
        return expr
    return "COUNT(*)::DOUBLE"


def build_value_index(con, cfg=None) -> int:
    """Materialize `_value_index` from the loaded tables. Returns its row count.

    Column selection is by profile, not by hand: every VARCHAR column of every
    non-internal, non-summary table with 2..max_distinct values and a short
    average length, minus identifier-shaped names and the RAW side of any
    canonicalization (the canonical column covers it). A city pack can add
    `grounding: {include_columns: [...], exclude_columns: [...], max_distinct: N}`
    with entries written as `table.column`.
    """
    spec = _spec(cfg)
    max_distinct = int(spec.get("max_distinct", DEFAULT_MAX_DISTINCT))
    include = set(spec.get("include_columns", []) or [])
    exclude = set(spec.get("exclude_columns", []) or [])
    # The raw column of a canonical pair: its variants are the noise the
    # canonical column exists to remove.
    for c in (getattr(cfg, "canonicalization", None) or []):
        exclude.add(f"{c.get('table', 'expenditures')}.{c['source_column']}")
    dq = getattr(cfg, "data_quality", None) or {}
    amount_table, amount_column = dq.get("table", "expenditures"), dq.get("amount_column")

    con.execute(f"DROP TABLE IF EXISTS {VALUE_INDEX_TABLE}")
    con.execute(
        f"CREATE TABLE {VALUE_INDEX_TABLE} "
        "(tbl VARCHAR, col VARCHAR, val VARCHAR, weight DOUBLE, n BIGINT)"
    )
    indexed = []
    max_rows = int(spec.get("max_table_rows", DEFAULT_MAX_TABLE_ROWS))
    for (table,) in con.execute("SHOW TABLES").fetchall():
        if table.startswith("_"):
            continue
        # Summary tables ARE queried (the prompt sends the model to them), so
        # their vocabularies are indexed too — that is how the repair step can
        # tell that fund = 'ARP' is real in expenditures but absent from
        # summary_grant_funding. A row-level mirror of the main table
        # (summary_largest_payments, 2.1M rows) is skipped: same values, same
        # cost to scan, nothing new to learn.
        if table != amount_table and con.execute(
                f"SELECT COUNT(*) FROM {_ident(table)}").fetchone()[0] > max_rows:
            continue
        cols = con.execute(f"DESCRIBE {_ident(table)}").fetchall()
        names = {c[0] for c in cols}
        for name, typ, *_ in cols:
            key = f"{table}.{name}"
            if key in exclude:
                continue
            if key not in include:
                if "VARCHAR" not in typ.upper() or EXCLUDED_COLUMN_PATTERN.search(name):
                    continue
                q = _ident(name)
                n_distinct, avg_len = con.execute(
                    f"SELECT COUNT(DISTINCT TRIM({q})), AVG(LENGTH({q})) FROM {_ident(table)}"
                ).fetchone()
                if not n_distinct or n_distinct < 2 or n_distinct > max_distinct:
                    continue
                if avg_len is not None and avg_len > DEFAULT_MAX_AVG_LENGTH:
                    continue
            q = _ident(name)
            weight = _weight_sql(table, amount_column if table == amount_table else None, names)
            artifact_filter = (
                " AND is_data_artifact = FALSE"
                if table == amount_table and "is_data_artifact" in names else ""
            )
            con.execute(
                f"INSERT INTO {VALUE_INDEX_TABLE} "
                f"SELECT '{table}', '{name}', TRIM({q}), {weight}, COUNT(*) "
                f"FROM {_ident(table)} WHERE {q} IS NOT NULL AND TRIM({q}) <> ''{artifact_filter} "
                f"GROUP BY TRIM({q})"
            )
            indexed.append(key)
    total = con.execute(f"SELECT COUNT(*) FROM {VALUE_INDEX_TABLE}").fetchone()[0]
    print(f"Value index: {total:,} values across {len(indexed)} columns "
          f"({', '.join(indexed)})")
    return total


def _spec(cfg) -> dict:
    raw = getattr(cfg, "raw", None) or {}
    return raw.get("grounding", {}) or {}


def synonyms_for(cfg=None) -> dict:
    """DEFAULT_SYNONYMS extended by the pack's grounding.synonyms."""
    merged = {k: list(v) for k, v in DEFAULT_SYNONYMS.items()}
    for k, v in (_spec(cfg).get("synonyms", {}) or {}).items():
        merged.setdefault(k.lower(), [])
        for s in (v if isinstance(v, list) else [v]):
            if s.lower() not in [x.lower() for x in merged[k.lower()]]:
                merged[k.lower()].append(s)
    return merged


def stopwords_for(cfg=None) -> frozenset:
    return STOPWORDS | {w.lower() for w in (_spec(cfg).get("stopwords", []) or [])}


# ── Question terms ───────────────────────────────────────────────────────────

def singularize(word: str) -> str:
    """Just enough to match 'vehicles' against 'Vehicle' — not a stemmer."""
    w = word
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("ses", "xes", "zes", "ches", "shes")):
        return w[:-2]
    if len(w) > 4 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def question_terms(question: str, cfg=None) -> list:
    """Content words of a question, singularized, in order of appearance.

    Quoted phrases are kept whole ("Yum Center") in addition to their words,
    since a quoted name is the strongest vocabulary signal a question carries.
    """
    stop = stopwords_for(cfg)
    terms = []

    def add(t):
        t = t.strip().lower()
        if t and t not in terms:
            terms.append(t)

    for phrase in re.findall(r"[\"“”']([^\"“”']{3,60})[\"“”']", question):
        add(phrase)
    # A run of Capitalized Words mid-sentence is a name ("American Rescue
    # Plan", "Yum Center"). Kept as one term — and when the pack knows it
    # (grounding.synonyms), its words are NOT also matched one by one, so
    # "plan" cannot drag in every 'Retirement Plan' line.
    syn = synonyms_for(cfg)
    covered = set()
    for m in re.finditer(r"\b([A-Z][A-Za-z&'-]+(?:\s+[A-Z][A-Za-z&'-]+){1,3})\b", question):
        phrase = m.group(1).lower()
        words = [w for w in phrase.split() if w not in stop]
        if len(words) < 2:
            continue
        add(phrase)
        if phrase in syn:
            covered.update(words)
    for w in re.findall(r"[A-Za-z][A-Za-z&'-]*", question):
        lw = w.lower().strip("'-")
        if not lw or lw in stop or lw in covered:
            continue
        # Short words are noise unless written as an acronym (ARP, LMPD, EMS).
        if len(lw) < 3 or (len(lw) == 3 and not w.isupper() and lw not in DEFAULT_SYNONYMS):
            continue
        # An acronym is not a plural: CARES must stay 'cares', never 'care'.
        add(lw if w.isupper() else singularize(lw))
    return terms[:MAX_TERMS]


# ── Lookup ───────────────────────────────────────────────────────────────────

def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# A token this short is an acronym or a short word (ARP, EMS, OT) and is
# matched on word boundaries; '%arp%' would also match Carpet and Sharp.
BOUNDARY_TOKEN_LENGTH = 3


def _token_predicate(token: str) -> tuple:
    """(sql_predicate_on_val, params) for one vocabulary token."""
    if len(token) <= BOUNDARY_TOKEN_LENGTH:
        return "regexp_matches(val, ?)", [r"(?i)\b" + re.escape(token) + r"\b"]
    return "val ILIKE ? ESCAPE '\\'", [f"%{_like_escape(token)}%"]


def _token_pattern_sql(column: str, token: str) -> str:
    """The same test written for the model's SQL, against a real column."""
    if len(token) <= BOUNDARY_TOKEN_LENGTH:
        return f"regexp_matches({column}, '(?i)\\b{token}\\b')"
    return f"{column} ILIKE '%{token}%'"


def _fmt_weight(weight, dollars: bool) -> str:
    if weight is None:
        return ""
    if not dollars:
        return f"{int(weight):,} rows"
    w = float(weight)
    a = abs(w)
    if a >= 1e9:
        return f"${w / 1e9:.1f}B"
    if a >= 1e6:
        return f"${w / 1e6:.1f}M"
    if a >= 1e3:
        return f"${w / 1e3:.1f}K"
    return f"${w:,.0f}"


def _dollar_tables(cfg) -> set:
    dq = getattr(cfg, "data_quality", None) or {}
    return {dq.get("table", "expenditures")} if dq.get("amount_column") else set()


def _column_sizes(con) -> dict:
    return {(t, c): n for t, c, n in con.execute(
        f"SELECT tbl, col, COUNT(*) FROM {VALUE_INDEX_TABLE} GROUP BY tbl, col"
    ).fetchall()}


def lookup_terms(con, terms: list, cfg=None, per_group: int | None = None,
                 only_synonyms: bool = False) -> list:
    """Index matches for each term (and its synonyms).

    Returns [{term, tokens, table, column, total, values: [(val, weight, n)]}]
    — one entry per (term, table, column) group. Groups from the primary
    (dollar) table come first, the most specific (fewest matches) first within
    it; an entity column (see ENTITY_COLUMN_DISTINCT) contributes a group only
    when the term picks out a handful of its values.

    only_synonyms: look up the term's synonyms but not the term itself — what
    the repair step needs to learn whether a literal that DID match is merely
    the narrow corner of a wider family.
    """
    if not terms or not _index_exists(con):
        return []
    per_group = per_group or VALUES_PER_GROUP
    syn = synonyms_for(cfg)
    sizes = _column_sizes(con)
    primary = (getattr(cfg, "data_quality", None) or {}).get("table", "expenditures")
    per_term = []
    for term in terms:
        tokens = [s for s in syn.get(term, []) if s != term]
        if not only_synonyms:
            tokens = [term] + tokens
        if not tokens:
            continue
        preds = [_token_predicate(t) for t in tokens]
        # Which tokens each value matched, so the block can say "also matched:
        # automotive, fleet" and build a covering pattern from real hits only.
        matched_expr = " || ".join(
            f"(CASE WHEN {sql} THEN '|' || ? ELSE '' END)" for sql, _ in preds
        )
        where = " OR ".join(sql for sql, _ in preds)
        params = [x for (sql, ps), t in zip(preds, tokens) for x in (*ps, t)] \
            + [x for _, ps in preds for x in ps]
        rows = con.execute(
            f"""
            WITH hits AS (
                SELECT tbl, col, val, weight, n, {matched_expr} AS matched
                FROM {VALUE_INDEX_TABLE}
                WHERE {where}
            ), ranked AS (
                SELECT *,
                       COUNT(*) OVER (PARTITION BY tbl, col) AS total,
                       ROW_NUMBER() OVER (PARTITION BY tbl, col
                                          ORDER BY weight DESC NULLS LAST, n DESC, val) AS rn
                FROM hits
            )
            SELECT tbl, col, val, weight, n, total, matched
            FROM ranked WHERE rn <= ?
            ORDER BY tbl, col, rn
            """,
            params + [per_group],
        ).fetchall()
        groups = {}
        for tbl, col, val, weight, n, total, matched in rows:
            if sizes.get((tbl, col), 0) > ENTITY_COLUMN_DISTINCT and total > ENTITY_COLUMN_MAX_MATCHES:
                continue
            g = groups.setdefault((tbl, col), {
                "term": term, "tokens": [], "table": tbl, "column": col,
                "total": int(total), "values": [],
            })
            g["values"].append((val, weight, int(n)))
            for tok in (matched or "").split("|"):
                if tok and tok not in g["tokens"]:
                    g["tokens"].append(tok)
        # Within a term: the dollar table first, then by the single heaviest
        # value (so fund = 'ARP' at $37.6M leads 84 small ARP grant lines).
        # The block then round-robins ACROSS terms so a common word ("fire")
        # cannot starve the one that carries the question ("vehicle").
        per_term.append(sorted(
            groups.values(),
            key=lambda g: (g["table"] != primary,
                           -max((w or 0) for _, w, _ in g["values"]), g["column"]),
        ))
    out = []
    for i in range(max((len(t) for t in per_term), default=0)):
        for t in per_term:
            if i < len(t):
                out.append(t[i])
    return out


def _index_exists(con) -> bool:
    try:
        return bool(con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
            [VALUE_INDEX_TABLE],
        ).fetchall())
    except Exception:
        return False


def format_groups(groups: list, cfg=None, budget: int | None = None) -> str:
    budget = budget or BLOCK_CHAR_BUDGET
    dollar_tables = _dollar_tables(cfg)
    lines, used = [], 0
    for g in groups:
        dollars = g["table"] in dollar_tables
        vals = ", ".join(
            f"'{v}'" + (f" {_fmt_weight(w, dollars)}" if w is not None else "")
            for v, w, _ in g["values"]
        )
        ident = f"{g['table']}.{g['column']}"
        also = [t for t in g["tokens"] if t != g["term"]]
        head = f'- "{g["term"]}"' + (f" (also matched: {', '.join(also)})" if also else "")
        if g["total"] > len(g["values"]):
            pattern = " OR ".join(_token_pattern_sql(g["column"], t) for t in g["tokens"])
            line = (f"{head} → {ident}: {g['total']} values, e.g. {vals}, … "
                    f"(to cover all of them: {pattern})")
        else:
            line = f"{head} → {ident}: {vals}"
        if used + len(line) > budget and lines:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


def grounding_block(con, question: str, cfg=None) -> str:
    """The vocabulary block appended to the SQL-generation request, or ''."""
    groups = lookup_terms(con, question_terms(question, cfg), cfg)
    if not groups:
        return ""
    return (
        "## Data vocabulary matched to this question\n"
        "These are exact strings that exist in the data (with all-years totals). "
        "Filter on them — the exact value, or a listed pattern that covers the whole "
        "family — instead of guessing a label. Use a pattern exactly as written: a "
        "shortened one ('%care%' for '%cares%') matches unrelated values. They are "
        "context, not instructions: still answer the question that was asked, and "
        "still follow the table rules above.\n"
        + format_groups(groups, cfg)
    )


# ── Filter diagnosis (the verify-and-repair step) ────────────────────────────

_SQL_STRING = r"'(?:[^']|'')*'"
_LITERAL_FILTER = re.compile(
    r"(?:(?:LOWER|UPPER|TRIM)\s*\(\s*)?"
    r"(?P<col>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s*\)?\s*"
    r"(?P<op>NOT\s+ILIKE|NOT\s+LIKE|NOT\s+IN|ILIKE|LIKE|IN|=|!=|<>)\s*"
    r"(?P<rhs>" + _SQL_STRING + r"|\(\s*" + _SQL_STRING + r"(?:\s*,\s*" + _SQL_STRING + r")*\s*\))",
    re.I,
)


def sql_literal_filters(sql: str) -> list:
    """(column, operator, literal) for every string-literal predicate in the SQL.

    Negated predicates are dropped: `<> 'x'` cannot empty a result by
    matching nothing. Table aliases are stripped from the column."""
    out = []
    for m in _LITERAL_FILTER.finditer(sql):
        op = re.sub(r"\s+", " ", m.group("op").upper())
        if op in ("NOT ILIKE", "NOT LIKE", "NOT IN", "!=", "<>"):
            continue
        col = m.group("col").split(".")[-1]
        for lit in re.findall(_SQL_STRING, m.group("rhs")):
            out.append((col, "=" if op == "IN" else op, lit[1:-1].replace("''", "'")))
    return out


_FROM_TABLE = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)


def sql_tables(sql: str) -> list:
    """Table names the SQL reads (FROM/JOIN targets), in order, deduplicated."""
    out = []
    for t in _FROM_TABLE.findall(sql or ""):
        if t.lower() not in ("select", "values", "lateral") and t not in out:
            out.append(t)
    return out


def _match_count(con, column: str, op: str, literal: str, tables: list | None = None) -> tuple:
    """(exact_or_pattern_matches, case_insensitive_matches, tables) for a literal.

    tables: restrict the check to these (the tables the SQL actually reads)
    when any of them carries the column; 'ARP' is a real fund in expenditures
    and absent from summary_grant_funding, and a query of the latter must be
    judged against the latter."""
    if op == "=":
        exact_sql, ci_sql = "val = ?", "val ILIKE ? ESCAPE '\\'"
        exact_arg, ci_arg = literal, _like_escape(literal)
    elif op == "LIKE":
        exact_sql, ci_sql = "val LIKE ?", "val ILIKE ?"
        exact_arg, ci_arg = literal, literal
    else:  # ILIKE
        exact_sql, ci_sql = "val ILIKE ?", "val ILIKE ?"
        exact_arg, ci_arg = literal, literal
    scope, scope_args = _table_scope(con, column, tables)
    row = con.execute(
        f"SELECT COUNT(*) FILTER (WHERE {exact_sql}), COUNT(*) FILTER (WHERE {ci_sql}), "
        f"LIST(DISTINCT tbl) FROM {VALUE_INDEX_TABLE} WHERE col = ?{scope}",
        [exact_arg, ci_arg, column] + scope_args,
    ).fetchone()
    return int(row[0]), int(row[1]), list(row[2] or [])


def _table_scope(con, column: str, tables: list | None) -> tuple:
    """SQL fragment (and args) restricting an index lookup to the SQL's own
    tables — only when at least one of them is indexed for this column, so an
    unindexed table falls back to the whole index rather than to nothing."""
    if not tables:
        return "", []
    present = [t for t in tables if con.execute(
        f"SELECT 1 FROM {VALUE_INDEX_TABLE} WHERE col = ? AND tbl = ? LIMIT 1", [column, t]).fetchone()]
    if not present:
        return "", []
    return " AND tbl IN (" + ", ".join("?" * len(present)) + ")", present


def _literal_terms(literal: str, cfg=None) -> list:
    """The content words of a SQL literal ('%Vehicle%' -> ['vehicle'])."""
    stop = stopwords_for(cfg)
    terms = []
    for w in re.findall(r"[A-Za-z][A-Za-z&'-]+", literal):
        lw = singularize(w.lower())
        if lw not in stop and len(lw) >= 3 and lw not in terms:
            terms.append(lw)
    return terms


def _suggestions(con, column: str, literal: str, cfg=None, limit: int = 8, tables=None) -> tuple:
    """Real values near a literal that matched nothing: by shared words (with
    synonyms), then by string similarity for typos. Returns (values, tokens).
    Scoped to `tables` (the SQL's own) when given."""
    scope, scope_args = _table_scope(con, column, tables)
    terms = _literal_terms(literal, cfg)
    syn = synonyms_for(cfg)
    tokens = []
    for t in terms:
        for tok in [t] + syn.get(t, []):
            if tok not in tokens:
                tokens.append(tok)
    found, matched_tokens = [], []
    if tokens:
        preds = [_token_predicate(t) for t in tokens]
        where = " OR ".join(sql for sql, _ in preds)
        rows = con.execute(
            f"SELECT val, weight FROM {VALUE_INDEX_TABLE} WHERE col = ?{scope} AND ({where}) "
            f"ORDER BY weight DESC NULLS LAST, n DESC LIMIT ?",
            [column] + scope_args + [x for _, ps in preds for x in ps] + [limit],
        ).fetchall()
        found = [(v, w) for v, w in rows]
        if found:
            for tok, (sql, ps) in zip(tokens, preds):
                hit = con.execute(
                    f"SELECT 1 FROM {VALUE_INDEX_TABLE} WHERE col = ?{scope} AND {sql} LIMIT 1",
                    [column] + scope_args + ps,
                ).fetchone()
                if hit:
                    matched_tokens.append(tok)
    if len(found) < limit:
        clean = re.sub(r"[%_]", "", literal).strip()
        if clean:
            rows = con.execute(
                f"SELECT val, weight FROM {VALUE_INDEX_TABLE} WHERE col = ?{scope} "
                f"AND jaro_winkler_similarity(LOWER(val), LOWER(?)) > 0.85 "
                f"ORDER BY jaro_winkler_similarity(LOWER(val), LOWER(?)) DESC, weight DESC LIMIT ?",
                [column] + scope_args + [clean, clean, limit - len(found)],
            ).fetchall()
            seen = {v for v, _ in found}
            found.extend((v, w) for v, w in rows if v not in seen)
    return found, matched_tokens


def _elsewhere(con, column: str, literal: str, cfg=None, limit: int = 5, tables=None) -> list:
    """Other columns — or the same column in a table the SQL did not read —
    holding values that match the literal's words: a fund named in an agency
    filter, a job title used as a department, ARP looked for in a summary
    table that does not carry it."""
    words = [singularize(w.lower()) for w in re.findall(r"[A-Za-z][A-Za-z&'-]+", literal)]
    words = [w for w in words if len(w) >= 3 and w not in stopwords_for(cfg)]
    if not words:
        return []
    preds = [_token_predicate(w) for w in words]
    where = " OR ".join(sql for sql, _ in preds)
    patterns = [x for _, ps in preds for x in ps]
    # Filter in one CTE, window in the next — deliberately. DuckDB 1.5.1
    # returns tbl and col SWAPPED (with a correct description) when a
    # `col <> ?` predicate shares a SELECT with a window partitioned by col;
    # see tests/test_grounding.py::test_elsewhere_returns_table_column_value.
    return con.execute(
        f"""
        WITH hits AS (
            SELECT tbl, col, val, weight FROM {VALUE_INDEX_TABLE}
            WHERE (col <> ?{" OR tbl NOT IN (" + ", ".join("?" * len(tables)) + ")" if tables else ""}) AND ({where})
        ), ranked AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY tbl, col ORDER BY weight DESC NULLS LAST) AS rn
            FROM hits
        )
        SELECT tbl, col, val FROM ranked WHERE rn <= 2
        ORDER BY weight DESC NULLS LAST LIMIT ?
        """,
        [column] + list(tables or []) + patterns + [limit],
    ).fetchall()


_YEAR_EQ = re.compile(r"\b(fiscal_year|calyear|cal_year|year)\s*=\s*(\d{4})\b", re.I)


def _year_gap(con, table: str, column: str, op: str, literal: str, sql: str) -> dict | None:
    """When the SQL pins a year and the literal IS real, find out whether the
    value simply has no rows in that year: fund = 'ARP' exists, FY2025 does
    not have it, and the question never named a year. Returns
    {year_col, year, first, last} or None."""
    m = _YEAR_EQ.search(sql)
    if not m:
        return None
    year_col, year = m.group(1), int(m.group(2))
    try:
        cols = {c[0].lower(): c[0] for c in con.execute(f"DESCRIBE {_ident(table)}").fetchall()}
    except Exception:
        return None
    if year_col.lower() not in cols or column.lower() not in cols:
        return None
    ycol, vcol = _ident(cols[year_col.lower()]), _ident(cols[column.lower()])
    pred = {"=": f"{vcol} = ?", "LIKE": f"{vcol} LIKE ?", "ILIKE": f"{vcol} ILIKE ?"}[op]
    row = con.execute(
        f"SELECT MIN({ycol}), MAX({ycol}), COUNT(*) FILTER (WHERE {ycol} = ?) "
        f"FROM {_ident(table)} WHERE {pred}", [year, literal]).fetchone()
    if row is None or row[0] is None or row[2]:
        return None  # has rows in that year (or none at all): not a year gap
    return {"year_col": cols[year_col.lower()], "year": year, "first": int(row[0]), "last": int(row[1])}


def _question_names_year(question: str, year: int) -> bool:
    q = question or ""
    return bool(re.search(rf"\b{year}\b", q) or re.search(rf"\bFY\s?{str(year)[-2:]}\b", q, re.I))


def diagnose_filters(con, sql: str, cfg=None, question: str = "") -> list:
    """For each string-literal filter in the SQL that matches NO indexed value,
    a diagnosis: {column, op, literal, total, suggestions, tokens, elsewhere,
    case_only}. Empty when every filter matched (or none is checkable), which
    tells the caller the empty result is genuine."""
    if not _index_exists(con):
        return []
    tables = sql_tables(sql)
    _tables = tables
    out = []
    seen = set()
    for col, op, lit in sql_literal_filters(sql):
        if (col, op, lit) in seen:
            continue
        seen.add((col, op, lit))
        scope, scope_args = _table_scope(con, col, tables)
        total = con.execute(
            f"SELECT COUNT(*) FROM {VALUE_INDEX_TABLE} WHERE col = ?{scope}", [col] + scope_args
        ).fetchone()[0]
        if not total:
            continue  # not an indexed column — nothing to say about it
        exact, ci, _tables = _match_count(con, col, op, lit, tables)
        d = {"column": col, "op": op, "literal": lit, "total": int(total), "matched": exact,
             "suggestions": [], "tokens": [], "elsewhere": [], "case_only": False, "narrow": False}
        if exact:
            # The literal is real, yet the query was empty. Two things can
            # still be said. (1) The SQL pinned a year the value has no rows
            # in (fund = 'ARP' AND fiscal_year = 2025 — ARP ran FY2021-24).
            # (2) Its synonyms reach a wider family in the same column, so the
            # filter was the narrow corner of that family ('%Vehicle%' beside
            # 51 Automotive/Fleet/Truck categories). Otherwise the emptiness
            # is genuine and there is nothing to say.
            # Only when the QUESTION did not name the year: a reader asking
            # about FY2031 must be told the data stops at FY2026, not handed
            # an all-years figure with the year quietly dropped.
            for t in (scope_args or _tables):
                gap = _year_gap(con, t, col, op, lit, sql)
                if gap and not _question_names_year(question, gap["year"]):
                    d.update(gap, year_gap=True)
                    out.append(d)
                    break
            if d.get("year_gap"):
                continue
            family = [g for g in lookup_terms(con, _literal_terms(lit, cfg), cfg, only_synonyms=True)
                      if g["column"] == col and (not scope_args or g["table"] in scope_args)]
            if not family:
                continue
            d["narrow"] = True
            seen, vals, toks = set(), [], []
            for g in family:
                for v, w, _ in g["values"]:
                    if v not in seen:
                        seen.add(v)
                        vals.append((v, w))
                for t in g["tokens"]:
                    if t not in toks:
                        toks.append(t)
            d["suggestions"], d["tokens"] = vals[:8], toks
            d["family_total"] = sum(g["total"] for g in family)
            out.append(d)
            continue
        if ci:
            d["case_only"] = True
            d["suggestions"] = con.execute(
                f"SELECT val, weight FROM {VALUE_INDEX_TABLE} WHERE col = ? AND val ILIKE ? ESCAPE '\\' "
                f"ORDER BY weight DESC NULLS LAST LIMIT 8",
                [col, _like_escape(lit) if op == "=" else lit],
            ).fetchall()
        else:
            d["suggestions"], d["tokens"] = _suggestions(con, col, lit, cfg, tables=scope_args)
            if not d["suggestions"]:
                d["elsewhere"] = _elsewhere(con, col, lit, cfg, tables=scope_args)
        out.append(d)
    return out


def format_repair_hint(diagnoses: list, cfg=None, instruct: bool = True) -> str:
    """The correction handed back to the model (and, in dev mode, shown).

    instruct=False leaves off the "rewrite the query" instruction, for when
    the findings are given to the model that EXPLAINS an empty result rather
    than the one asked to fix it."""
    if not diagnoses:
        return ""
    dollar_tables = _dollar_tables(cfg)
    lines = ["The query ran but at least one of its filters matches nothing in the data:"]
    for d in diagnoses:
        col, lit, op = d["column"], d["literal"], d["op"]
        shown = f"{col} {op} '{lit}'"
        if d["case_only"]:
            vals = ", ".join(f"'{v}'" for v, _ in d["suggestions"])
            lines.append(f"- {shown}: no match with that capitalization; the real value(s): {vals}.")
            continue
        if d.get("year_gap"):
            lines.append(
                f"- {shown} is a real value, but it has no rows in {d['year_col']} = {d['year']}: "
                f"its rows run {d['year_col']} {d['first']}-{d['last']}. The question did NOT ask "
                f"for {d['year']} — that filter was added by the query, not the reader. REMOVE the "
                f"{d['year_col']} = {d['year']} predicate and query all years (the data has no "
                f"other reading of the question; the \"return it unchanged\" exception below does "
                f"not apply here)."
            )
            continue
        if d.get("narrow"):
            vals = ", ".join(
                f"'{v}'" + (f" {_fmt_weight(w, True)}" if w is not None and dollar_tables else "")
                for v, w in d["suggestions"]
            )
            pattern = " OR ".join(_token_pattern_sql(col, t) for t in d["tokens"])
            lines.append(
                f"- {shown} matches {d['matched']} {col} value(s), but the query found nothing "
                f"with it. The same topic appears under {d.get('family_total', 0)} other {col} "
                f"values, e.g. {vals}. A pattern covering the whole family: {pattern} OR {shown}."
            )
            continue
        head = f"- {shown} matches 0 of {d['total']} {col} values."
        if d["suggestions"]:
            vals = ", ".join(
                f"'{v}'" + (f" {_fmt_weight(w, True)}" if w is not None and dollar_tables else "")
                for v, w in d["suggestions"]
            )
            head += f" Closest real values: {vals}."
            if d["tokens"]:
                pattern = " OR ".join(_token_pattern_sql(col, t) for t in d["tokens"])
                head += f" A pattern covering the whole family: {pattern}."
        elif d["elsewhere"]:
            alt = ", ".join(f"{t}.{c} = '{v}'" for t, c, v in d["elsewhere"])
            head += f" Nothing similar in {col}; matching values exist in other columns: {alt}."
        else:
            head += " Nothing similar exists in that column."
        lines.append(head)
    if not instruct:
        return "\n".join(lines)
    lines.append(
        "Rewrite the query using these real values (prefer the covering pattern when "
        "several values belong together). Keep everything else — the year, the "
        "department, the aggregation — exactly as the question asked. If the data "
        "genuinely has nothing for the question, return the original query unchanged. "
        "Return ONLY the SQL."
    )
    return "\n".join(lines)
