"""Optional "Grounding with Google Maps" provider (Gemini API `models.generateContent`).

Why: a supplementary way to do MEANING-BASED place discovery ("quiet bakery known for sourdough in
Riyadh") next to `google_agent_tools.py`. The model answers in prose and Google attaches the Maps
places it used as grounding chunks; this module keeps those chunks. It returns a short list, so
`text_search` never reports `saturated`.

CONFIRMED by tests/fixtures/gemini_generate_content_v1beta.json (Google's API description, v1beta,
fetched 2026-11-26): `POST v1beta/{+model}:generateContent` on https://generativelanguage.googleapis.com
with `model` = "models/{model}"; request `contents[]` (Content: role, parts[].text), `tools[]` with
`googleMaps` (Tool; GoogleMaps only has `enableWidget`, not sent), `toolConfig.retrievalConfig` with
`latLng` {latitude, longitude} and `languageCode`; response `candidates[].content.parts[].text`,
`candidates[].groundingMetadata.groundingChunks[].maps` {placeId "places/{id}", title, uri, text},
and `promptFeedback.blockReason`. The key is sent only in the `x-goog-api-key` header.

NOT confirmed (needs a call with a real key and Google's current pricing and terms pages):
- which model names support Maps grounding today (`DEFAULT_MODEL` is an unconfirmed alias; set
  `MAPS_GEMINI_MODEL` to override it);
- the price per request (the SKU `google.gemini_maps_grounding` has no free allowance, so free mode
  refuses it until the price is confirmed);
- quotas and rate limits;
- Google's data handling for prompts on the free tier (they may be used to improve products). The
  only variable content sent is the owner's query text, wrapped in `PROMPT_TEMPLATE`.

Note: the fixture names the grounding-support schema `GoogleAiGenerativelanguageV1betaGroundingSupport`
(not `GroundingSupport`); supports are not used here.

Keep the Google Maps source links (`maps_url`, `attributions`) with any output; callers own that duty.
Every request is charged to the budget immediately before it is sent.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .. import policy
from ..budget import Budget
from ..geography import BBox
from ..models import Hit, Place, Query, Review
from .base import PolicyStop, ProviderError, QuotaExceeded, SearchPage
from .google_agent_tools import AgentSearchResult

BASE_URL = "https://generativelanguage.googleapis.com"
DEFAULT_MODEL = "gemini-flash-latest"  # unconfirmed alias; override with MAPS_GEMINI_MODEL
SKU = "google.gemini_maps_grounding"
RAW_TEXT_LIMIT = 2_000
PROMPT_TEMPLATE = (
    "List the places that match this description, grounded in Google Maps. "
    "Answer with the matching places and nothing else.\n\nDescription: {text}"
)


class GeminiMaps:
    """Semantic place discovery through Gemini's Grounding with Google Maps."""

    name = "gemini_maps"

    def __init__(
        self,
        budget: Budget,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        model: str | None = None,
    ) -> None:
        self.budget = budget
        self._key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY", "")
        self._client = client or httpx.Client(timeout=60.0)
        self.model = model or os.environ.get("MAPS_GEMINI_MODEL") or DEFAULT_MODEL

    def available(self) -> bool:
        return bool(self._key)

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    def _url(self) -> str:
        model = self.model.removeprefix("models/")
        return f"{BASE_URL}/v1beta/models/{model}:generateContent"

    @staticmethod
    def _body(text: str, bbox: BBox | None, language: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": PROMPT_TEMPLATE.format(text=text)}]}],
            "tools": [{"googleMaps": {}}],
        }
        retrieval: dict[str, Any] = {}
        if bbox is not None:
            lat, lng = bbox.center
            retrieval["latLng"] = {"latitude": lat, "longitude": lng}
        if language:
            retrieval["languageCode"] = language
        if retrieval:
            body["toolConfig"] = {"retrievalConfig": retrieval}
        return body

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "x-goog-api-key": self._key}
        try:
            resp = self._client.post(self._url(), json=body, headers=headers)
        except httpx.HTTPError as e:
            raise ProviderError(f"gemini_maps: request failed ({type(e).__name__})") from None
        if not 200 <= resp.status_code < 300:
            detail = ""
            try:
                err = resp.json().get("error")
                detail = str(err.get("message") or "") if isinstance(err, dict) else str(err or "")
            except (ValueError, AttributeError):
                detail = resp.text[:200]
            # Enforcement first: before the quota mapping, and no retry exists to run.
            reason = policy.detect_enforcement(resp.status_code, detail or resp.text[:300])
            if reason:
                raise PolicyStop(self.name, self._scrub(f"HTTP {resp.status_code}: {detail or reason}"))
            if resp.status_code == 429:
                raise QuotaExceeded("gemini_maps: HTTP 429, the provider's quota was reached")
            msg = f"gemini_maps: HTTP {resp.status_code}" + (f": {detail}" if detail else "")
            raise ProviderError(self._scrub(msg))
        try:
            doc = resp.json()
        except ValueError:
            raise ProviderError("gemini_maps: response is not JSON") from None
        if not isinstance(doc, dict):
            raise ProviderError("gemini_maps: unexpected payload (expected a JSON object)")
        return doc

    def search_places(
        self, text: str, *, bbox: BBox | None = None, language: str | None = None
    ) -> AgentSearchResult:
        return self._search(text, bbox=bbox, language=language, level=0)

    def _search(self, text: str, *, bbox: BBox | None, language: str | None, level: int) -> AgentSearchResult:
        if not self._key:
            raise ProviderError("gemini_maps: GEMINI_API_KEY is not set")
        policy.check_google_key(self._key)
        body = self._body(text, bbox, language)
        self.budget.charge(SKU)
        doc = self._post(body)
        feedback = doc.get("promptFeedback")
        reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
        if reason:
            raise ProviderError(self._scrub(f"gemini_maps: prompt blocked ({reason})"))
        candidates = doc.get("candidates")
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            raise ProviderError("gemini_maps: response has no candidates")
        cand = candidates[0]
        content = cand.get("content") if isinstance(cand.get("content"), dict) else {}
        parts = content.get("parts") if isinstance(content.get("parts"), list) else []
        summary = "".join(p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str))
        meta = cand.get("groundingMetadata") if isinstance(cand.get("groundingMetadata"), dict) else {}
        chunks = meta.get("groundingChunks") if isinstance(meta.get("groundingChunks"), list) else []
        out = AgentSearchResult(summary=summary, raw_text=summary[:RAW_TEXT_LIMIT])
        seen: set[str] = set()
        for chunk in chunks:
            maps = chunk.get("maps") if isinstance(chunk, dict) else None
            if not isinstance(maps, dict):
                continue
            raw_id = maps.get("placeId")
            pid = raw_id.removeprefix("places/") if isinstance(raw_id, str) else ""
            if not pid or pid in seen:
                continue
            seen.add(pid)
            title = maps.get("title") if isinstance(maps.get("title"), str) else ""
            uri = maps.get("uri") if isinstance(maps.get("uri"), str) and maps.get("uri") else None
            place = Place(place_id=pid, name=title, maps_url=uri)
            place.hits.append(
                Hit(
                    provider=self.name,
                    query=text,
                    language=language,
                    cell=bbox.key() if bbox else None,
                    level=level,
                    rank=len(out.places) + 1,
                )
            )
            out.places.append(place)
            out.attributions[pid] = {"title": title, "url": uri or ""}
            if isinstance(maps.get("text"), str) and maps["text"]:
                out.notes[pid] = maps["text"]
        return out

    # -- Provider protocol --------------------------------------------------------------------

    def text_search(
        self,
        query: Query | str,
        *,
        bbox: BBox | None,
        level: int = 0,
        max_results: int = 60,
        **_: Any,
    ) -> SearchPage:
        """Adapter for the Provider protocol. Never saturated: grounding returns a short list."""
        text, language = (query.text, query.language) if isinstance(query, Query) else (str(query), None)
        res = self._search(text, bbox=bbox, language=language, level=level)
        return SearchPage(places=res.places[:max_results], saturated=False)

    def reviews(self, place_id: str, *, limit: int = 5, language: str | None = None) -> list[Review]:
        """Grounding offers no reviews."""
        return []
