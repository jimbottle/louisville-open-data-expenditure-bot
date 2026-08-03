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
import logging
import os
import re
import sys
import threading
import time

import duckdb
import requests

log = logging.getLogger("rag")

# Corpus source is per-city and lives in the config pack's `rag:` block —
# most Legistar cities expose the same API (Cincinnati is on Legistar too), so
# onboarding a second city's corpus is a client name and a list of matter type
# ids, not code. These module defaults are the Louisville values and the
# fallback for a pack that declares no rag block.
LEGISTAR_CLIENT = "louisville"

# Spending-relevant matter types: Resolution, Ordinance, Capital Infrastructure
# Fund, Neighborhood Development Fund, Municipal Aid Program Funds, Paving Funds
MATTER_TYPE_IDS = (52, 53, 64, 65, 66, 70)

# Bare filename on purpose: db_path() joins it with the deployment's data
# directory. A dir-carrying default would bypass that resolution entirely and
# silently resolve to /app/data inside the container.
DEFAULT_DB = "rag_documents.duckdb"
DEFAULT_SINCE = "2020-01-01"
PAGE_SIZE = 1000


def _matter_url(web: str, matter_id) -> str:
    """Public InSite page for a matter, addressed by the API's own MatterId.

    The obvious URL — LegislationDetail.aspx?ID=<MatterId>&GUID=<MatterGuid> —
    does not work: InSite's ?ID= is a separate identifier space (O-167-22 is
    MatterId 60267 in the API and ID 5922229 on the web, with a different GUID
    as well), and the API exposes no field carrying the web id. Every such link
    returns a 19-byte "Invalid parameters!" body under HTTP 200, which is why
    this shipped looking healthy: the citation footer rendered, the link 200'd,
    and only a human clicking it ever saw the failure.

    Gateway.aspx?M=L resolves the API id server-side and redirects."""
    return f"{web}?M=L&ID={matter_id}"


def _repaired_url(url: str, matter_id) -> str:
    """Heal a stored LegislationDetail URL at read time.

    The URL is written into the corpus at ingest, and the corpus lives in a
    mounted volume that a deploy does not replace — so shipping the corrected
    _matter_url alone would leave production serving the same dead links off
    the old database until someone remembered to re-ingest. The host is taken
    from the stored URL so this needs no config, and a corpus written by the
    fixed ingest passes through untouched."""
    if url and "/LegislationDetail.aspx" in url:
        return _matter_url(url.split("/LegislationDetail.aspx")[0] + "/Gateway.aspx",
                           matter_id)
    return url


def corpus_settings(cfg=None) -> dict:
    """Resolve the corpus source for a city pack, falling back to Louisville."""
    block = (getattr(cfg, "raw", {}) or {}).get("rag", {}) if cfg else {}
    client = block.get("legistar_client") or LEGISTAR_CLIENT
    return {
        "client": client,
        "api": f"https://webapi.legistar.com/v1/{client}",
        # Gateway, NOT LegislationDetail: the InSite viewer's ?ID= is a
        # different identifier space from the Web API's MatterId (7-digit vs
        # 5-digit, and its GUID differs too), so a LegislationDetail URL built
        # from API data answers "Invalid parameters!" for every document.
        # Gateway.aspx?M=L takes the API's own MatterId and redirects to the
        # right page. See _matter_url.
        "web": f"https://{client}.legistar.com/Gateway.aspx",
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


def ingest(db: str = None, since: str = None, cfg=None) -> int:
    """Pull council matters into a DuckDB documents table and build the FTS index.

    `db` defaults through db_path(), so an ingest with no explicit path writes
    where the app will actually read."""
    s = corpus_settings(cfg)
    db = db or db_path(cfg)
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
                    _matter_url(WEB, m["MatterId"]),
                ))
            if len(batch) < PAGE_SIZE:
                break
            skip += PAGE_SIZE
            time.sleep(0.2)
        print(f"type {type_id}: {len(rows):,} total docs so far")

    n = _build_db(rows, db)
    print(f"Ingested {n:,} documents -> {db}")
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
            _load_fts(con)
            con.execute("PRAGMA create_fts_index('documents', 'doc_id', 'text', overwrite=1)")
            n = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        finally:
            con.close()
        os.replace(part_path, db_path)
        return n
    except BaseException:
        _remove_partials(part_path)
        raise


# How long a failed INSTALL suppresses the next attempt. Long enough that a
# blocked-egress container isn't paying a connect timeout per question, short
# enough that a transient blip during the very first query doesn't disable
# retrieval until the container is recreated.
_FTS_RETRY_AFTER = 300.0
_FTS_INSTALL_LOCK = threading.Lock()
_fts_install_failed_at = 0.0


