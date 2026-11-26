# Experiment log

Newest first. One entry per experiment: what changed, what was expected, what
the benchmark showed, kept or reverted.

## 2026-11-26: partial matching in review search

- **Change:** in `maps reviews search`, a query term of three or more
  characters also matches review words that contain it, at half weight.
- **Why:** in a skill test, "crack" did not match "cracked" and Arabic "إصلاح"
  did not match "لإصلاحه", so the one review reporting bad workmanship was
  missed by every planned search.
- **Expected:** those two cases match; an exact word still outranks an
  inflected one; place discovery is untouched.
- **Result:** as expected in `tests/test_reviews.py`. Simulator recall rows are
  unchanged (the change does not touch discovery).
- **Decision:** kept. Risk to watch live: short terms matching inside unrelated
  longer words ("late" in "plate"). If that shows up, raise the minimum length.

## 2026-11-26: a provider's quota refusal ends its part of the run

- **Change:** an HTTP 429 that survives one retry now stops that provider for
  the rest of the run, is reported in `coverage.stopped`, and is not
  checkpointed, so `--resume` retries it later.
- **Why:** the recommended setup is a strict daily quota in Google Cloud, so
  quota refusals are routine. Before, each one counted as an error and the
  provider was dropped only after three in a row.
- **Result:** covered by tests with a mocked 429; simulator rows unchanged.
- **Decision:** kept. Unconfirmed live: that Google signals an exhausted daily
  quota with HTTP 429 (`docs/live-verification.md`).

## 2026-11-26: unrated places rank last

- **Change:** in `best`, `gems`, `explore`, `hire` and `investigate`, a place
  with no rating is ordered after every rated place; in `hire` and
  `investigate` an unknown review count counts as zero reviews.
- **Why:** an independent review showed an unrated place scoring exactly the
  prior and outranking a 4.6-star place with 12 reviews under `hire`.
- **Expected:** no change to simulator recall (ranking only); the reviewer's
  three-place case orders the unrated place last.
- **Result:** as expected. Recall rows unchanged; new tests in
  `tests/test_scoring.py` pass.
- **Decision:** kept. Not yet checked against a live benchmark, because none
  exists yet.

## 2026-11-26: baseline of the method on the simulator

- **Setup:** simulated city, 3,000 places, about 1,200 matching "bakery" in
  English or Arabic, 60-result cap, queries `bakery` + `مخبز:ar`, mean of seeds
  7, 11 and 23 (`maps eval sim`).
- **Expected:** recall rises with depth; one search of the whole area finds few
  hidden gems; English alone plateaus below the bilingual search.
- **Result:**

  | Depth | Recall | Recall, English only | Gem recall | Searches |
  |---|---|---|---|---|
  | quick | 0.02 | 0.02 | 0.00 | 2 |
  | standard | 0.54 | 0.41 | 0.47 | 38 |
  | deep | 0.99 | 0.75 | 0.99 | 101 |
  | exhaustive | 1.00 | 0.76 | 1.00 | 111 |

- **Decision:** kept as the baseline. A 600-place city was too easy to separate
  the depths (standard already reached 0.98), so the benchmark uses 3,000.
- **Caveat:** this validates the subdivision logic only. Google's ranking is not
  a pure prominence sort, and the 60-result cap is unconfirmed.
