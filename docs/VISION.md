# Vision: "Lou for cities" — civic analytics as a product

Lou started as a one-evening proof of concept for Louisville, but the problem it
solves is universal: every city with an open data portal (most on the same
ArcGIS Hub stack Louisville uses) has data that residents, journalists, and
council staff can't actually query. Metadata quality — not API access — is the
real bottleneck, and Lou's data dictionary + canonicalization layer is exactly
the piece that fixes it.

**Go-to-market**: sell to municipal governments or the Esri partner ecosystem,
not consumers. Govtech contracts are slow but sticky. Existing relationships:
LMTS, Metro Watchdog Alliance.

## Product architecture direction

Split the codebase into a **generic engine** and a **per-city config pack**, so
onboarding a new city means writing config + curation, not code:

| Generic engine (reusable) | City config pack (per deployment) |
|---|---|
| DuckDB loader, schema unification framework | Dataset list + file/API sources (ArcGIS Hub item ids) |
| Canonicalization framework (exact + prefix maps) | Agency/payee mapping tables |
| Offsetting/artifact data-quality flags | Data dictionary + semantic labels |
| Schema description generators (full + compact) | Summary-table specs (common starter questions) |
| Chart inference, SSE app, agent loop, cache | Branding, starter questions, fund/grant patterns |

## Roadmap themes (tracked in beads)

1. **Reusability refactor** — extract the engine/config split above.
2. **Model resilience** — inference providers deprecate models without notice
   (qwen 404 incident); the app should fall back at runtime, not require a
   redeploy.
3. **RAG over city documents** — ground interpretations in press releases,
   budget books, and council minutes with citations, so "why did spending
   spike in 2021" gets the ARPA context, not just the numbers.
4. **Operational maturity** — uptime monitoring, deploy verification, the
   things a paying city would ask about.
