# Search lessons

What searches have taught, one line each, newest first. Read before planning a
search. Each lesson names its evidence: a case study, an experiment, or
"simulator" (which tests the method, not Google's real behaviour).

## Confirmed on the simulator (to re-confirm live)

- One whole-city search sees almost nothing: 2% of about 1,200 matching places,
  and no hidden gems at all, because the cap returns the most prominent first.
  Evidence: `evals/experiments.md` 2026-11-26.
- Subdividing saturated cells recovers the rest: 54% at standard depth, 99% at
  deep, for about 38 and 101 searches. Evidence: same.
- A second language is worth more than more depth when businesses are listed in
  only one language: English alone tops out near 76% however deep the search
  goes. Evidence: same.

## Hypotheses to test on the first live searches

- Google Text Search stops at 60 results per query+area. If the real cap
  differs, saturation detection in `maps/providers/google_places.py` needs it.
- `minRating` on an IDs-only search (which is free and unlimited) may still
  filter server-side. If so, rating buckets can be discovered at no cost, and
  only the survivors need a billed details call.
- Small cells surface low-prominence places that never appear in a city-wide
  search. This is the assumption the whole method rests on; measure it by
  comparing `first_level` of the final picks.
- Places Aggregate counts give a ground-truth denominator for recall by type.
  Compare `maps census` with what `maps search` found for the same type and area.

## From real searches

Nothing yet.
