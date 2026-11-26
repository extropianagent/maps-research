"""Fixed benchmarks for the improve-or-revert loop: change retrieval or scoring, rerun, keep or revert.

The simulator knows the ground truth, so recall is exact here. Real-world recall needs a hand-made
list of expected places; `expected_recall` scores a run against one.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .budget import Budget, Ledger
from .config import SIM_AREA
from .models import Place, Query
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
    *, seeds: tuple[int, ...] = (7, 11, 23), n_places: int = 3000, query: str = "bakery"
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


# --- comparison with a baseline list ---------------------------------------------------------------

MIN_CONTAINMENT_CHARS = 5  # a name this short or shorter never matches by containment
_PLACE_ID_IN_URL = re.compile(r"(?:place_id[:=]|!1s)([A-Za-z0-9_-]{10,})|(ChIJ[A-Za-z0-9_-]{10,})")


@dataclass(frozen=True)
class BaselinePlace:
    name: str = ""
    maps_url: str = ""
    place_id: str = ""


@dataclass(frozen=True)
class Baseline:
    name: str
    source: str
    places: list[BaselinePlace]


def load_baseline(path: Path | str) -> Baseline:
    """Read `{"name", "source", "places": [{"name", "maps_url", "place_id"}]}`; ValueError when unusable."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read {path}: {e}") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("places"), list) or not doc["places"]:
        raise ValueError('expected {"name": str, "source": str, "places": [..]} with at least one place')
    name, source = doc.get("name", ""), doc.get("source", "")
    if not isinstance(name, str) or not isinstance(source, str):
        raise ValueError("name and source must be strings")
    places = []
    for i, raw in enumerate(doc["places"]):
        if not isinstance(raw, dict):
            raise ValueError(f"place {i} must be an object")
        vals = {k: raw.get(k, "") for k in ("name", "maps_url", "place_id")}
        if any(v is None for v in vals.values()):
            vals = {k: (v or "") for k, v in vals.items()}
        if not all(isinstance(v, str) for v in vals.values()):
            raise ValueError(f"place {i}: name, maps_url and place_id must be strings")
        if not any(v.strip() for v in vals.values()):
            raise ValueError(f"place {i} has none of name, maps_url or place_id")
        places.append(BaselinePlace(**{k: v.strip() for k, v in vals.items()}))
    return Baseline(name, source, places)


def normalise_name(name: str | None) -> str:
    """Casefolded, punctuation stripped, whitespace collapsed."""
    return " ".join(re.sub(r"[\W_]+", " ", (name or "").casefold()).split())


def place_id_from_url(url: str | None) -> str | None:
    """A Google place ID embedded in a Maps URL, when the URL form carries one (a `cid=` does not)."""
    m = _PLACE_ID_IN_URL.search(url or "")
    return (m.group(1) or m.group(2)) if m else None


def find_match(target: BaselinePlace, places: Sequence[Place]) -> int | None:
    """Index in `places` (best first) of the place `target` names: by ID, else by normalised name.

    Name matching: equality first, then one name containing the other when the shorter one has at
    least MIN_CONTAINMENT_CHARS characters. The earliest (best-ranked) candidate wins a tie.
    """
    wanted = {i for i in (target.place_id, place_id_from_url(target.maps_url)) if i}
    if wanted:
        for idx, p in enumerate(places):
            if p.place_id in wanted or place_id_from_url(p.maps_url) in wanted:
                return idx
    want = normalise_name(target.name)
    if not want:
        return None
    names = [normalise_name(p.name) for p in places]
    for idx, have in enumerate(names):
        if have == want:
            return idx
    for idx, have in enumerate(names):
        short, long_ = sorted((want, have), key=len)
        if len(short) >= MIN_CONTAINMENT_CHARS and short in long_:
            return idx
    return None


def compare_to_baseline(
    baseline: Baseline, ranked: Sequence[Place], all_places: Iterable[Place], *, top: int
) -> dict[str, Any]:
    """Where the toolkit's run stands against a hand-made list.

    `ranked` is the whole filtered, ranked list (best first); `all_places` is everything the run
    retrieved. The toolkit's top N is `ranked[:top]`.
    """
    in_ranked = {p.place_id for p in ranked}
    candidates = [*ranked, *(p for p in all_places if p.place_id not in in_ranked)]
    rank_of = {p.place_id: i for i, p in enumerate(ranked, 1)}
    matched_top: set[str] = set()
    overlap: list[dict[str, Any]] = []
    only_baseline: list[dict[str, Any]] = []
    for b in baseline.places:
        idx = find_match(b, candidates)
        entry = {"name": b.name, "maps_url": b.maps_url, "place_id": b.place_id}
        if idx is None:
            only_baseline.append({**entry, "gap": "not retrieved"})
            continue
        found = candidates[idx]
        rank = rank_of.get(found.place_id)
        if rank is None:
            only_baseline.append({**entry, "gap": "retrieved but filtered out"})
        elif rank <= top:
            matched_top.add(found.place_id)
            overlap.append({"baseline": entry, "place_id": found.place_id, "name": found.name, "rank": rank})
        else:
            only_baseline.append({**entry, "gap": f"retrieved, ranked #{rank}"})
    only_toolkit = [
        {"place_id": p.place_id, "name": p.name, "rank": i}
        for i, p in enumerate(ranked[:top], 1)
        if p.place_id not in matched_top
    ]
    return {
        "overlap": overlap,
        "only_toolkit": only_toolkit,
        "only_baseline": only_baseline,
        "counts": {
            "baseline": len(baseline.places),
            "toolkit_top": min(top, len(ranked)),
            "overlap": len(overlap),
            "only_toolkit": len(only_toolkit),
            "only_baseline": len(only_baseline),
        },
    }
