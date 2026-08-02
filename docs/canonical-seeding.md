# Canonical-map seeding (louisville-open-data-7y7)

Implements the seeding tool from [canonical-model.md](canonical-model.md) §5.
Executed 2026-08-02.

`canonical_seed.py` reads a city's raw agency/payee values, clusters the
spellings that are provably the same entity, and writes a draft map in the
two-column shape the engine loads — plus a review report aimed at the
curation it *can't* do automatically.

```
python canonical_seed.py --city cities/cincinnati/city.yaml \
    --dimension payee --data-dir data_cincinnati --report review.md
```

The draft lands at `<map>.draft.csv`; the pack's live map is never overwritten
without `--force`, so re-running on an onboarded city can't destroy curation.

## What it merges, and what it refuses to

Only values whose **normalized keys are identical** are merged into the draft:
case, punctuation, `&`/`and`, corporate suffixes (`INC`, `LLC`, `CO`, …), and
trailing store codes (`WALGREENS #4821`). A wrong merge silently folds one
organization's spend into another's and is invisible in every downstream
total, so nothing merges on similarity alone.

Everything else is a **suggestion** in the report, never a map row:

| Section | Catches | Example |
|---|---|---|
| Near-duplicates | truncations, spelling drift | `LOUISVILLE GAS AND ELE` ~ `LOUISVILLE GAS AND ELECTRIC` |
| Initialisms | acronym expansion | `APCD` → `Air Pollution Control District` |
| Prefix candidates | branch/store families | `WALGREENS #` → Walgreens |
| Worklist | the heaviest names still unmerged | biggest dollars first |

Store-code stripping is anchored to the end of the name on purpose: a mid-name
`#` is part of the identity, and `BETHEL #2 APOSTOLIC PENTECOSTAL CHURCH`
would otherwise collapse into its `#3` namesake.

## Measured against Louisville's hand-built maps

The honest result, and it is not the flattering one:

| | agency | payee |
|---|---|---|
| Curated groups with >1 spelling in the data | 25 | 4 |
| Reproduced automatically (orthographic) | 2 | 0 |
| Only reachable semantically | 23 | 4 |
| …of those, surfaced as a suggestion | 4 | 1 |
| **False merges** (tool merged what the curator separated) | **0** | **0** |

Louisville's maps are ~92% *semantic* work — `APCD` → Air Pollution Control
District, `LG&E` → Louisville Gas & Electric — which no amount of case and
punctuation normalization can infer. The seeder does not replace that curator.
Zero false merges across both dimensions is the number that matters most: the
draft is safe to install unreviewed.

## Measured on Cincinnati (the unseeded second city)

Cincinnati's raw payee field is the opposite shape — its variance is almost
entirely orthographic, which is exactly what the tool is for:

- 11,074 distinct raw payees → **10,419** after the draft map (655 collapsed)
- 550 clusters merged automatically, covering **$927M** of $8.3B in payments
- 1,205 map rows, generated in ~4 seconds
- agency: 0 rows — Cincinnati's 147 department names were already clean

The draft was audited before install: 350 of the 550 merges differ only in
case or punctuation, and all 200 that rest on something stronger were reviewed
individually — 166 differ only by a legal suffix or a trailing store code, 34
by a leading "The" or by periods inside an acronym (`H.C.P.A` / `HCPA`). All
200 are the same entity. It was then installed as
`cities/cincinnati/payee_map.csv`, replacing the empty stub.
The generated review is checked in at
[cincinnati-payee-curation.md](cincinnati-payee-curation.md) as the worked
example, and its worklist is what a curator or LLM should work down next.

One known aggressiveness: `INC` and `LLC` variants of the same name merge
(`Lykins Contracting, Inc.` / `Lykins Contracting, LLC`). These are legally
distinct entities that in practice are one vendor re-registered; for spend
analytics merging is right, but a city that needs entity-level legal fidelity
should review that class.

## What this means for onboarding cost

The seeding tool removes the mechanical portion of map curation and, more
importantly, **prioritizes what's left by dollars** — the previous state was a
curator staring at 11,074 unsorted names. It does not reduce curation to zero,
and the share it can automate is city-dependent: high where a portal's variance
is orthographic (Cincinnati), low where the city's own naming is acronym-heavy
(Louisville). The remaining semantic pass is the natural place to point an LLM,
working down the report's worklist. Cincinnati's own worklist already shows
the shape of that work: `The Enquirer` and `Cincinnati Enquirer` are still
separate clusters, and no string rule should decide they are one paper.
