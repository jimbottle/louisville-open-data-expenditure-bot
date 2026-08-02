#!/usr/bin/env python3
"""Seed draft canonical maps for a city config pack.

Canonicalization (docs/canonical-model.md §5) collapses the many spellings of
one agency or vendor into a single canonical name. Hand-curating those maps is
the dominant per-city onboarding cost (docs/cincinnati-onboarding.md), so this
tool does the mechanical 90%: normalize case/punctuation/corporate suffixes,
cluster the variants that normalize alike, pick a canonical label, and write a
draft CSV in the exact two-column shape the engine loads.

The draft is a starting point for human/LLM curation, not a finished map. Two
deliberate conservatisms keep it safe to hand to a curator:

  * Only variants whose *normalized keys are identical* are merged into the
    map. Merely similar names (LOUISVILLE GAS AND ELE vs LOUISVILLE GAS AND
    ELECTRIC) go to the review report as suggestions — a wrong merge silently
    corrupts every total downstream, and no similarity threshold is worth that.
  * An existing map is never overwritten without --force; the draft lands at
    <map>.draft.csv so a curated map can't be clobbered by a re-run.

Usage:
    python canonical_seed.py --city cities/cincinnati/city.yaml \\
        --dimension payee --data-dir data_cincinnati
    python canonical_seed.py --city ... --dimension agency --report -
"""

import argparse
import csv
import difflib
import os
import re
import sys
from collections import defaultdict

# Trailing tokens that carry no identity: "ACME INC" and "ACME" are one vendor.
# Repeatedly stripped from the end, so "ACME CO INC" reduces to "ACME".
CORPORATE_SUFFIXES = {
    "INC", "INCORPORATED", "LLC", "LLP", "LP", "LTD", "LIMITED", "CO",
    "COMPANY", "CORP", "CORPORATION", "PLLC", "PLC", "PC", "PA", "DBA",
}

# Words that must not be title-cased when a canonical label is derived from an
# ALL-CAPS source. Acronyms stay upper; joiners go lower unless they lead.
# INC/CORP/CO/LTD are deliberately absent — they read as words ("Humana Inc"),
# not as initialisms.
ACRONYMS = {
    "LLC", "LLP", "LP", "PLLC", "PLC", "PC", "PA", "USA", "US",
    "HVAC", "IT", "EMS", "EMT", "HR", "PD", "FD", "DBA", "II", "III", "IV",
    "V", "VI", "VII", "VIII", "IX", "X", "AT&T", "LG&E", "UPS", "IBM", "HP",
    "3M", "CDW", "ADT", "GE", "TV", "AV", "PPE", "ID", "GIS", "DNA",
}
# "a"/"an" are deliberately absent: a lone A mid-name is almost always an
# initial ("A & A SAFETY"), and lowercasing it reads as a typo.
JOINERS = {"of", "and", "the", "for", "to", "in", "on", "at", "or", "de"}

# Store/branch codes: "WALGREENS #4821", "CDW GOVT #". The number identifies a
# location, not a vendor, so it is dropped before clustering — and its presence
# is what makes a value a prefix-rule candidate.
#
# Anchored to the END on purpose. A mid-name "#" is part of the identity, not a
# branch: real payees like "BETHEL #2 APOSTOLIC PENTECOSTAL CHURCH" would
# otherwise collapse into their #3 and #4 namesakes, silently merging distinct
# organizations' spend.
STORE_CODE = re.compile(r"\s*#\s*\d*\s*$")


def normalize(name: str) -> str:
    """Identity key for a raw name. Two names with the same key are the same
    entity as far as this tool will assert without human review."""
    if name is None:
        return ""
    s = str(name).upper()
    s = s.replace(".", "")            # L.L.C. -> LLC, before punctuation split
    s = s.replace("&", " AND ")
    s = STORE_CODE.sub(" ", s)
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    tokens = s.split()
    if tokens and tokens[0] == "THE":
        tokens = tokens[1:]
    while len(tokens) > 1 and tokens[-1] in CORPORATE_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _looks_like_acronym(name: str) -> bool:
    if len(name.split()) != 1 or not 2 <= len(name) <= 5 or not name.isalpha():
        return False
    return sum(c in "AEIOU" for c in name) / len(name) < 0.5


