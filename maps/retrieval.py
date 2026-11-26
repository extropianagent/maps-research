"""Adaptive discovery: keep subdividing wherever a search hits the provider's result cap.

A capped search returns the most prominent matches in its area. Everything else
in that area stays invisible until the area is small enough that the cap no
longer binds. So: search a cell; if the result was saturated, split the cell in
four and search each. Unsaturated cells are complete and are left alone, which
keeps spend concentrated where places are dense.

Tasks run breadth-first, so when a budget or task limit ends the run early, the
whole area has been covered at a uniform depth rather than one corner
exhaustively. Whatever could not be resolved is reported in `unresolved`: a
search never claims coverage it did not achieve.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .budget import BudgetExceeded
from .geography import BBox
from .models import Place, Query
from .providers.base import Provider, ProviderError
from .store import RunStore

MAX_CONSECUTIVE_ERRORS = 3


@dataclass(frozen=True)
class Depth:
    name: str
    max_level: int  # how many times a saturated cell may be split
    max_results: int  # results requested per search (the provider caps at its own limit)
    max_tasks: int  # ceiling on searches executed in one run
    min_cell_m: float = 100.0  # never split below this cell size


DEPTHS: dict[str, Depth] = {
    "quick": Depth("quick", max_level=0, max_results=20, max_tasks=8),
    "standard": Depth("standard", max_level=2, max_results=60, max_tasks=60),
    "deep": Depth("deep", max_level=4, max_results=60, max_tasks=300),
    "exhaustive": Depth("exhaustive", max_level=7, max_results=60, max_tasks=2500),
}


@dataclass
class DiscoveryResult:
    places: dict[str, Place] = field(default_factory=dict)
    tasks_run: int = 0
    tasks_resumed: int = 0
    stopped: str | None = None  # why the run ended early, if it did
    # Cells that were still saturated when the run could go no deeper: known coverage gaps.
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.stopped is None and not self.unresolved and not self.errors


def _splittable(cell: BBox, level: int, depth: Depth) -> bool:
    return level < depth.max_level and min(cell.width_m, cell.height_m) / 2 >= depth.min_cell_m


def discover(
    providers: list[Provider],
    queries: list[Query],
    area: BBox,
    depth: Depth,
    *,
    store: RunStore | None = None,
    progress: Callable[[str], None] | None = None,
    search_kwargs: dict[str, Any] | None = None,
) -> DiscoveryResult:
    """Run every query against every provider over `area`, subdividing saturated cells.

    With a `store`, completed tasks are checkpointed and a later call with the
    same arguments replays them instead of repeating the requests.
    """
    result = DiscoveryResult()
    search_kwargs = search_kwargs or {}
    queue: deque[tuple[Provider, Query, BBox, int]] = deque(
        (p, q, area, 0) for q in queries for p in providers
    )
    exhausted: dict[str, str] = {}  # provider name -> reason it was dropped
    consecutive_errors: dict[str, int] = {}

    def merge(places: list[Place]) -> None:
        for p in places:
            if p.place_id in result.places:
                result.places[p.place_id].merge(p)
            else:
                result.places[p.place_id] = p

    while queue:
        provider, query, cell, level = queue.popleft()
        gap = {"provider": provider.name, "query": query.key(), "cell": cell.key(), "level": level}
        if provider.name in exhausted:
            result.unresolved.append({**gap, "reason": "not searched"})
            continue

        key = f"{provider.name}|{query.key()}|{cell.key()}|{depth.max_results}"
        record = store.done(key) if store else None
        if record is not None:
            places, saturated = [Place.from_dict(p.to_dict()) for p in record.places], record.saturated
            result.tasks_resumed += 1
        else:
            if result.tasks_run >= depth.max_tasks:
                exhausted[provider.name] = f"max_tasks ({depth.max_tasks}) reached for depth '{depth.name}'"
                result.unresolved.append({**gap, "reason": "not searched"})
                continue
            try:
                page = provider.text_search(
                    query, bbox=cell, level=level, max_results=depth.max_results, **search_kwargs
                )
            except BudgetExceeded as e:
                exhausted[provider.name] = str(e)
                result.unresolved.append({**gap, "reason": "not searched"})
                continue
            except ProviderError as e:
                result.errors.append(f"{provider.name} '{query.key()}' @ {cell.key()}: {e}")
                result.unresolved.append({**gap, "reason": "error"})
                consecutive_errors[provider.name] = consecutive_errors.get(provider.name, 0) + 1
                if consecutive_errors[provider.name] >= MAX_CONSECUTIVE_ERRORS:
                    exhausted[provider.name] = f"{MAX_CONSECUTIVE_ERRORS} consecutive errors, last: {e}"
                continue
            consecutive_errors[provider.name] = 0
            result.tasks_run += 1
            places, saturated = page.places, page.saturated
            if store:
                store.record(key, places, saturated=saturated)
            if progress:
                progress(
                    f"[{result.tasks_run}] {provider.name} '{query.key()}' L{level}: "
                    f"{len(places)} results{' (saturated)' if saturated else ''}"
                )

        merge(places)
        if saturated:
            if _splittable(cell, level, depth):
                queue.extend((provider, query, child, level + 1) for child in cell.split())
            elif depth.max_level > 0:
                result.unresolved.append({**gap, "reason": "still saturated at depth limit"})

    if exhausted:
        result.stopped = "; ".join(f"{name}: {why}" for name, why in exhausted.items())
    return result


def coverage_summary(result: DiscoveryResult) -> dict[str, Any]:
    """Honest account of what the run did and did not cover, for the report header."""
    gaps: dict[str, int] = {}
    for u in result.unresolved:
        gaps[u["reason"]] = gaps.get(u["reason"], 0) + 1
    return {
        "places": len(result.places),
        "searches_run": result.tasks_run,
        "searches_resumed": result.tasks_resumed,
        "complete": result.complete,
        "stopped": result.stopped,
        "gaps": gaps,
        "errors": result.errors[:5],
    }


def suggest_followups(places: list[Place], queries: list[Query], *, top: int = 8) -> dict[str, list[tuple[str, int]]]:
    """Recursive discovery: what do the results suggest searching for next?

    Returns frequent primary types and name tokens that the queries so far did
    not use. The agent decides which are worth a new search; this only surfaces
    the candidates.
    """
    used = {t for q in queries for t in q.text.casefold().split()}
    types: dict[str, int] = {}
    tokens: dict[str, int] = {}
    for p in places:
        if p.primary_type and p.primary_type.replace("_", " ").casefold() not in used:
            types[p.primary_type] = types.get(p.primary_type, 0) + 1
        for tok in set((p.name or "").casefold().replace("-", " ").split()):
            tok = tok.strip(".,()&|'\"")
            if len(tok) > 2 and tok not in used and not tok.isdigit():
                tokens[tok] = tokens.get(tok, 0) + 1
    by_count = lambda d: sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:top]  # noqa: E731
    return {"types": by_count(types), "name_tokens": [kv for kv in by_count(tokens) if kv[1] >= 2]}
