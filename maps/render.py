"""Output shaping: flat result rows, a markdown table, and review blocks that keep attribution."""

from __future__ import annotations

from typing import Any

from .models import Review
from .reviews import ReviewMatch
from .scoring import Scored, visibility


def place_row(scored: Scored, *, index: int) -> dict[str, Any]:
    """One flat, agent-friendly row. `index` is the displayed rank (callers count from 1)."""
    p = scored.place
    vis = visibility(p)
    return {
        "rank": index,
        "place_id": p.place_id,
        "name": p.name,
        "rating": p.rating,
        "reviews": p.review_count,
        "score": round(scored.score, 3),
        "type": p.primary_type,
        "address": p.address,
        "maps_url": p.maps_url,
        "seen_by": sorted({h.query for h in p.hits}),
        "first_level": vis["min_level"],
        "top_rank": vis["best_top_rank"],
        "sources": p.sources,
        "parts": {k: round(v, 3) for k, v in scored.parts.items()},
    }


def _cell(value: Any) -> str:
    """Markdown table cell: unknowns blank, pipes and newlines neutralised."""
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")


def to_markdown(rows: list[dict[str, Any]]) -> str:
    """GitHub table. Names link to Google Maps when a URL exists (attribution travels with output)."""
    out = [
        "| # | Name | Rating | Reviews | Score | Type | Hidden? |",
        "|---|------|--------|---------|-------|------|---------|",
    ]
    for r in rows:
        name = _cell(r.get("name") or r.get("place_id"))
        if r.get("maps_url"):
            name = f"[{name}]({r['maps_url']})"
        hidden = "yes" if r.get("top_rank") is None else ""
        out.append(
            f"| {_cell(r['rank'])} | {name} | {_cell(r.get('rating'))} | {_cell(r.get('reviews'))} "
            f"| {_cell(r.get('score'))} | {_cell(r.get('type'))} | {hidden} |"
        )
    return "\n".join(out)


def review_block(match_or_review: ReviewMatch | Review, place_name: str) -> dict[str, Any]:
    """A review with its attribution fields, always present so callers cannot drop them by accident."""
    review = match_or_review.review if isinstance(match_or_review, ReviewMatch) else match_or_review
    block: dict[str, Any] = {
        "place_id": review.place_id,
        "place": place_name,
        "rating": review.rating,
        "text": review.text,
        "language": review.language,
        "published": review.published,
        "author": review.author,
        "url": review.url,
        "flag_url": review.flag_url,
    }
    if isinstance(match_or_review, ReviewMatch):
        block["score"] = round(match_or_review.score, 3)
    return block
