# Case studies

One file per search that taught something: `YYYY-MM-DD-short-name.md`. Write one
when a search surprised you, missed something you later found another way, or
when a change to the method clearly helped. Routine searches need none.

Keep place IDs, Google Maps links and your own judgement. Leave out review text,
addresses and rating tables: those are Google's content and may not be stored
(see `docs/permissions.md`).

## Template

```markdown
# <what was searched, where>

- Date:
- Request, in the owner's words:
- Commands run (objective, depth, cost, queries, languages):
- Spend: searches run, out-of-pocket USD

## What was found
The picks, as place IDs + Maps links, each with one line of your own reasoning.

## What was missed or wrong
Places found later by other means, weak evidence that was trusted, filters that
excluded good candidates. How each miss was discovered.

## Why
The mechanism: wrong keyword, wrong language, area too coarse, cap hit, review
sample too thin, ranking weight off.

## Lesson
One or two sentences, copied into `knowledge/search-lessons.md`. If it suggests
a code or weight change, the experiment to run (see `evals/README.md`).
```
