---
name: maps-research
description: Use when the owner asks to find, compare, explore, vet or hire places or local businesses in an area (restaurants, shops, tradespeople, services), or asks whether a place's reviews or reputation can be trusted.
---

# Maps research

A place request is a research question. One search returns only what the result
cap lets through, most prominent first; what the owner wants usually sits past
the cap. Work the steps in order. Each ends on a **done when**; stay on the step
until it holds.

Run `uv run maps …` from the repo root. `maps <command> --help` lists every flag
and what it costs. With no API key, `--provider sim` searches a simulated city.

## 1. Brief

Read `knowledge/preferences.md`, `knowledge/feedback-log.md` and
`knowledge/search-lessons.md`, then fix five settings from the owner's words:

| Setting | How to read the request | Default |
|---|---|---|
| Objective | see the table below | `best` |
| Depth | "quick", "deep", "exhaustive" | `standard` |
| Cost | spending needs the owner's own ceiling: "max $3" → `--cost quality --max-usd 3` | `--cost free` |
| Filters | every number becomes an exact flag: "no higher than 4.5, at least 10 reviews" → `--max-rating 4.5 --min-reviews 10` | none |
| Area | `--area NAME`, or `--near LAT,LNG --radius M` | ask the owner; it is the one question worth asking |

| Objective | The owner wants | Rank with | Evidence step (5) |
|---|---|---|---|
| BEST | the highest real quality | `best` | required |
| GEMS | quality that ordinary search hides | `gems` | required |
| EXPLORE | variety, new kinds of places | `explore` | optional |
| FIND | everything meeting stated requirements | `find` | optional |
| HIRE | competence they can rely on | `hire` | required, with integrity |
| INVESTIGATE | whether reviews and reputation hold up | `investigate` | required, with integrity |

The objective decides the evidence step. Ranking can differ: when the owner
says popularity should not count, rank with `gems` whatever the objective.

**Done when** you can state all five in one line.

Then read the one domain module the request falls under. Each adds the
keywords, filters, evidence words and report details of its domain:

| The request is about | Read |
|---|---|
| restaurants, cafes, bakeries, a dish | `domains/food.md` |
| hotels or anywhere to stay | `domains/hotels.md` |
| shops, or where to buy a product | `domains/shopping.md` |
| a tradesperson, contractor or service to hire | `domains/hiring.md` |
| what to see or do in a city or district | `domains/city-exploration.md` |
| whether a place's reviews or reputation hold up | `domains/investigation.md` |

Anything else: continue with this file alone.

## 2. Keywords

Write the query list before searching: what locals type, in every language
businesses there list themselves in. Per language: the plain category word, one
or two synonyms or trade terms, and the thing itself when it differs from the
business ("sourdough" beside "bakery"). Pass each as `-q "text:lang"`.

A query that returns nothing is information: keep it in the report's query list.

**Done when** every local language has at least two queries (one at quick depth).

## 3. Search, then read coverage first

Run `maps search` with the brief's settings. The `coverage` block comes before
the results in importance:

- `complete: true`: continue.
- Gaps, and the depth allows more: `maps search --resume RUN --depth <deeper>`.
- Stopped by the budget guard: continue with what was found. The guard's refusal
  is a result to report. Spending is the owner's decision alone.
- Exit code 3, or a refusal starting "Account protection:": the search is over.
  Report what the tool said to the owner and follow `docs/account-protection.md`.

To look at the results again, or with other filters or another ranking, use
`maps rank RUN` with the brief's filters. It is free and searches nothing.

**Done when** coverage is complete, or each remaining gap is noted for the report.

## 4. Follow the results

Run `maps followups RUN`. It lists place types and name words in the results
that no query used. Add the promising ones with
`maps search --resume RUN -q "new term:lang"` (earlier queries replay free).

When the owner describes a quality, not a category ("quiet", "known for its
sourdough"), also ask Google's agent tools in plain words:
`maps discover "quiet bakery known for sourdough in <city>" --into RUN`, then
`maps details RUN --missing` so the new places have ratings. It returns a short
curated list, so it adds candidates; it never replaces the search.

**Done when** a follow-up pass adds no new place that passes the filters, or two
passes have run.

## 5. Evidence and counterevidence

A rating is a claim; reviews are the evidence, and Google gives at most five per
place. Build a **shortlist** three times the size of the answer (the answer is
five places unless the owner says otherwise), then test it:

1. `maps reviews fetch RUN --top <shortlist size>` with the same filters and
   ranking as the search. The places with fetched reviews are now the shortlist.
2. Plan the review searches before running any. Per language, two word sets:
   what the owner cares about, and how it fails (for a tiler: "level grout
   finish" and "cracked uneven redo late"). One command per set, each run once:
   `maps reviews search RUN "level grout finish" --by-place`.
   Add `--mode hybrid` when `maps doctor` shows `semantic` installed: it also
   matches paraphrases and reviews in the other language.
   That is at most four searches for two languages. No matches means the
   fetched reviews are silent on the point: that is the finding. Write it down
   and move to the next step.
3. HIRE and INVESTIGATE: `maps reviews integrity RUN` (covers every place with
   fetched reviews) and `maps reviews contributors RUN` (reviewers shared
   between places).
4. GEMS and EXPLORE, only when `maps doctor` shows `apify_contributor`
   approved: follow reviewers
   who rated the best finds highly to the other places they rated,
   `maps reviews contributors RUN --liked PLACE_ID --expand 5`. It looks up
   individual reviewers through a scraper, so use it on a few contributors.

**Counterevidence** is the point: for every place you intend to recommend, you
have looked for a reason to drop it.

**Done when** each shortlisted place has a verdict (recommend, doubtful, drop)
with its reason, or you can say exactly why the reviews cannot settle it.

## 6. Report

The filled-in template below is the message the owner receives, on a phone
or anywhere else. Copy it and fill it in. Keep all five bold headings, in this order;
write "none" under a heading that has nothing. When the evidence supports no
recommendation the template still applies: the Answer says so, and Picks lists
the strongest candidates marked "unverified" with their links.

```markdown
**Answer:** <one or two sentences. If the evidence cannot support a
recommendation, say that here.>

**Picks**
1. [Name](Google Maps link) · rating · review count
   Why: <evidence in your own words>   Against: <what counts against it, or "nothing found">
…

**Doubtful or dropped:** <name: reason>

**How I searched:** <objective, depth, cost> · <queries and languages> ·
<N searches, coverage complete | gaps: …> · spent $<X>

**What would change this:** <the missing evidence, and what would get it:
a deeper search, more reviews with an approved budget, a phone call>
```

Quote review text only with its author and link, as the tool returns them.

## 7. Learn

When a search surprised you, missed something, or a method change clearly
helped: write a case study (`knowledge/case-studies/README.md`) and add its
one-line lesson to `knowledge/search-lessons.md`. When the owner states a
preference, record it in `knowledge/preferences.md` with the date and their words.
When the owner reports back on a place, add a row to `knowledge/feedback-log.md`
and follow its rule for proposing preference changes.
