# Experiment log

Newest first. One entry per experiment: what changed, what was expected, what
the benchmark showed, kept or reverted.

## 2026-11-26: baseline of the method on the simulator

- **Setup:** simulated city, 3,000 places, 1,178 matching "bakery" in English or
  Arabic, 60-result cap, queries `bakery` + `مخبز:ar`, seed 7.
- **Expected:** recall rises with depth; one search of the whole area finds few
  hidden gems.
- **Result:**

  | Depth | Recall | Gem recall | Searches |
  |---|---|---|---|
  | quick | 0.03 | 0.00 | 2 |
  | standard | 0.56 | 0.48 | 34 |
  | deep | 1.00 | 1.00 | 94 |
  | exhaustive | 1.00 | 1.00 | 102 |

  English-only querying of the smaller 600-place city plateaued at 0.76 recall at
  every depth beyond quick.
- **Decision:** kept as the baseline. A 600-place city was too easy to separate
  the depths (standard already reached 0.98), so the benchmark uses 3,000.
- **Caveat:** this validates the subdivision logic only. Google's ranking is not
  a pure prominence sort, and the 60-result cap is unconfirmed.
