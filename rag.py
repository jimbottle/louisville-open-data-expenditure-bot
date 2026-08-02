#!/usr/bin/env python3
"""
RAG spike (louisville-open-data-p4y): retrieval over city documents.

Corpus v0: Louisville Metro Council legislation from the public Legistar API
(ordinances, resolutions, and the council fund types) — the "why" behind
spending. Retrieval v0: DuckDB's FTS extension (BM25) — chosen because the
current inference stack (Cerebras) offers no embeddings endpoint, FTS adds
zero dependencies, and it keeps the single-DuckDB-engine story. Design note
and the embeddings upgrade path: docs/rag-spike.md.

Usage:
    python rag.py ingest                       # pull matters -> data/rag_documents.duckdb
    python rag.py query "ARPA rescue plan spending"
"""

import argparse
import os
import re
import sys
import time

import duckdb
import requests

# Corpus source is per-city and lives in the config pack's `rag:` block —
# most Legistar cities expose the same API (Cincinnati is on Legistar too), so
# onboarding a second city's corpus is a client name and a list of matter type
# ids, not code. These module defaults are the Louisville values and the
# fallback for a pack that declares no rag block.
LEGISTAR_CLIENT = "louisville"

# Spending-relevant matter types: Resolution, Ordinance, Capital Infrastructure
# Fund, Neighborhood Development Fund, Municipal Aid Program Funds, Paving Funds
MATTER_TYPE_IDS = (52, 53, 64, 65, 66, 70)

DEFAULT_DB = "data/rag_documents.duckdb"
DEFAULT_SINCE = "2020-01-01"
PAGE_SIZE = 1000


def corpus_settings(cfg=None) -> dict:
    """Resolve the corpus source for a city pack, falling back to Louisville."""
    block = (getattr(cfg, "raw", {}) or {}).get("rag", {}) if cfg else {}
    client = block.get("legistar_client") or LEGISTAR_CLIENT
    return {
        "client": client,
        "api": f"https://webapi.legistar.com/v1/{client}",
        "web": f"https://{client}.legistar.com/LegislationDetail.aspx",
        "matter_type_ids": tuple(block.get("matter_type_ids") or MATTER_TYPE_IDS),
        "since": block.get("since") or DEFAULT_SINCE,
        "db": block.get("db") or DEFAULT_DB,
        "min_score": float(block.get("min_score", 3.0)),
        "k": int(block.get("k", 3)),
    }


def db_path(cfg=None, data_dir: str = None) -> str:
    """Where this deployment's corpus DB lives.

    A pack declares a bare filename, which is joined with the deployment's data
    directory — a dev checkout reads ./data while the container mounts its data
    volume at /data, and the pack must not have to know which. A value that
    already carries a directory is taken literally."""
    db = corpus_settings(cfg)["db"]
    if os.path.dirname(db):
        return db
    return os.path.join(data_dir or os.environ.get("DATA_DIR", "data"), db)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def ingest(db_path: str = None, since: str = None, cfg=None) -> int:
    """Pull council matters into a DuckDB documents table and build the FTS index."""
    s = corpus_settings(cfg)
    db_path = db_path or s["db"]
    since = since or s["since"]
    API, WEB = s["api"], s["web"]
    rows = []
    for type_id in s["matter_type_ids"]:
        skip = 0
        while True:
            params = {
                "$top": PAGE_SIZE,
                "$skip": skip,
                "$filter": f"MatterTypeId eq {type_id} and MatterIntroDate ge datetime'{since}T00:00:00'",
            }
            r = requests.get(f"{API}/matters", params=params, timeout=60)
            r.raise_for_status()
            batch = r.json()
            for m in batch:
                text = _clean(m.get("MatterTitle") or m.get("MatterName") or "")
                if not text:
                    continue
                rows.append((
                    m["MatterId"],
                    m.get("MatterFile"),
                    m.get("MatterTypeName"),
                    m.get("MatterStatusName"),
                    (m.get("MatterIntroDate") or "")[:10] or None,
                    (m.get("MatterPassedDate") or "")[:10] or None,
                    m.get("MatterEnactmentNumber"),
                    text,
                    f"{WEB}?ID={m['MatterId']}&GUID={m.get('MatterGuid', '')}",
                ))
            if len(batch) < PAGE_SIZE:
                break
            skip += PAGE_SIZE
            time.sleep(0.2)
        print(f"type {type_id}: {len(rows):,} total docs so far")

    n = _build_db(rows, db_path)
    print(f"Ingested {n:,} documents -> {db_path}")
    return n


