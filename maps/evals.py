"""Fixed benchmarks for the improve-or-revert loop: change retrieval or scoring, rerun, keep or revert.

The simulator knows the ground truth, so recall is exact here. Real-world recall needs a hand-made
list of expected places; `expected_recall` scores a run against one.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .budget import Budget, Ledger
from .config import SIM_AREA
from .models import Query
from .retrieval import DEPTHS, discover
from .sim import CATEGORIES, SimProvider

GEM_MIN_RATING = 4.7
GEM_REVIEW_RANGE = (5, 80)


def _arabic_twin(query: str) -> str | None:
    """The Arabic keyword for the same category, so the benchmark can measure multilingual gain."""
    for _, en, ar, _, _ in CATEGORIES.values():
        if query.casefold() in (k.casefold() for k in en):
            return ar[0]
    return None


def _recall(found: set[str], truth: set[str]) -> float:
    return len(found & truth) / len(truth) if truth else 1.0


def sim_benchmark(
    *, seeds: tuple[int, ...] = (7, 11, 23), n_places: int = 600, query: str = "bakery"
) -> list[dict[str, Any]]:
    """Recall and cost per depth, averaged over simulated worlds."""
    twin = _arabic_twin(query)
    both = [Query(query), *([Query(twin, "ar")] if twin else [])]
    english = [Query(query)]
    worlds = []
    for seed in seeds:
        sim = SimProvider(Budget(Ledger(":memory:")), area=SIM_AREA, n_places=n_places, seed=seed)
        truth = {p.place_id for p in sim.world if any(sim.matches(p, q) for q in both)}
        gems = {
            p.place_id
            for p in sim.world
            if p.place_id in truth
            and (p.rating or 0) >= GEM_MIN_RATING
            and p.review_count is not None
            and GEM_REVIEW_RANGE[0] <= p.review_count <= GEM_REVIEW_RANGE[1]
        }
        worlds.append((sim, truth, gems))

    rows = []
    for name, depth in DEPTHS.items():
        acc = {"recall": 0.0, "recall_en_only": 0.0, "gem_recall": 0.0, "searches": 0.0}
        for sim, truth, gems in worlds:
            full = discover([sim], both, SIM_AREA, depth)
            en = discover([sim], english, SIM_AREA, depth)
            acc["recall"] += _recall(set(full.places), truth)
            acc["recall_en_only"] += _recall(set(en.places), truth)
            acc["gem_recall"] += _recall(set(full.places), gems)
            acc["searches"] += full.tasks_run
        rows.append({"depth": name, **{k: round(v / len(worlds), 3) for k, v in acc.items()}})
    return rows


def expected_recall(found_ids: Iterable[str], expected_ids: Iterable[str]) -> dict[str, Any]:
    """Score a run against a hand-curated list of places that a good search must surface."""
    found, expected = set(found_ids), set(expected_ids)
    hit = expected & found
    return {
        "expected": len(expected),
        "found": len(hit),
        "recall": len(hit) / len(expected) if expected else 1.0,
        "missing": sorted(expected - found),
    }
