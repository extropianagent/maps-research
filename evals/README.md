# Evals and the experiment loop

Changes to retrieval or ranking are kept only when a benchmark says they help.

```
run a real search → find what it missed → propose one change → run the benchmarks
        → better: keep it, log it      → not better: revert it, log it
```

## The benchmarks

**Simulator (fixed, free, offline).** `maps eval sim` (or `make eval`) runs every
depth against a simulated city where a search returns at most 60 results by
prominence. It reports recall, recall of hidden gems, what a second language
adds, and how many searches each depth costs. It measures the search *method*:
does subdividing and multilingual querying recover what a capped search hides?
It says nothing about Google's real ranking.

**Live benchmarks (yours, cost API calls).** A file in `benchmarks/` lists place
IDs that a good search for some request must find: places you know are good, or
places a search missed that you later found another way. Check any run against it:

```bash
maps eval check RUN_ID --expect evals/benchmarks/riyadh-bakeries.json
```

Keep a few benchmarks aside that you never tune against, so an improvement can be
checked on searches it was not designed for.

**Baseline.** "Ordinary Google Maps" is approximated by `--depth quick` with one
query: one capped search of the whole area. Every claim that the toolkit finds
more should be a comparison against that.

## Rules

1. One change per experiment.
2. Write the expected effect in `experiments.md` before running.
3. A change that does not improve a benchmark is reverted, and the null result is
   logged. Null results stop the same idea being tried twice.
4. Scoring weights in `maps/scoring.py` are starting points. Tune them only
   against live benchmarks built from your own judgements, never by feel.
5. Spending rules apply to experiments too: free by default.
