# RAG Spike: grounding answers in city documents

Tracks louisville-open-data-p4y. Executed 2026-07-31. Verdict: **viable and
cheap** — a working retrieval prototype over Louisville Metro Council
legislation exists (`rag.py`), with clean citations, zero new inference
dependencies, and a clear integration path.

## What was built

- **Corpus v0: council legislation via the public Legistar API** (no auth,
  JSON). 1,406 documents since 2021: ordinances, resolutions, and the three
  council fund types (Capital Infrastructure, Neighborhood Development,
  Municipal Aid). One document per matter; the matter title text is the body.
- **Retrieval v0: DuckDB FTS (BM25)**, one `.duckdb` file
  (`data/rag_documents.duckdb`). `rag.retrieve(question, k)` returns hits with
  file number, type, status, dates, and a Legistar deep link for citation;
  `rag.format_context(hits)` renders a prompt block.
- Quality check: "American Rescue Plan ARPA federal relief spending" returns
  R-057-21 (accepting ARP funds), O-374-22 (ARP reappropriations), R-083-21
  (ARP priority areas) — precisely the "why" behind the 2021+ spend spike.
  "Affordable housing trust fund appropriation" surfaces a district housing
  appropriation as its top hit.

## Spike findings (the reasons behind the choices)

1. **No embeddings provider exists on the current stack.** Cerebras has no
   embeddings endpoint and the only configured keys are Cerebras. Embeddings
   would mean a new provider account or a local model dependency (torch).
   BM25 ships today with zero deps; embeddings are an upgrade, not a
   prerequisite.
2. **The city's own website is bot-protected** (louisvilleky.gov 403s
   non-browser fetches, incl. press releases and budget book PDFs). The
   Legistar API is the reliable, structured corpus — and for spending
   questions it's also the best one: appropriations ARE legislation.
   Press releases/budget PDFs need a browser-based or manual ingest path later.
3. **BM25 absolute scores are poorly calibrated.** Real hits score ~5+;
   nonsense queries still hit ~2.5-2.9 on stray tokens. `min_score=3.0`
   filters junk on this corpus, but the durable fix is (a) letting the
   interpreter LLM ignore irrelevant context (it sees the chunks and the
   question) and/or (b) the embeddings upgrade.

## Integration: SHIPPED 2026-08-02 (louisville-open-data-c1i)

Live at louisville.raylytics.io. What the build changed from the design below,
and what only showed up under a real deployment:

- **Corpus source moved into the city pack** (`rag:` block: legistar client,
  matter type ids, since, min_score, k). A second Legistar city is config.
- **The footer lists only what the answer cited**, not what BM25 returned.
  Retrieval fires on every question, so an answer about executive salaries
  came back with a $1,000 neighborhood appropriation attached at score 3.1.
  Letting the model's decision to name a file number act as the relevance
  filter solves the calibration problem that finding 3 describes, without
  needing embeddings.
- **Three deployment failures the design didn't anticipate**, each of which
  silently disabled citations while everything looked healthy:
  1. `rag.py` wasn't in the Dockerfile's COPY list.
  2. The container had no DuckDB FTS extension — `LOAD` does not install one,
     so every request logged a warning and answered without citations. Startup
     now runs a real retrieval probe instead of stat-ing the corpus file.
  3. The corpus path was relative (`data/…`), which doesn't exist inside the
     container; the pack now declares a bare filename resolved against
     DATA_DIR.
- **The refinement pass deleted every citation.** Its rubric says anything the
  RESULTS table doesn't support must go, and a file number never appears in a
  results table. It now receives the same document block, with a bounded
  carve-out that keeps a cited file number but can't invent one.
- **The first citation rule was too strict** ("only when a document explains a
  NUMBER") and never fired: council legislation explains the *program*, not
  the line items. Loosened to "what the money was for or why", with the hard
  line that results remain the only source of figures.

Verified end-to-end in production: "What did Louisville spend American Rescue
Plan money on?" answers from the data and adds "Priority spending areas were
set by Resolution R-083-21" with a Legistar link; "What are the highest paid
positions?" retrieves three weak matches, cites none, and renders no footer.

## Integration design (as specified before the build)

- **Routing: always-retrieve, threshold-gated.** Run `retrieve()` on every
  question (~ms, local); include `format_context()` in the *interpretation*
  prompt only when hits clear the threshold. No question classifier needed —
  empty hits just means no context block, and SQL generation is untouched.
- **Citations:** a new `sources` SSE event carrying file numbers + Legistar
  URLs so the frontend can render links; instruct the interpreter to cite
  file numbers inline when it uses a document.
- **Refresh:** re-run `rag.py ingest` in `refresh_data.py`'s cadence. DuckDB
  allows one writer OR many readers per file, never both — `ingest` therefore
  builds a `.part` file and `os.replace`s it in, and `retrieve` opens
  read-only per query, so a refresh never collides with serving reads.
- **Config-pack fit:** the corpus source belongs in the city pack — Legistar
  client name + matter type ids per city (most Legistar cities expose the same
  API; Cincinnati is on Legistar too).

## Upgrade path

- Embeddings + DuckDB VSS (hybrid with BM25) once an embeddings provider is
  chosen — candidates: Gemini free tier, a small local model, or whatever the
  product's paid stack standardizes on.
- Real chunking when document bodies grow past titles (attachments, minutes
  PDFs via Legistar's MatterAttachments endpoint).
