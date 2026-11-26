"""Review-content search and review-integrity signals over reviews already attached to places.

Everything here is plain stdlib: the corpus is personal-scale (hundreds to a few thousand reviews),
so clarity beats vectorisation. Integrity output is evidence for a human or LLM to weigh, never a verdict.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from maps.models import Place, Review

# --- tokenisation ------------------------------------------------------------------------------

_ARABIC_DIACRITICS = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_ARABIC_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه"})
_ARABIC_ARTICLES = ("وال", "بال", "كال", "فال", "لل", "ال")  # longest first
_SPLIT = re.compile(r"[\W_]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Lowercased word tokens with Arabic orthographic variants folded so spellings match each other."""
    if not text:
        return []
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _ARABIC_DIACRITICS.sub("", text).translate(_ARABIC_FOLD)
    return [_strip_article(t) for t in _SPLIT.split(text) if len(t) > 1]


def _strip_article(token: str) -> str:
    """Drop an Arabic definite article (with its one-letter clitics) when 3+ letters remain."""
    for prefix in _ARABIC_ARTICLES:
        if token.startswith(prefix) and len(token) - len(prefix) >= 3:
            return token[len(prefix) :]
    return token


# --- BM25 search -------------------------------------------------------------------------------

BM25_K1 = 1.5
BM25_B = 0.75


@dataclass
class ReviewMatch:
    score: float
    place_id: str
    place_name: str
    review: Review


@dataclass
class PlaceMatch:
    place_id: str
    place_name: str
    score: float
    n_matches: int
    best: ReviewMatch


class ReviewIndex:
    """Okapi BM25 over individual reviews, so a single telling review can surface its place."""

    def __init__(self, places: Iterable[Place]) -> None:
        self._docs: list[tuple[str, str, Review, Counter[str], int]] = []
        self._postings: dict[str, list[int]] = defaultdict(list)
        for place in places:
            for review in place.reviews:
                if not review.text or not review.text.strip():
                    continue
                tokens = tokenize(review.text)
                tf = Counter(tokens)
                doc_id = len(self._docs)
                self._docs.append((place.place_id, place.name, review, tf, len(tokens)))
                for term in tf:
                    self._postings[term].append(doc_id)
        total = sum(d[4] for d in self._docs)
        self._avgdl = total / len(self._docs) if self._docs else 0.0

    def __len__(self) -> int:
        return len(self._docs)

    def _idf(self, term: str) -> float:
        n, df = len(self._docs), len(self._postings.get(term, ()))
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def _score_all(self, query: str) -> dict[int, float]:
        scores: dict[int, float] = defaultdict(float)
        if not self._docs or self._avgdl == 0:
            return scores
        for term in dict.fromkeys(tokenize(query)):
            idf = self._idf(term)
            for doc_id in self._postings.get(term, ()):
                tf, dl = self._docs[doc_id][3][term], self._docs[doc_id][4]
                norm = BM25_K1 * (1 - BM25_B + BM25_B * dl / self._avgdl)
                scores[doc_id] += idf * tf * (BM25_K1 + 1) / (tf + norm)
        return scores

    def _matches(
        self, query: str, min_rating: float | None = None, max_rating: float | None = None
    ) -> list[ReviewMatch]:
        out = []
        for doc_id, score in self._score_all(query).items():
            if score <= 0:
                continue
            place_id, place_name, review, _, _ = self._docs[doc_id]
            if min_rating is not None and (review.rating is None or review.rating < min_rating):
                continue
            if max_rating is not None and (review.rating is None or review.rating > max_rating):
                continue
            out.append(ReviewMatch(score, place_id, place_name, review))
        out.sort(key=lambda m: (-m.score, m.place_id))
        return out

    def search(
        self,
        query: str,
        *,
        top: int = 20,
        min_rating: float | None = None,
        max_rating: float | None = None,
    ) -> list[ReviewMatch]:
        """Best-matching reviews; rating bounds apply to the review's own rating (unrated ones drop)."""
        return self._matches(query, min_rating, max_rating)[:top]

    def search_places(self, query: str, *, top: int = 20) -> list[PlaceMatch]:
        """Places ranked by their top-3 review scores, so one lucky review cannot dominate alone."""
        grouped: dict[str, list[ReviewMatch]] = defaultdict(list)
        for m in self._matches(query):
            grouped[m.place_id].append(m)
        out = [
            PlaceMatch(pid, ms[0].place_name, sum(m.score for m in ms[:3]), len(ms), ms[0])
            for pid, ms in grouped.items()
        ]
        out.sort(key=lambda p: (-p.score, p.place_id))
        return out[:top]


