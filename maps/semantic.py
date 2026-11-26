"""Meaning-based review search: finds paraphrases and cross-language matches that BM25 misses.

Review text is Google Maps content and must stay on this machine, so embeddings come from a LOCAL
model (the optional `semantic` extra: fastembed, ONNX on CPU), never from an API. The module itself
imports with the standard library only; the corpus is personal-scale (hundreds to a few thousand
reviews), so plain lists and math are enough and no numpy is required here.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

from maps.models import Place, Review
from maps.reviews import PlaceMatch, ReviewIndex, ReviewMatch

# Chosen from the model list in fastembed's own source (fastembed/text/pooled_embedding.py on the
# qdrant/fastembed main branch), which lists this model as "Multilingual (~50 languages)", 384 dims,
# ~0.22 GB, 128-token truncation, and needs no query/passage prefixes. It could NOT be downloaded or run
# in the session that wrote this, so its Arabic quality is unverified. `intfloat/multilingual-e5-small`
# is not in that list (only the 2.2 GB `-large` is), which is why it is not the default.
DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

INSTALL_HINT = "install with `uv sync --extra semantic`"

# Model families trained with asymmetric prefixes: (query prefix, document prefix).
_PREFIXES: dict[str, tuple[str, str]] = {"intfloat/multilingual-e5": ("query: ", "passage: ")}

_CACHE_VERSION = 1
HYBRID_MIN_DEPTH = 50
HYBRID_DEPTH_FACTOR = 5
_MIN_POSITIVE_SCORE = 1e-6  # hybrid ignores semantic non-matches so they cannot add noise ranks


class SemanticUnavailable(RuntimeError):  # noqa: N818 - reads better as a condition than an *Error
    """Semantic search cannot run here (extra not installed, or the model cannot be loaded)."""


class Embedder(Protocol):
    """Anything that turns text into vectors; `name` keys the cache so models never mix."""

    name: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed documents (reviews)."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query (some models treat queries differently from documents)."""
        ...