def smart_title(name: str) -> str:
    """Title-case an ALL-CAPS name, leaving mixed-case names alone.

    Portal exports are usually shouted; a canonical label that reads like a
    name is the point of curation. Anything already carrying lowercase is
    treated as intentionally cased and returned untouched."""
    if not name or any(c.islower() for c in name):
        return name
    # A short, vowel-poor single token is an acronym the ACRONYMS set has
    # never heard of (APCD, LMPD). Title-casing it produces "Apcd", which a
    # curator would only undo. Length alone is not the signal — HUMANA and
    # KROGER are words and do want title case — so vowel density decides.
    # It errs toward leaving short shouts alone: a five-letter surname vendor
    # ("SMITH") stays shouted, which a curator fixes in seconds, while the
    # opposite error turns every municipal acronym into "Apcd"/"Lmpd".
    if _looks_like_acronym(name):
        return name
    out = []
    for i, word in enumerate(name.split()):
        bare = word.strip(".,")
        if bare in ACRONYMS:
            out.append(word)
        elif i and word.lower() in JOINERS:
            out.append(word.lower())
        else:
            out.append(word.title().replace("'S", "'s"))
    return " ".join(out)


class Cluster:
    """One canonical entity and the raw spellings that map to it."""

    def __init__(self, key: str, members: dict):
        self.key = key
        self.members = members            # raw value -> weight
        self.weight = sum(members.values())
        self.canonical = smart_title(self.winner())

    def winner(self) -> str:
        """Heaviest raw spelling, deterministically tie-broken.

        Weight (dollars, or rows when no amount column exists) picks the
        spelling the city itself uses most; the alphabetical tie-break keeps
        the output byte-identical across runs, which matters because these
        files are committed and diffed."""
        return max(self.members.items(), key=lambda kv: (kv[1], kv[0]))[0]

    def is_trivial(self) -> bool:
        """Nothing for a curator to gain: one spelling, already canonical."""
        return len(self.members) == 1 and next(iter(self.members)) == self.canonical


def cluster_values(weights: dict) -> list:
    """Group raw values by normalized key, heaviest cluster first."""
    groups = defaultdict(dict)
    for raw, weight in weights.items():
        if raw is None or not str(raw).strip():
            continue
        key = normalize(raw)
        if not key:
            continue
        groups[key][raw] = groups[key].get(raw, 0) + weight
    clusters = [Cluster(k, m) for k, m in groups.items()]
    clusters.sort(key=lambda c: (-c.weight, c.key))
    return clusters


def prefix_candidates(clusters: list) -> list:
    """Raw values holding a store/branch code, grouped by the prefix a
    prefix_map rule would key on ("WALGREENS #" -> Walgreens).

    Returns (prefix, canonical, [raw values]) sorted by weight. These are
    suggestions only: a prefix rule is greedier than an exact row and is the
    curator's call."""
    groups = defaultdict(lambda: {"raws": {}, "canon": None, "weight": 0.0})
    for c in clusters:
        for raw, weight in c.members.items():
            m = STORE_CODE.search(str(raw))
            if not m:
                continue
            prefix = str(raw)[: str(raw).index("#", m.start()) + 1]
            g = groups[prefix.upper()]
            g["raws"][raw] = weight
            g["weight"] += weight
            # Attribute the prefix to the heaviest cluster it feeds.
            if g["canon"] is None or c.weight > g["canon"][1]:
                g["canon"] = (c.canonical, c.weight)
    out = [
        (prefix, g["canon"][0], sorted(g["raws"]), g["weight"])
        for prefix, g in groups.items()
    ]
    out.sort(key=lambda t: (-t[3], t[0]))
    return out


def near_duplicates(clusters: list, threshold: float = 0.86, limit: int = 400) -> list:
    """Pairs of distinct clusters similar enough to be worth a human look.

    Blocked on the first four characters of the normalized key so this stays
    linear-ish on the tens of thousands of distinct payees a mid-size city has;
    only the `limit` heaviest clusters are compared, because the point is to
    aim curation at the dollars. Both bounds are reported by the caller — a
    silent cap would read as "nothing else to merge"."""
    head = clusters[:limit]
    blocks = defaultdict(list)
    for c in head:
        blocks[c.key[:4]].append(c)
    pairs = []
    for group in blocks.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                ratio = difflib.SequenceMatcher(None, a.key, b.key).ratio()
                if ratio >= threshold:
                    pairs.append((round(ratio, 3), a, b))
    pairs.sort(key=lambda t: (-(t[1].weight + t[2].weight), t[1].key, t[2].key))
    return pairs


# Tokens that a writer drops when forming an initialism: nobody abbreviates
# "Air Pollution Control District" with an A for "and".
INITIALISM_SKIP = {"AND", "OF", "THE", "FOR", "TO", "IN", "ON", "AT"}


def _initials(key: str) -> str:
    return "".join(t[0] for t in key.split() if t not in INITIALISM_SKIP)


def _as_initialism(key: str) -> str:
    """The letters a short key contributes, if it looks like an initialism."""
    tokens = [t for t in key.split() if t not in INITIALISM_SKIP]
    letters = "".join(tokens)
    if 2 <= len(letters) <= 6 and letters.isalpha() and len(tokens) <= 3:
        return letters
    return ""