# --- integrity signals -------------------------------------------------------------------------

MIN_REVIEWS_FOR_FLAGS = 5
FIVE_STAR_SHARE_FLAG = 0.9
POLARIZATION_FLAG = 0.9
POLARIZATION_MIN_EACH_SIDE = 0.1
TEXTLESS_FIVE_STAR_FLAG = 0.6
TEXTLESS_MAX_CHARS = 15
DUPLICATE_MIN_CHARS = 30
DUPLICATE_TEXT_FLAG = 0.1
BURST_WINDOW = timedelta(days=7)
BURST_MIN_DATED = 8
BURST_MIN_SPAN_DAYS = 60
BURST_FLAG = 0.5
SINGLE_REVIEW_AUTHOR_FLAG = 0.4
RATING_GAP_CAVEAT = 0.7


def _confidence(n: int) -> str:
    if n < 5:
        return "none"
    if n < 20:
        return "low"
    if n < 100:
        return "medium"
    return "high"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def _share(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _burst(reviews: list[Review]) -> tuple[float | None, float | None]:
    """Max share of dated reviews inside any 7-day window, plus the dated span in days."""
    times = sorted(t for t in (_parse_ts(r.published) for r in reviews) if t is not None)
    if len(times) < BURST_MIN_DATED:
        return None, None
    best, j = 0, 0
    for i, start in enumerate(times):
        j = max(j, i)
        while j + 1 < len(times) and times[j + 1] - start < BURST_WINDOW:
            j += 1
        best = max(best, j - i + 1)
    span = (times[-1] - times[0]).total_seconds() / 86400
    return round(best / len(times), 4), round(span, 1)


def _duplicate_share(reviews: list[Review]) -> float | None:
    texts = [" ".join(tokenize(r.text)) for r in reviews if r.text and r.text.strip()]
    if not texts:
        return None
    counts = Counter(t for t in texts if len(t) >= DUPLICATE_MIN_CHARS)
    return _share(sum(1 for t in texts if counts.get(t, 0) > 1), len(texts))


def integrity_report(place: Place) -> dict[str, Any]:
    """Explainable signals about whether a place's review sample looks manipulated.

    Each signal is None when the data needed for it is missing. Flags are deliberately conservative and
    describe what crossed a threshold; they are prompts for scrutiny, not findings of fraud.
    """
    reviews = list(place.reviews)
    n = len(reviews)
    rated = [r.rating for r in reviews if r.rating is not None]
    stars = [round(x) for x in rated]
    five = [r for r in reviews if r.rating is not None and round(r.rating) == 5]
    counted = [r for r in reviews if r.author_review_count is not None]
    burst_share, span_days = _burst(reviews)

    signals: dict[str, float | None] = {
        "five_star_share": _share(stars.count(5), len(stars)),
        "one_star_share": _share(stars.count(1), len(stars)),
        "polarization": _share(stars.count(1) + stars.count(5), len(stars)),
        "textless_five_star_share": _share(
            sum(1 for r in five if len((r.text or "").strip()) < TEXTLESS_MAX_CHARS), len(five)
        ),
        "duplicate_text_share": _duplicate_share(reviews),
        "burst_share": burst_share,
        "dated_span_days": span_days,
        "single_review_author_share": _share(
            sum(1 for r in counted if r.author_review_count == 1), len(counted)
        ),
        "rating_gap": (
            round(place.rating - sum(rated) / len(rated), 3) if place.rating is not None and rated else None
        ),
        "owner_reply_share": _share(sum(1 for r in reviews if (r.owner_reply or "").strip()), n),
    }

    # Each flag needs enough of its own denominator; signals are reported regardless.
    enough = MIN_REVIEWS_FOR_FLAGS
    texts = [r for r in reviews if r.text and r.text.strip()]
    flags: list[str] = []
    if n >= enough:
        s = signals
        if (
            len(stars) >= enough
            and s["five_star_share"] is not None
            and s["five_star_share"] >= FIVE_STAR_SHARE_FLAG
        ):
            flags.append(f"{s['five_star_share']:.0%} of sampled ratings are 5★")
        if (
            len(stars) >= enough
            and s["polarization"] is not None
            and s["polarization"] >= POLARIZATION_FLAG
            and (s["five_star_share"] or 0) >= POLARIZATION_MIN_EACH_SIDE
            and (s["one_star_share"] or 0) >= POLARIZATION_MIN_EACH_SIDE
        ):
            flags.append(f"polarised ratings: {s['polarization']:.0%} are 1★ or 5★")
        textless = s["textless_five_star_share"]
        if len(five) >= enough and textless is not None and textless >= TEXTLESS_FIVE_STAR_FLAG:
            flags.append(f"{s['textless_five_star_share']:.0%} of 5★ reviews have little or no text")
        if (
            len(texts) >= enough
            and s["duplicate_text_share"] is not None
            and s["duplicate_text_share"] >= DUPLICATE_TEXT_FLAG
        ):
            flags.append(f"{s['duplicate_text_share']:.0%} of review texts are repeated verbatim")
        if (
            s["burst_share"] is not None
            and s["burst_share"] >= BURST_FLAG
            and (s["dated_span_days"] or 0) >= BURST_MIN_SPAN_DAYS
        ):
            flags.append(
                f"{s['burst_share']:.0%} of dated reviews fall in one 7-day window "
                f"(span {s['dated_span_days']:.0f} days)"
            )
        if (
            len(counted) >= enough
            and s["single_review_author_share"] is not None
            and s["single_review_author_share"] >= SINGLE_REVIEW_AUTHOR_FLAG
        ):
            flags.append(f"{s['single_review_author_share']:.0%} of reviewers have only this one review")
        if len(rated) >= enough and s["rating_gap"] is not None and abs(s["rating_gap"]) >= RATING_GAP_CAVEAT:
            flags.append(
                "sample may be unrepresentative: place rating differs from sample mean by "
                f"{s['rating_gap']:+.2f}"
            )

    if place.review_count is not None and place.review_count > n:
        caveat = (
            f"Partial sample: {n} of {place.review_count} reviews. "
            "Signals are evidence to weigh, not a verdict."
        )
    else:
        caveat = "Signals are evidence to weigh, not a verdict."

    return {
        "place_id": place.place_id,
        "n_reviews": n,
        "signals": signals,
        "flags": flags,
        "confidence": _confidence(n),
        "caveat": caveat,
    }


# --- contributor graph -------------------------------------------------------------------------


_AuthorIndex = tuple[dict[str, dict[str, float | None]], dict[str, str], dict[str, str | None]]


def _author_ratings(places: Iterable[Place]) -> _AuthorIndex:
    """author_id -> {place_id: rating}, plus place names and author display names."""
    by_author: dict[str, dict[str, float | None]] = defaultdict(dict)
    names: dict[str, str] = {}
    authors: dict[str, str | None] = {}
    for place in places:
        names.setdefault(place.place_id, place.name)
        for r in place.reviews:
            if not r.author_id:
                continue
            authors.setdefault(r.author_id, r.author)
            if authors[r.author_id] is None and r.author:
                authors[r.author_id] = r.author
            seen = by_author[r.author_id]
            if seen.get(place.place_id) is None:
                seen[place.place_id] = r.rating
    return by_author, names, authors


def co_review_graph(places: Iterable[Place], *, min_shared: int = 2) -> list[dict[str, Any]]:
    """Contributors who reviewed several places in the corpus: a lead for rings or for trusted tastes."""
    by_author, names, authors = _author_ratings(places)
    out = []
    for author_id, ratings in by_author.items():
        if len(ratings) < min_shared:
            continue
        vals = [v for v in ratings.values() if v is not None]
        out.append(
            {
                "author_id": author_id,
                "author": authors.get(author_id),
                "n_places": len(ratings),
                "mean_rating": round(sum(vals) / len(vals), 3) if vals else None,
                "places": [{"place_id": pid, "name": names[pid], "rating": v} for pid, v in ratings.items()],
            }
        )
    out.sort(key=lambda d: (-d["n_places"], d["author_id"]))
    return out


def contributor_overlap(
    places: Iterable[Place], liked_place_ids: set[str], *, min_rating: float = 4.0
) -> list[dict[str, Any]]:
    """Places endorsed by people who also endorsed places you like: taste-based discovery."""
    by_author, names, _ = _author_ratings(places)
    via: dict[str, set[str]] = defaultdict(set)
    for author_id, ratings in by_author.items():
        liked = {pid for pid, v in ratings.items() if v is not None and v >= min_rating}
        if not liked & liked_place_ids:
            continue
        for pid in liked - liked_place_ids:
            via[pid].add(author_id)
    out = [
        {"place_id": pid, "name": names[pid], "supporters": len(a), "via": sorted(a)}
        for pid, a in via.items()
    ]
    out.sort(key=lambda d: (-d["supporters"], d["place_id"]))
    return out