class FastEmbedEmbedder:
    """Local fastembed model. fastembed is imported lazily so the core stays standard-library only.

    The first use downloads the model (a few hundred MB) from its host, so that host must be
    reachable once; afterwards it runs offline. Nothing about the reviews is sent anywhere.
    """

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.name = model
        self._model: Any = None
        self._query_prefix, self._doc_prefix = next(
            (p for family, p in _PREFIXES.items() if model.startswith(family)), ("", "")
        )

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise SemanticUnavailable(
                f"Semantic search needs the optional fastembed package: {INSTALL_HINT}."
            ) from exc
        try:
            self._model = TextEmbedding(model_name=self.name)
        except Exception as exc:  # download, network, disk and format errors all mean "unusable"
            raise SemanticUnavailable(
                f"Could not load embedding model {self.name!r}: {exc}. The model host must be reachable "
                "on first use so the model can be downloaded; it is cached locally afterwards."
            ) from exc
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        return [[float(x) for x in v] for v in model.embed([self._doc_prefix + t for t in texts])]

    def embed_query(self, text: str) -> list[float]:
        model = self._load()
        vec = next(iter(model.query_embed(self._query_prefix + text)))
        return [float(x) for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity; 0.0 when either vector has zero norm so callers never see NaN."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _key(embedder_name: str, text: str) -> str:
    return hashlib.sha256((embedder_name + "\n" + text).encode("utf-8")).hexdigest()


def _load_cache(path: Path) -> dict[str, list[float]]:
    """Cached vectors, or {} if the file is missing or unusable (it is then simply rebuilt)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data["vectors"]
        if data.get("version") != _CACHE_VERSION or not isinstance(raw, dict):
            return {}
        return {
            k: [float(x) for x in v] for k, v in raw.items() if isinstance(v, list) and isinstance(k, str)
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def _write_cache(path: Path, vectors: dict[str, list[float]]) -> None:
    """Atomic write (temp file + rename) so an interrupted run never leaves a half-written cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": _CACHE_VERSION, "vectors": vectors}, fh)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class SemanticIndex:
    """Cosine-similarity search over review embeddings, one vector per review with text."""

    def __init__(
        self, places: Iterable[Place], embedder: Embedder, *, cache_path: Path | None = None
    ) -> None:
        self._embedder = embedder
        self._docs: list[tuple[str, str, Review, list[float]]] = []
        pending: list[tuple[str, str, Review]] = []
        for place in places:
            for review in place.reviews:
                if review.text and review.text.strip():
                    pending.append((place.place_id, place.name, review))

        cache = _load_cache(cache_path) if cache_path else {}
        keys = [_key(embedder.name, r.text) for _, _, r in pending]
        missing: dict[str, str] = {}  # key -> text; duplicates are embedded once
        for key, (_, _, review) in zip(keys, pending, strict=True):
            if key not in cache and key not in missing:
                missing[key] = review.text
        if missing:
            vectors = embedder.embed(list(missing.values()))
            if len(vectors) != len(missing):
                raise ValueError(f"embedder returned {len(vectors)} vectors for {len(missing)} texts")
            cache.update(zip(missing, (list(v) for v in vectors), strict=True))
            if cache_path:
                _write_cache(cache_path, cache)
        for key, (pid, name, review) in zip(keys, pending, strict=True):
            self._docs.append((pid, name, review, cache[key]))

    def __len__(self) -> int:
        return len(self._docs)

    def search(
        self,
        query: str,
        *,
        top: int = 20,
        min_rating: float | None = None,
        max_rating: float | None = None,
        min_score: float = 0.0,
    ) -> list[ReviewMatch]:
        """Reviews most similar in meaning to the query; rating bounds drop unrated reviews."""
        if not self._docs:
            return []
        qvec = self._embedder.embed_query(query)
        out = []
        for pid, name, review, vec in self._docs:
            if min_rating is not None and (review.rating is None or review.rating < min_rating):
                continue
            if max_rating is not None and (review.rating is None or review.rating > max_rating):
                continue
            score = cosine(qvec, vec)
            if score >= min_score:
                out.append(ReviewMatch(score, pid, name, review))
        out.sort(key=lambda m: (-m.score, m.place_id))
        return out[:top]


def hybrid_search(
    keyword: ReviewIndex,
    semantic: SemanticIndex,
    query: str,
    *,
    top: int = 20,
    min_rating: float | None = None,
    max_rating: float | None = None,
    k: int = 60,
) -> list[ReviewMatch]:
    """Reciprocal rank fusion of BM25 and semantic rankings.

    RRF needs only ranks, so the incomparable score scales (BM25 vs cosine) never have to be
    normalised. A review found by both lists outranks one found by a single list at similar depth.
    """
    depth = max(top * HYBRID_DEPTH_FACTOR, HYBRID_MIN_DEPTH)
    lists = (
        keyword.search(query, top=depth, min_rating=min_rating, max_rating=max_rating),
        semantic.search(
            query, top=depth, min_rating=min_rating, max_rating=max_rating, min_score=_MIN_POSITIVE_SCORE
        ),
    )
    fused: dict[int, float] = defaultdict(float)
    first: dict[int, ReviewMatch] = {}
    for ranked in lists:
        for rank, m in enumerate(ranked, start=1):
            ident = id(m.review)  # the same Review object is shared by both indexes
            fused[ident] += 1.0 / (k + rank)
            first.setdefault(ident, m)
    out = [
        ReviewMatch(score, first[i].place_id, first[i].place_name, first[i].review)
        for i, score in fused.items()
    ]
    out.sort(key=lambda m: (-m.score, m.place_id))
    return out[:top]


def aggregate_by_place(matches: list[ReviewMatch], *, top: int = 20) -> list[PlaceMatch]:
    """Places ranked by their top-3 review scores, as `ReviewIndex.search_places` does."""
    grouped: dict[str, list[ReviewMatch]] = defaultdict(list)
    for m in matches:
        grouped[m.place_id].append(m)
    out = []
    for pid, ms in grouped.items():
        ms.sort(key=lambda m: -m.score)
        out.append(PlaceMatch(pid, ms[0].place_name, sum(m.score for m in ms[:3]), len(ms), ms[0]))
    out.sort(key=lambda p: (-p.score, p.place_id))
    return out[:top]