def initialism_candidates(clusters: list, limit: int = 400) -> list:
    """Short clusters that spell out a longer cluster's initials.

    This is the *semantic* half of curation — APCD vs Air Pollution Control
    District, LG&E vs Louisville Gas & Electric — and it is the half that
    dominated Louisville's hand-built maps, because no amount of case and
    punctuation normalization can reach it. Suggestions only: an initialism
    match is evidence, not proof (PW could be Public Works or Parks & Waste),
    so a human or LLM confirms before these become map rows."""
    head = clusters[:limit]
    long_by_initials = defaultdict(list)
    for c in head:
        if len(c.key.split()) >= 2:
            long_by_initials[_initials(c.key)].append(c)
    out = []
    for short in head:
        letters = _as_initialism(short.key)
        if not letters:
            continue
        for long in long_by_initials.get(letters, []):
            if long.key != short.key:
                out.append((short, long))
    out.sort(key=lambda t: (-(t[0].weight + t[1].weight), t[0].key, t[1].key))
    return out


def residual_clusters(clusters: list, already_suggested: set, top: int = 40) -> list:
    """Heaviest clusters the seeder neither merged nor had a suggestion for.

    Everything automatic is already in the draft map and everything the tool
    had a hunch about is in the suggestion sections; this is the true
    remainder, biggest dollars first, and it is where a curator's time is
    actually worth spending."""
    return [c for c in clusters
            if c.key not in already_suggested and len(c.members) == 1][:top]


def draft_rows(clusters: list, min_cluster: int = 2) -> list:
    """(source, canonical) rows for the draft map.

    Every member of a kept cluster gets a row, including the winner: the
    canonical label often differs from the raw spelling only in case, and the
    engine's exact map is a literal lookup, so an omitted identity row would
    leave that spelling un-canonicalized."""
    rows = []
    for c in clusters:
        if len(c.members) < min_cluster or c.is_trivial():
            continue
        for raw in sorted(c.members):
            rows.append((raw, c.canonical))
    return rows


def load_weights(data_dir: str, cfg, table: str, column: str, amount: str = None) -> tuple:
    """(raw value -> total spend or row count, unit name) from a city's data.

    Weighting by dollars is what aims curation at the clusters that move real
    totals, so the amount column comes from the pack's own declaration
    (`data_quality.amount_column`) rather than a guessed name — Louisville
    calls it extended_amount, Cincinnati calls it amount. Row counts are the
    fallback when a pack declares none."""
    import data_model

    amount = amount or (cfg.data_quality or {}).get("amount_column") or "amount"
    con = data_model.load_all_data(data_dir, cfg)
    cols = {r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()}
    if amount in cols:
        weight_expr, unit = f"SUM(TRY_CAST({amount} AS DOUBLE))", "dollars"
    else:
        weight_expr, unit = "COUNT(*)", "rows"
    rows = con.execute(
        f"SELECT {column}, {weight_expr} FROM {table} "
        f"WHERE {column} IS NOT NULL GROUP BY 1"
    ).fetchall()
    con.close()
    return {r[0]: float(r[1] or 0) for r in rows}, unit


def spec_for(cfg, dimension: str) -> dict:
    """The pack's canonicalization spec for a dimension (agency/payee)."""
    for spec in cfg.canonicalization:
        if spec["source_column"] == dimension or spec["target_column"] == dimension:
            return spec
    raise SystemExit(
        f"no canonicalization spec for '{dimension}' in this pack; "
        f"have: {[s['source_column'] for s in cfg.canonicalization]}"
    )


def write_map(path: str, rows: list) -> None:
    with open(path, "w", newline="") as f:
        # LF, matching the hand-authored maps already in the packs; csv's
        # default CRLF would make every regenerated map a whole-file diff.
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["source", "canonical"])
        w.writerows(rows)