def _load_fts(con) -> None:
    """LOAD the FTS extension, installing it once if this host lacks it.

    LOAD alone fails on a machine where the extension was never installed —
    which is every fresh container. The Dockerfile installs it at build time
    so the common path is a plain LOAD with no network; this fallback keeps a
    dev checkout or an older image working instead of silently dropping
    citations.

    A failed INSTALL is remembered for _FTS_RETRY_AFTER seconds, because
    INSTALL reaches out to extensions.duckdb.org: in a container with blocked
    or slow egress, retrying per question would make every request pay a full
    connect timeout before answering uncited. Within that window the LOAD error
    propagates immediately, as it did before the fallback existed.

    The suppression expires rather than lasting the process lifetime, and every
    suppressed attempt logs: a permanently latched process that says so once is
    the same silent-citation-loss failure the startup probe exists to catch."""
    global _fts_install_failed_at
    try:
        con.execute("LOAD fts;")
        return
    except duckdb.Error:
        pass
    with _FTS_INSTALL_LOCK:
        since = time.monotonic() - _fts_install_failed_at
        if _fts_install_failed_at and since < _FTS_RETRY_AFTER:
            log.warning("DuckDB FTS extension unavailable (install failed %.0fs "
                        "ago, retrying in %.0fs); answering without documents",
                        since, _FTS_RETRY_AFTER - since)
            raise duckdb.Error("fts extension unavailable; install recently failed")
        try:
            con.execute("INSTALL fts; LOAD fts;")
        except duckdb.Error:
            _fts_install_failed_at = time.monotonic()
            log.warning("DuckDB FTS extension could not be installed; document "
                        "retrieval is disabled for the next %.0fs",
                        _FTS_RETRY_AFTER)
            raise
        _fts_install_failed_at = 0.0


def retrieve(question: str, k: int = 3, db_path: str = DEFAULT_DB, min_score: float = 3.0) -> list:
    """Top-k document chunks for a question, with citation fields.

    Returns [] when nothing clears min_score — callers should treat that as
    "no document context" rather than padding the prompt with weak hits.
    """
    con = duckdb.connect(db_path, read_only=True)
    try:
        _load_fts(con)
        hits = con.execute("""
            SELECT file_no, matter_type, status, intro_date, passed_date, enactment_no,
                   text, url, doc_id,
                   fts_main_documents.match_bm25(doc_id, ?) AS score
            FROM documents
            WHERE score IS NOT NULL AND score >= ?
            ORDER BY score DESC
            LIMIT ?
        """, [question, min_score, k]).fetchall()
    finally:
        con.close()
    cols = ["file_no", "matter_type", "status", "intro_date", "passed_date",
            "enactment_no", "text", "url", "doc_id", "score"]
    out = []
    for h in hits:
        d = dict(zip(cols, h))
        d["url"] = _repaired_url(d["url"], d.pop("doc_id"))
        out.append(d)
    return out


def corpus_size(db_path: str) -> int:
    """Document count, or 0 if the corpus can't be read.

    A queryable-but-empty corpus otherwise reads as healthy at startup, which
    is the same false confidence as a missing one."""
    try:
        con = duckdb.connect(db_path, read_only=True)
        try:
            return con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        finally:
            con.close()
    except duckdb.Error:
        return 0


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


def main(argv=None):
    # --city lives on a shared parent parser so it works BEFORE or AFTER the
    # subcommand; registered on the top-level parser alone it only parsed when
    # placed first, and `rag.py ingest --city ...` — the natural spelling —
    # exited with "unrecognized arguments".
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--city", help="city.yaml whose rag block defines the corpus")
    common.add_argument("--db", help="corpus path (default: the pack's, under DATA_DIR)")

    parser = argparse.ArgumentParser(description="City-document RAG",
                                     parents=[common])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_ing = sub.add_parser("ingest", parents=[common])
    p_ing.add_argument("--since")
    p_q = sub.add_parser("query", parents=[common])
    p_q.add_argument("question")
    p_q.add_argument("-k", type=int)
    args = parser.parse_args(argv)

    cfg = None
    if args.city:
        from city_config import load_city_config
        cfg = load_city_config(args.city)
    settings = corpus_settings(cfg)
    # db_path(), not settings["db"] — the pack declares a bare filename that
    # must be resolved against DATA_DIR, or ingest writes the corpus into the
    # working directory while the app reads it from the data dir and serves
    # zero citations.
    target = args.db or db_path(cfg)

    if args.cmd == "ingest":
        ingest(target, args.since, cfg=cfg)
    else:
        hits = retrieve(args.question, k=args.k or settings["k"],
                        db_path=target, min_score=settings["min_score"])
        if not hits:
            print("(no hits above threshold)")
        for h in hits:
            print(f"[{h['score']:.2f}] {h['file_no']} ({h['matter_type']}, {h['intro_date']})")
            print(f"        {h['text'][:200]}")
            print(f"        {h['url']}\n")


if __name__ == "__main__":
    main()
