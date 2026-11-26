# maps-research

This is a toolkit that an AI assistant drives for you to research places on Google Maps: bakeries, shops, tradespeople, hidden gems. You ask in plain language. The AI runs a command-line program called `maps` and reports back. There is no app and no server to run. It is free by default: it refuses to spend money unless you say so and give a ceiling.

## Ask your AI

You do not type commands. You write requests like these:

- "Find the best bakeries in Riyadh. Go deep, free only."
- "Shops across London, quality first. I will spend up to $3."
- "Tile installers in Riyadh with a rating of 4.5 or lower and at least 10 reviews. Search exhaustively and investigate their workmanship."

The AI follows `skills/maps-research/SKILL.md` to turn a request into commands.

## Try it now, no API key

The simulator (`--provider sim`) is a fake city with known answers. It is free and works offline. Use a scratch state folder so nothing lingers:

```bash
export MAPS_STATE_DIR=$(mktemp -d)
```

Search in two languages, ranked for hidden gems, as a markdown report:

```bash
uv run maps search -q bakery -q 'مخبز:ar' --provider sim --objective gems --format md
```

The report starts like this (your run id will differ):

```text
# Maps results for run `20261126-142024-22da`

- Coverage: 227 places from 18 searches (0 replayed), INCOMPLETE
- Gaps: {'saturated: more places exist here (depth limit reached)': 2}
- Spend: $0.0000 out of pocket (free mode)
- Ranking: gems; 227 matched filters (none); showing 20

| # | Name | Rating | Reviews | Score | Type | Hidden? |
| 1 | Bakery 170 | 5.0 | 21 | 4.94 | bakery | yes |
```

Re-rank the stored run for free, with exact filters (use your run id):

```bash
uv run maps rank RUN_ID --objective best --min-reviews 10 --max-rating 4.5 --top 3 --format md
```

Fetch reviews for the top places, search them, and check them for signs of manipulation:

```bash
uv run maps reviews fetch RUN_ID --provider sim --top 5
```

```bash
uv run maps reviews search RUN_ID "fresh bread" --by-place
```

```bash
uv run maps reviews integrity RUN_ID --top 2
```

Integrity output is evidence to weigh, not a verdict. For real searches you need a key: see `docs/setup.md`.

## How it finds more than an ordinary search

Google Maps text search returns at most 60 places per query and area, so a crowded city hides most of its places. This toolkit splits any area that hits the cap into smaller cells and searches each one, repeating down to a depth you choose. It searches each keyword in every language you give it, because the Arabic and English searches return different places. Filters such as "rating at most 4.5" are applied exactly after retrieval, since Google cannot filter that way itself. Every search reports what it covered and which cells were still capped, so a gap is stated rather than hidden.

Benchmark on a simulated city (`uv run maps eval sim`). Each row searches in English and Arabic; the third column shows the same search in English only:

| Depth | Recall | Recall, English only | Hidden-gem recall | Searches |
|---|---|---|---|---|
| quick | 0.02 | 0.02 | 0.00 | 2 |
| standard | 0.54 | 0.41 | 0.47 | 38 |
| deep | 0.99 | 0.75 | 0.99 | 101 |
| exhaustive | 1.00 | 0.76 | 1.00 | 111 |

These numbers come from a simulated city. They show the method works. They say nothing about how Google behaves.

## Objectives, depth, cost

Objective (`--objective`, how results are ranked):

| Objective | What it optimises |
|---|---|
| best | Highest well-evidenced quality: rating shrunk toward the average by review count. |
| gems | Hidden high-quality places: good rating, hard to find, not already famous. |
| explore | A varied shortlist: good places, penalising repeats of the same type. |
| find | Places matching the most of your keywords, then by quality. |
| hire | Quality you can rely on: like best, but distrusts thin evidence more. |
| investigate | Candidates for a closer look: like hire, with evidence-weighted quality. |

Depth (`--depth`, how hard to look):

| Depth | What it does |
|---|---|
| quick | One search, no splitting. The baseline. |
| standard | Splits capped areas up to 2 times, at most 60 searches. Default. |
| deep | Up to 4 splits, at most 300 searches. |
| exhaustive | Up to 7 splits, at most 2500 searches. |

Cost (`--cost`):

| Mode | What it does |
|---|---|
| free | Default. Never spends money. Stops at 90% of each free monthly allowance. |
| quality | Allows paid calls up to `--max-usd N` for that command. |

## What it costs

The default is free: the guard refuses any request that would cost money and stops at 90% of each free monthly allowance, keeping a 10% reserve. When it stops a search early, the result says what was covered and what was not. The guard counts only this toolkit's own usage, kept in a ledger in the state folder. It cannot see other apps using the same key, and in a cloud session the ledger is lost when the container goes away, so the guard only protects within one session. The guarantee of zero spending is therefore a daily quota cap you set in Google Cloud, which Google enforces whatever this toolkit believes; see `docs/setup.md`.

## Status

This toolkit has been built and tested against mocked APIs and a simulator only. Nothing has run against the live Google APIs yet. Field names, free allowances and prices are unconfirmed until a first live run: see `docs/live-verification.md`.

## Layout

```text
maps/        the Python code behind the maps command
skills/      the research method the AI follows
knowledge/   your preferences, case studies and lessons (no Maps content)
docs/        setup, data permissions, live verification checklist
evals/       benchmarks and the log of retrieval experiments
tests/       the test suite (mocked APIs and simulator)
```

## More

- `docs/setup.md`: API key, where to run it, first live run.
- `docs/mcp.md`: the MCP server, for using the toolkit from Claude or another client without a shell.
- `docs/account-protection.md`: the fixed rules that protect your Google account and AI subscriptions, and how the code enforces them.
- `docs/permissions.md`: what Google's terms allow you to keep and share.
- `evals/README.md`: how changes to search are tested.
- `AGENTS.md`: rules for any AI agent working in this repo.