def write_report(out, clusters: list, rows: list, pairs: list, prefixes: list,
                 initialisms: list, residual: list, unit: str, limit: int,
                 threshold: float) -> None:
    total = sum(c.weight for c in clusters)
    merged = [c for c in clusters if len(c.members) > 1]
    fmt = (lambda v: f"${v:,.0f}") if unit == "dollars" else (lambda v: f"{v:,.0f} rows")

    print(f"# Draft canonical map review\n", file=out)
    print(f"{len(clusters):,} clusters from "
          f"{sum(len(c.members) for c in clusters):,} distinct raw values "
          f"({fmt(total)} total)", file=out)
    print(f"{len(merged):,} clusters merge >1 spelling, covering "
          f"{fmt(sum(c.weight for c in merged))}", file=out)
    print(f"{len(rows):,} rows written to the draft map\n", file=out)

    print("## Merged clusters (automatic, in the draft map)\n", file=out)
    for c in merged[:60]:
        print(f"- **{c.canonical}** — {fmt(c.weight)}", file=out)
        for raw in sorted(c.members):
            print(f"    - {raw}", file=out)
    if len(merged) > 60:
        print(f"\n_({len(merged) - 60:,} further merged clusters not listed; "
              "all are in the draft map.)_", file=out)

    print(f"\n## Near-duplicate suggestions (NOT merged — curator decides)\n", file=out)
    print(f"_Compared the {min(limit, len(clusters)):,} heaviest of "
          f"{len(clusters):,} clusters at similarity >= {threshold}. "
          "Lighter clusters were not compared._\n", file=out)
    if not pairs:
        print("_None found._", file=out)
    for ratio, a, b in pairs[:60]:
        print(f"- {ratio} — `{a.canonical}` ({fmt(a.weight)}) ~ "
              f"`{b.canonical}` ({fmt(b.weight)})", file=out)
    if len(pairs) > 60:
        print(f"\n_({len(pairs) - 60:,} further suggestions omitted.)_", file=out)

    print(f"\n## Initialism suggestions (NOT merged — curator decides)\n", file=out)
    print("_A short name whose letters match a longer name's initials, e.g. "
          "APCD / Air Pollution Control District. Confirm each: matching "
          "initials are evidence, not proof._\n", file=out)
    if not initialisms:
        print("_None found._", file=out)
    for short, long in initialisms[:40]:
        print(f"- `{short.canonical}` ({fmt(short.weight)}) may be "
              f"`{long.canonical}` ({fmt(long.weight)})", file=out)
    if len(initialisms) > 40:
        print(f"\n_({len(initialisms) - 40:,} further suggestions omitted.)_", file=out)

    print(f"\n## Curation worklist: heaviest names the seeder could not merge\n", file=out)
    print("_Single-spelling clusters, biggest first. Automatic merging is "
          "already done; this is where a curator's time is worth spending._\n",
          file=out)
    for c in residual:
        print(f"- {fmt(c.weight)} — {c.canonical}", file=out)

    print(f"\n## Prefix-rule candidates (for prefix_map, NOT in the draft)\n", file=out)
    if not prefixes:
        print("_None found._", file=out)
    for prefix, canon, raws, weight in prefixes[:40]:
        print(f"- `{prefix}` -> **{canon}** — {fmt(weight)}, "
              f"{len(raws)} spelling(s)", file=out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--city", help="path to a city.yaml (default: $CITY_CONFIG or Louisville)")
    p.add_argument("--dimension", default="payee", help="agency or payee (default: payee)")
    p.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "data"))
    p.add_argument("--out", help="draft map path (default: <pack map>.draft.csv)")
    p.add_argument("--report", help="review report path, or - for stdout")
    p.add_argument("--min-cluster", type=int, default=2,
                   help="smallest cluster to write (default 2: only real merges)")
    p.add_argument("--similarity", type=float, default=0.86,
                   help="near-duplicate suggestion threshold (default 0.86)")
    p.add_argument("--compare-top", type=int, default=400,
                   help="how many heaviest clusters to compare for near-duplicates")
    p.add_argument("--force", action="store_true",
                   help="write over the pack's real map instead of a .draft.csv")
    args = p.parse_args(argv)

    from city_config import load_city_config
    cfg = load_city_config(args.city)
    spec = spec_for(cfg, args.dimension)
    table = spec.get("table", "expenditures")
    column = spec["source_column"]

    map_path = os.path.join(cfg.base_dir, spec.get("exact_map", f"{column}_map.csv"))
    out_path = args.out or (map_path if args.force else map_path + ".draft.csv")
    if os.path.exists(out_path) and out_path == map_path and not args.force:
        raise SystemExit(f"refusing to overwrite {out_path} without --force")

    weights, unit = load_weights(args.data_dir, cfg, table, column)
    clusters = cluster_values(weights)
    rows = draft_rows(clusters, args.min_cluster)
    pairs = near_duplicates(clusters, args.similarity, args.compare_top)
    prefixes = prefix_candidates(clusters)
    initialisms = initialism_candidates(clusters, args.compare_top)
    suggested = {c.key for pair in pairs for c in pair[1:]}
    suggested |= {c.key for pair in initialisms for c in pair}
    residual = residual_clusters(clusters, suggested)

    write_map(out_path, rows)
    print(f"wrote {len(rows):,} draft rows -> {out_path}", file=sys.stderr)
    if out_path != map_path:
        print(f"(the pack's live map at {map_path} was NOT touched; "
              "curate the draft, then move it into place)", file=sys.stderr)

    if args.report:
        if args.report == "-":
            write_report(sys.stdout, clusters, rows, pairs, prefixes,
                         initialisms, residual, unit, args.compare_top,
                         args.similarity)
        else:
            with open(args.report, "w") as f:
                write_report(f, clusters, rows, pairs, prefixes, initialisms,
                             residual, unit, args.compare_top, args.similarity)
            print(f"wrote review report -> {args.report}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
