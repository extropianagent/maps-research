"""Provider-neutral data model. Every provider maps its payloads onto these."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any


@dataclass
class Hit:
    """One sighting of a place: which provider/query/cell surfaced it, and at what rank.

    Hits are the raw material for search-visibility measurement: a place seen only
    in small cells at deep subdivision levels is invisible to an ordinary search.
    """

    provider: str
    query: str
    language: str | None = None
    cell: str | None = None  # BBox.key() of the searched cell, None if unrestricted
    level: int = 0  # subdivision depth of that cell (0 = the whole search area)
    rank: int = 0  # 1-based position in that result list


@dataclass
class Review:
    place_id: str
    text: str = ""
    rating: float | None = None
    language: str | None = None
    published: str | None = None  # ISO 8601 timestamp when the provider gives one
    author: str | None = None
    author_id: str | None = None  # stable contributor id or profile URL
    author_review_count: int | None = None
    owner_reply: str | None = None
    source: str = ""  # provider name
    url: str | None = None  # link to the review on Google Maps (attribution)
    flag_url: str | None = None  # Google's content-reporting link; show it with the review

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Review":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Place:
    place_id: str
    name: str = ""
    lat: float | None = None
    lng: float | None = None
    rating: float | None = None
    review_count: int | None = None
    primary_type: str | None = None
    types: list[str] = field(default_factory=list)
    address: str | None = None
    price_level: str | None = None
    business_status: str | None = None
    website: str | None = None
    phone: str | None = None
    maps_url: str | None = None
    hits: list[Hit] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)

    @property
    def sources(self) -> list[str]:
        return sorted({h.provider for h in self.hits})

    def merge(self, other: "Place") -> None:
        """Fold another sighting of the same place into this one.

        Scalar fields keep the first non-empty value; hits and reviews accumulate.
        """
        if other.place_id != self.place_id:
            raise ValueError("cannot merge different places")
        for f in fields(self):
            if f.name in ("place_id", "hits", "reviews", "types"):
                continue
            if getattr(self, f.name) in (None, "") and getattr(other, f.name) not in (None, ""):
                setattr(self, f.name, getattr(other, f.name))
        for t in other.types:
            if t not in self.types:
                self.types.append(t)
        self.hits.extend(other.hits)
        seen = {(r.source, r.author_id or r.author, r.published, r.text[:80]) for r in self.reviews}
        for r in other.reviews:
            key = (r.source, r.author_id or r.author, r.published, r.text[:80])
            if key not in seen:
                self.reviews.append(r)
                seen.add(key)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Place":
        d = dict(d)
        hits = [Hit(**h) for h in d.pop("hits", [])]
        reviews = [Review.from_dict(r) for r in d.pop("reviews", [])]
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known}, hits=hits, reviews=reviews)


@dataclass(frozen=True)
class Query:
    """A keyword in a specific language. Parsed from 'text' or 'text:lang' (e.g. 'مخبز:ar')."""

    text: str
    language: str | None = None

    @classmethod
    def parse(cls, raw: str) -> "Query":
        raw = raw.strip()
        if ":" in raw:
            text, _, lang = raw.rpartition(":")
            if 2 <= len(lang) <= 7 and lang.replace("-", "").isalpha() and text.strip():
                return cls(text.strip(), lang)
        return cls(raw)

    def key(self) -> str:
        return f"{self.text}:{self.language}" if self.language else self.text
