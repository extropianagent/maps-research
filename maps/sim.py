"""Offline simulated city implementing the Provider protocol (no API key, no cost).

For tests, evals and dry runs. It reproduces the one dynamic that matters: a
search returns at most 60 results ranked by prominence, so obscure places only
surface when the searched area is small. The world is deterministic per
(seed, area, n_places), so recall experiments are repeatable.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

from .budget import Budget
from .geography import BBox
from .models import Hit, Place, Query, Review
from .providers.base import SearchPage

SEARCH_CAP = 60
# Fixed "now" so generated review timestamps do not change between runs.
REFERENCE_DATE = datetime(2026, 1, 1)
POOL_SIZE = 300

# category: (weight, English keywords, Arabic keywords, primary_type, types)
CATEGORIES: dict[str, tuple[float, list[str], list[str], str, list[str]]] = {
    "bakery": (0.40, ["bakery"], ["مخبز"], "bakery", ["bakery", "food", "store"]),
    "cafe": (0.25, ["cafe", "coffee"], ["مقهى"], "cafe", ["cafe", "food", "establishment"]),
    "tile installer": (
        0.20,
        ["tile", "tile installer"],
        ["بلاط"],
        "contractor",
        ["contractor", "general_contractor"],
    ),
    "bookstore": (0.15, ["bookstore", "book shop"], ["مكتبة"], "book_store", ["book_store", "store"]),
}

PHRASES: dict[str, dict[str, dict[str, list[str]]]] = {
    "bakery": {
        "en": {
            "pos": [
                "Fresh bread every morning, lovely staff.",
                "Best croissants in the area.",
                "Warm, fairly priced and always fresh.",
            ],
            "neg": ["Bread was stale and the line was slow.", "Overpriced for what you get."],
        },
        "ar": {
            "pos": ["خبز طازج كل صباح وخدمة ممتازة.", "أفضل معجنات في المنطقة."],
            "neg": ["الخبز كان قديما والخدمة بطيئة.", "الأسعار مرتفعة جدا."],
        },
    },
    "cafe": {
        "en": {
            "pos": [
                "Great coffee and a quiet place to work.",
                "Friendly baristas, good espresso.",
                "Cosy seats and tasty pastries.",
            ],
            "neg": ["Coffee arrived cold and the place was noisy.", "Long wait and rude service."],
        },
        "ar": {
            "pos": ["قهوة رائعة ومكان هادئ للعمل.", "موظفون لطفاء وقهوة ممتازة."],
            "neg": ["القهوة كانت باردة والمكان مزعج.", "انتظار طويل وخدمة سيئة."],
        },
    },
    "tile installer": {
        "en": {
            "pos": [
                "Neat tiling work, finished on time.",
                "Careful, tidy and honest about the price.",
                "Our bathroom floor looks perfect.",
            ],
            "neg": ["Uneven tiles and he never came back to fix them.", "Went over budget and late."],
        },
        "ar": {
            "pos": ["عمل بلاط متقن وانتهى في الوقت المحدد.", "دقيق ونظيف وصادق في السعر."],
            "neg": ["البلاط غير مستو ولم يعد لإصلاحه.", "تجاوز الميزانية وتأخر كثيرا."],
        },
    },
    "bookstore": {
        "en": {
            "pos": [
                "Wide selection and helpful staff.",
                "A calm place to browse for hours.",
                "Found a rare title here.",
            ],
            "neg": ["Hard to find anything, books not organised.", "Rude owner and high prices."],
        },
        "ar": {
            "pos": ["تشكيلة واسعة وموظفون متعاونون.", "مكان هادئ للتصفح لساعات."],
            "neg": ["صعب إيجاد أي كتاب والتنظيم سيئ.", "المالك فظ والأسعار مرتفعة."],
        },
    },
}

BURST_TEXTS = ["Great!", "Best place", "Amazing", "Top", "Perfect!", "ممتاز", "رائع جدا"]


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class SimProvider:
    name = "sim"

    def __init__(self, budget: Budget, *, area: BBox, n_places: int = 600, seed: int = 7) -> None:
        self.budget = budget
        self.area = area
        self.seed = seed
        self.world: list[Place] = []
        self.manipulated_ids: set[str] = set()
        self._keywords: dict[str, list[str]] = {}
        self._prominence: dict[str, float] = {}
        self._index: dict[str, int] = {}
        self._category: dict[str, str] = {}
        rng = random.Random(seed)
        for i in range(n_places):
            self._add_place(rng, i)
        # Contributor pool: ~10% are single-review accounts, the raw material of fake bursts.
        pool_rng = random.Random(seed + 991)
        self._contrib_counts = [
            1 if pool_rng.random() < 0.10 else 2 + int(pool_rng.lognormvariate(1.5, 1.0))
            for _ in range(POOL_SIZE)
        ]

    def available(self) -> bool:
        return True

    # -- world generation -------------------------------------------------

    def _location(self, rng: random.Random) -> tuple[float, float]:
        a = self.area
        dlat, dlng = a.north - a.south, a.east - a.west
        r = rng.random()
        if r < 0.5:
            lat = rng.gauss(a.south + 0.30 * dlat, 0.06 * dlat)
            lng = rng.gauss(a.west + 0.30 * dlng, 0.06 * dlng)
        elif r < 0.7:
            lat = rng.gauss(a.south + 0.65 * dlat, 0.04 * dlat)
            lng = rng.gauss(a.west + 0.70 * dlng, 0.04 * dlng)
        else:
            lat = rng.uniform(a.south, a.north)
            lng = rng.uniform(a.west, a.east)
        return _clamp(lat, a.south, a.north), _clamp(lng, a.west, a.east)

    def _add_place(self, rng: random.Random, i: int) -> None:
        lat, lng = self._location(rng)
        r, acc, cat = rng.random(), 0.0, "bookstore"
        for name, spec in CATEGORIES.items():
            acc += spec[0]
            if r < acc:
                cat = name
                break
        _, en, ar, ptype, types = CATEGORIES[cat]
        r = rng.random()
        words = en + ar if r < 0.5 else en if r < 0.75 else ar
        rating = round(float(_clamp(rng.gauss(4.2, 0.45), 1, 5)), 1)
        review_count = int(rng.lognormvariate(3.5, 1.4))
        prominence = math.log1p(review_count) + 0.3 * rating + rng.gauss(0, 0.3)
        manipulated = rng.random() < 0.03
        pid = f"sim_{i:06d}"
        if manipulated:
            # A burst of 12+ fake reviews needs enough reviews to hide among.
            review_count = max(review_count, 20)
            self.manipulated_ids.add(pid)
        self.world.append(
            Place(
                place_id=pid,
                name=f"{cat.title()} {i}",
                lat=lat,
                lng=lng,
                rating=rating,
                review_count=review_count,
                primary_type=ptype,
                types=list(types),
                business_status="OPERATIONAL",
                maps_url=f"https://maps.example/sim/{pid}",
            )
        )
        self._keywords[pid] = list(words)
        self._prominence[pid] = prominence
        self._index[pid] = i
        self._category[pid] = cat

    # -- Provider protocol ------------------------------------------------

    def matches(self, place: Place, query: Query | str) -> bool:
        text = query.text if isinstance(query, Query) else query
        q = text.casefold().strip()
        if not q:
            return False
        return any(k.casefold() in q or q in k.casefold() for k in self._keywords.get(place.place_id, []))

    def text_search(
        self, query: Query, *, bbox: BBox | None, level: int = 0, max_results: int = SEARCH_CAP, **_
    ) -> SearchPage:
        self.budget.charge("sim.search")
        candidates = [
            p for p in self.world if self.matches(p, query) and (bbox is None or bbox.contains(p.lat, p.lng))
        ]
        candidates.sort(key=lambda p: (-self._prominence[p.place_id], p.place_id))
        returned = candidates[: min(max_results, SEARCH_CAP)]
        places = []
        for rank, p in enumerate(returned, 1):
            copy = Place.from_dict(p.to_dict())
            copy.hits.append(
                Hit(
                    provider="sim",
                    query=query.text,
                    language=query.language,
                    cell=bbox.key() if bbox else None,
                    level=level,
                    rank=rank,
                )
            )
            places.append(copy)
        return SearchPage(places, saturated=len(candidates) > len(returned))

    def reviews(self, place_id: str, *, limit: int = 5, language: str | None = None) -> list[Review]:
        self.budget.charge("sim.reviews")
        place = self.world[self._index[place_id]]
        rng = random.Random(self.seed * 1_000_003 + self._index[place_id])
        n = min(place.review_count or 0, 30)
        manipulated = place_id in self.manipulated_ids
        n_burst = min(n, rng.randint(12, 16)) if manipulated else 0
        single = [i for i, c in enumerate(self._contrib_counts) if c == 1]
        regular = [i for i, c in enumerate(self._contrib_counts) if c > 1]
        bank = PHRASES[self._category[place_id]]
        out: list[Review] = []

        window_start = REFERENCE_DATE - timedelta(days=rng.randint(30, 1000))
        for _ in range(n_burst):
            c = rng.choice(single)
            when = window_start + timedelta(seconds=rng.randint(0, 4 * 86400))
            out.append(self._review(place_id, c, rng.choice(BURST_TEXTS), 5.0, when))
        for _ in range(n - n_burst):
            c = rng.choice(regular)
            rating = float(round(_clamp(rng.gauss(place.rating or 4.2, 0.9), 1, 5)))
            lang = "ar" if rng.random() < 0.3 else "en"
            tone = "neg" if rating <= 2 else "pos"
            text = rng.choice(bank[lang][tone])
            when = REFERENCE_DATE - timedelta(seconds=rng.randint(0, 3 * 365 * 86400))
            out.append(self._review(place_id, c, text, rating, when, lang))
        out.sort(key=lambda r: r.published or "", reverse=True)  # newest first
        return out[:limit]

    def _review(
        self, place_id: str, contrib: int, text: str, rating: float, when: datetime, lang: str | None = None
    ) -> Review:
        return Review(
            place_id=place_id,
            text=text,
            rating=rating,
            language=lang or ("ar" if any("؀" <= ch <= "ۿ" for ch in text) else "en"),
            published=when.isoformat(timespec="seconds"),
            author=f"Contributor {contrib}",
            author_id=f"contrib_{contrib:03d}",
            author_review_count=self._contrib_counts[contrib],
            source="sim",
        )
