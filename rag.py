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

LEGISTAR_CLIENT = "louisville"
API = f"https://webapi.legistar.com/v1/{LEGISTAR_CLIENT}"
WEB = f"https://{LEGISTAR_CLIENT}.legistar.com/LegislationDetail.aspx"

# Spending-relevant matter types: Resolution, Ordinance, Capital Infrastructure
# Fund, Neighborhood Development Fund, Municipal Aid Program Funds, Paving Funds
MATTER_TYPE_IDS = (52, 53, 64, 65, 66, 70)

DEFAULT_DB = "data/rag_documents.duckdb"
DEFAULT_SINCE = "2020-01-01"
PAGE_SIZE = 1000


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def ingest(db_path: str = DEFAULT_DB, since: str = DEFAULT_SINCE) -> int:
    """Pull council matters into a DuckDB documents table and build the FTS index."""
    rows = []
    for type_id in MATTER_TYPE_IDS:
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
    p_ing = sub.add_parser("ingest")
    p_ing.add_argument("--since", default=DEFAULT_SINCE)
    p_ing.add_argument("--db", default=DEFAULT_DB)
    p_q = sub.add_parser("query")
    p_q.add_argument("question")
    p_q.add_argument("-k", type=int, default=3)
    p_q.add_argument("--db", default=DEFAULT_DB)
    args = parser.parse_args()

    if args.cmd == "ingest":
        ingest(args.db, args.since)
    else:
        hits = retrieve(args.question, k=args.k, db_path=args.db)
        if not hits:
            print("(no hits above threshold)")
        for h in hits:
            print(f"[{h['score']:.2f}] {h['file_no']} ({h['matter_type']}, {h['intro_date']})")
            print(f"        {h['text'][:200]}")
            print(f"        {h['url']}\n")


if __name__ == "__main__":
    main()