def _remove_partials(part_path: str) -> None:
    for p in (part_path, part_path + ".wal"):
        if os.path.exists(p):
            os.remove(p)


def _build_db(rows: list, db_path: str) -> int:
    """Build the documents DB into a fresh .part file and swap on success
    (same atomic pattern as pull_socrata): a failed ingest can never destroy
    the existing corpus, and the swap sidesteps DuckDB's
    one-writer-or-many-readers file locking. Stale .part/.part.wal leftovers
    from a hard kill are cleared before connecting."""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    part_path = db_path + ".part"
    _remove_partials(part_path)
    try:
        con = duckdb.connect(part_path)
        try:
            con.execute("""
                CREATE TABLE documents (
                    doc_id INTEGER, file_no VARCHAR, matter_type VARCHAR, status VARCHAR,
                    intro_date VARCHAR, passed_date VARCHAR, enactment_no VARCHAR,
                    text VARCHAR, url VARCHAR
                )
            """)
            con.executemany("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?)", rows)
            con.execute("INSTALL fts; LOAD fts;")
            con.execute("PRAGMA create_fts_index('documents', 'doc_id', 'text', overwrite=1)")
            n = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        finally:
            con.close()
        os.replace(part_path, db_path)
        return n
    except BaseException:
        _remove_partials(part_path)
        raise


def retrieve(question: str, k: int = 3, db_path: str = DEFAULT_DB, min_score: float = 3.0) -> list:
    """Top-k document chunks for a question, with citation fields.

    Returns [] when nothing clears min_score — callers should treat that as
    "no document context" rather than padding the prompt with weak hits.
    """
    con = duckdb.connect(db_path, read_only=True)
    try:
        con.execute("LOAD fts;")
        hits = con.execute("""
            SELECT file_no, matter_type, status, intro_date, passed_date, enactment_no,
                   text, url,
                   fts_main_documents.match_bm25(doc_id, ?) AS score
            FROM documents
            WHERE score IS NOT NULL AND score >= ?
            ORDER BY score DESC
            LIMIT ?
        """, [question, min_score, k]).fetchall()
    finally:
        con.close()
    cols = ["file_no", "matter_type", "status", "intro_date", "passed_date",
            "enactment_no", "text", "url", "score"]
    return [dict(zip(cols, h)) for h in hits]


def format_context(hits: list) -> str:
    """Render retrieved docs as a prompt block with citation markers."""
    if not hits:
        return ""
    lines = ["## Related city legislation (cite by file number when used)"]
    for h in hits:
        lines.append(
            f"- [{h['file_no']}] ({h['matter_type']}, {h['status']}, "
            f"introduced {h['intro_date'] or 'n.d.'}): {h['text'][:600]}"
        )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="City-document RAG spike")
    sub = parser.add_subparsers(dest="cmd", required=True)
    parser.add_argument("--city", help="city.yaml whose rag block defines the corpus")
    p_ing = sub.add_parser("ingest")
    p_ing.add_argument("--since")
    p_ing.add_argument("--db")
    p_q = sub.add_parser("query")
    p_q.add_argument("question")
    p_q.add_argument("-k", type=int)
    p_q.add_argument("--db")
    args = parser.parse_args()

    cfg = None
    if args.city:
        from city_config import load_city_config
        cfg = load_city_config(args.city)
    settings = corpus_settings(cfg)

    if args.cmd == "ingest":
        ingest(args.db, args.since, cfg=cfg)
    else:
        hits = retrieve(args.question, k=args.k or settings["k"],
                        db_path=args.db or settings["db"],
                        min_score=settings["min_score"])
        if not hits:
            print("(no hits above threshold)")
        for h in hits:
            print(f"[{h['score']:.2f}] {h['file_no']} ({h['matter_type']}, {h['intro_date']})")
            print(f"        {h['text'][:200]}")
            print(f"        {h['url']}\n")


if __name__ == "__main__":
    main()
