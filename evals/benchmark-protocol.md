# Benchmarking against ordinary AI and Ask Maps

Run this before trusting the toolkit, and again whenever the method changes.
The question it answers: does the toolkit find better places than what the
owner would get without it? If not, the extra machinery is not earning its keep.

## The four standing benchmarks

One real request per domain, written by the owner in their own words, kept in
`benchmarks/`: `food.json`, `hotels.json`, `shopping.json`, `hiring.json`.
Each is a request the owner can judge, in a city they know.

## For each benchmark

1. **Baselines first, before running the toolkit**, so they cannot be shaped by
   its results. Two of them:
   - *Ordinary AI:* the same request to the AI host with no toolkit.
   - *Ask Maps:* the same request typed into Ask Maps in the Google Maps app.
   Save each as a baseline file listing the places it named. Names and Google
   Maps links only, no review text or ratings:
   `benchmarks/baselines/<domain>-ordinary-ai.json`, `…-ask-maps.json`.
2. **Run the toolkit** by following the skill, free, at standard depth. Note the
   run ID, searches and spend.
3. **Compare:**

   ```bash
   uv run maps eval compare RUN_ID --baseline evals/benchmarks/baselines/food-ask-maps.json --top 10
   ```

   It reports the overlap, what only the toolkit found, what only the baseline
   found, and for each baseline place the toolkit missed whether it was never
   retrieved (a retrieval gap) or retrieved but ranked low (a ranking gap).
4. **The owner judges** every place that appears in only one list: good, fine,
   bad, or cannot tell. Judge without looking at which list it came from where
   that is practical.
5. **Record** the result in `experiments.md`: counts per list of good, fine and
   bad, the gaps, the spend, and the decision.

## Sharper tests, one at a time

Each isolates one thing. Change one thing per test, and repeat the baseline
once to see how much the answer moves on its own.

| Test | How | What it shows |
|---|---|---|
| Added value of a route | Compare the baseline with one new route (deeper search, `maps discover`, a curator's list) at the same filters, area and time | Whether the route found good eligible places the baseline did not. More results is not the measure |
| Preference removal | Run the same request with one stated preference left out | If the picks do not change, the preference was decoration |
| Runner-up crux | For the top pick and the runner-up, state what observed fact would reverse them and the cheapest way to observe it | Whether the order rests on evidence |
| Order and framing | Give the AI the same shortlist in reversed and shuffled order, and with neutral versus promotional wording of the same facts, in a fresh conversation each time | Whether the judgement follows evidence or position. Use the simulator or a shortlist already fetched; do not query Maps again to manufacture test data |
| Trusted collection | Search alone, trusted collection plus search, and collection alone, at equal effort | Whether the collection adds finds or narrows them |

The toolkit's own ranking is code and does not depend on order
(`tests/test_scoring.py`). These tests are about the AI's judgement on top of it.

## Deciding

| Result | Decision |
|---|---|
| Toolkit's own finds are mostly good, and it misses little of the baselines | Keep the setup. Add the good finds to the benchmark's expected places. |
| Baseline names good places the toolkit never retrieved | Retrieval gap: more keywords, another language, deeper search, or `maps discover`. Run an experiment. |
| Toolkit retrieved them but ranked them low | Ranking gap: an experiment on `maps/scoring.py` weights. |
| Toolkit's own finds are mostly bad or indistinguishable from the baselines | The method is not adding value for this domain. Say so, and use the baseline for it until an experiment changes that. |

A domain where the baselines are already good enough is a legitimate outcome.
The toolkit should be used where it wins.
