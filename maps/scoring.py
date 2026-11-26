"""Objective-based ranking. Every score is a sum of named parts so it can be explained.

The weights here (prior weights, bonuses, penalties) are starting points to be
tuned by experiments (evals/), not truths. Change them when an eval says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .models import Place

OBJECTIVES: dict[str, str] = {
    "best": "Highest well-evidenced quality: rating shrunk toward the average by review count.",
    "gems": "Hidden high-quality places: good rating, hard to find, not already famous.",
    "explore": "A varied shortlist: good places, penalising repeats of the same type.",
    "find": "Places matching the most of your keywords, then by quality.",
    "hire": "Quality you can rely on: like best, but distrusts thin evidence more.",
    "investigate": "Candidates for a closer look: like hire, with evidence-weighted quality.",
}

DEFAULT_PRIOR_MEAN = 4.2


@dataclass
class Scored:
    place: Place
    score: float
    parts: dict[str, float] = field(default_factory=dict)


def bayesian_rating(
    rating: float | None, n: int | None, *, prior_mean: float, prior_weight: float = 20
) -> float:
    """Shrink a rating toward the prior so a handful of reviews cannot top the list."""
    if rating is None or n is None:
        return prior_mean
    return (prior_weight * prior_mean + n * rating) / (prior_weight + n)


def visibility(place: Place) -> dict:
    """How findable a place was: hits, distinct queries, best top-level rank, shallowest level."""
    hits = place.hits
    top = [h.rank for h in hits if h.level == 0]
    return {
        "n_hits": len(hits),
        "n_queries": len({h.query for h in hits}),
        "best_top_rank": min(top) if top else None,
        "min_level": min((h.level for h in hits), default=0),
    }


def _prior_mean(places: list[Place]) -> float:
    rated = [p.rating for p in places if p.rating is not None]
    return sum(rated) / len(rated) if rated else DEFAULT_PRIOR_MEAN


def _type_key(place: Place) -> str | None:
    return place.primary_type or (place.types[0] if place.types else None)


def _score_one(place: Place, objective: str, prior: float) -> Scored:
    n = place.review_count
    if objective == "best":
        b = bayesian_rating(place.rating, n, prior_mean=prior, prior_weight=20)
        return Scored(place, b, {"bayes": b})
    if objective == "gems":
        b = bayesian_rating(place.rating, n, prior_mean=prior, prior_weight=10)
        v = visibility(place)
        hidden = (0.15 if v["best_top_rank"] is None else 0.0) + 0.05 * min(v["min_level"], 3)
        pop = 0.25 * max(0.0, math.log10(n) - 2) if n is not None and n >= 1 else 0.0
        thin = 0.4 * max(0.0, 1 - (n or 0) / 8)
        return Scored(
            place,
            b + hidden - pop - thin,
            {"bayes": b, "hidden_bonus": hidden, "popularity_penalty": pop, "thin_evidence_penalty": thin},
        )
    if objective in ("hire", "investigate"):
        b = bayesian_rating(place.rating, n, prior_mean=prior, prior_weight=40)
        unc = 0.5 / math.sqrt(n + 1) if n is not None else 0.0
        return Scored(place, b - unc, {"bayes": b, "uncertainty": unc})
    # find
    b = bayesian_rating(place.rating, n, prior_mean=prior, prior_weight=20)
    nq = visibility(place)["n_queries"]
    return Scored(place, nq + b / 10, {"bayes": b, "n_queries": float(nq)})


def _explore(places: list[Place], prior: float) -> list[Scored]:
    """Greedy: each pick lowers the value of further places of the same type."""
    base = {p.place_id: bayesian_rating(p.rating, p.review_count, prior_mean=prior) for p in places}
    remaining = list(places)
    picked_types: dict[str, int] = {}
    out: list[Scored] = []
    while remaining:
        def adjusted(p: Place) -> tuple[float, float]:
            key = _type_key(p)
            pen = 0.3 * picked_types.get(key, 0) if key else 0.0
            return base[p.place_id] - pen, pen

        best = max(remaining, key=lambda p: adjusted(p)[0])  # max keeps the first of equal values
        value, pen = adjusted(best)
        out.append(Scored(best, value, {"bayes": base[best.place_id], "diversity_penalty": pen}))
        remaining.remove(best)
        key = _type_key(best)
        if key:
            picked_types[key] = picked_types.get(key, 0) + 1
    return out


def rank(places: list[Place], objective: str, *, top: int | None = None) -> list[Scored]:
    """Rank places best-first for an objective; unknown objectives raise ValueError."""
    if objective not in OBJECTIVES:
        raise ValueError(f"unknown objective {objective!r}; choose from {', '.join(OBJECTIVES)}")
    prior = _prior_mean(places)
    if objective == "explore":
        scored = _explore(places, prior)
    else:
        scored = [_score_one(p, objective, prior) for p in places]
        # Stable sort; "best" breaks ties toward more reviews.
        scored.sort(key=lambda s: (-s.score, -(s.place.review_count or 0) if objective == "best" else 0))
    return scored[:top] if top is not None else scored
