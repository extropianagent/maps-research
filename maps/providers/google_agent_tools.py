"""Google "Maps Experience agent tools" (formerly "Maps Grounding Lite") MCP provider.

Why: Google's own MCP server answers natural-language place questions ("quiet bakery known for
sourdough"). The toolkit uses it for SEMANTIC DISCOVERY next to keyword Text Search, which cannot
express that kind of intent. It returns a short curated list (5 places by default, 10 at most), so
`text_search` never reports `saturated`: there is no cap-ranking to subdivide.

Transport: MCP Streamable HTTP, spoken directly with httpx (no new dependency): JSON-RPC 2.0 POSTs
to one endpoint, `initialize` -> `notifications/initialized` -> `tools/list` -> `tools/call`.
Replies are plain JSON or SSE-framed; both are parsed. Auth is the `X-Goog-Api-Key` header only. The
input schema is still read from `tools/list` (free) so a changed schema fails loudly, not silently.

CONFIRMED by the live `tools/list` response (captured 2026-11-26, saved verbatim as
tests/fixtures/google_agent_tools_tools_list.json): the endpoint answers `initialize` (protocol
"2025-06-18", serverInfo "StatelessServer", no session id header) and `tools/list` without a key.
Tools offered: search_places, lookup_weather, compute_routes, search_along_route, resolve_names,
resolve_maps_urls; only search_places is used here. Input (camelCase): `textQuery` (required),
`locationBias` = {circle: {center: {latitude, longitude}, radiusMeters <= 50,000}} (a circle is the
only bias shape), `languageCode`, `regionCode`, `pageSize` (1-10, default 5; above 5 is a higher
billing tier), `includeExtendedDetails` (true triggers the Pro tier). Output (`outputSchema`):
`places[]` of PlaceView {id, place "places/{id}", location, googleMapsLinks, attribution
{title, url}}, a `summary` whose zero-based citations "[0]" index into `places`, and `contentToken`.
There is NO place-name field. The tool description requires grounded output to be attributed with
the `attribution` field, so this module keeps it (`attributions`) and never passes it off as a name.

Free-first: `pageSize` is clamped to 1..5 and anything larger is refused; `includeExtendedDetails`
is never sent.

STILL UNCONFIRMED (needs a call with a real key): behaviour of `tools/call`, billing and quota,
whether the text content mirrors `structuredContent` (results are read from `structuredContent`,
else a JSON document in the first text item, else a regex over the text), and the Terms of Service
duties around retention. Update `docs/live-verification.md` and this module together when settled.

Terms (unverified): keep Google Maps source links with any output, and do not send the output to a
model that trains on or retains its input. Callers own that duty; this module keeps `maps_url`,
`attributions` and the `summary` so they can.

Every request is charged to the budget immediately before it is sent. A `tools/call` is charged to
`google.agent_tools.search`; `initialize`, `notifications/initialized` and each `tools/list` page are
not billable but are requests all the same: they are charged to the zero-price SKU
`google.agent_tools.setup`, so they count toward `max_requests`, obey the kill switch and the daily cap,
and obey the repeated-request rule.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .. import policy
from ..budget import Budget
from ..geography import BBox
from ..models import Hit, Place, Query, Review
from .base import PolicyStop, ProviderError, QuotaExceeded, SearchPage
from .http import make_client, request_timeout

DEFAULT_ENDPOINT = "https://mapstools.googleapis.com/mcp"
TIMEOUT_S = 60.0  # one request's own timeout; the run's remaining time can only shorten it
PROTOCOL_VERSION = "2025-06-18"
TOOL_NAME = "search_places"
SKU = "google.agent_tools.search"
SETUP_SKU = "google.agent_tools.setup"
RAW_TEXT_LIMIT = 2_000
MAX_BIAS_RADIUS_M = 50_000.0  # inputSchema: "The radius must be within 50,000 meters"
MAX_FREE_PAGE_SIZE = 5  # pageSize above 5 is billed at a higher tier

_QUERY_NAMES = ("textQuery", "text_query", "query", "text")
_BIAS_NAMES = ("locationBias", "location_bias")
_PAGE_SIZE_NAMES = ("pageSize", "page_size")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?\u061f\u3002])\s+")
_PLACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
_TEXT_ID_RES = (re.compile(r"places/([A-Za-z0-9_-]{10,})"), re.compile(r"\b(ChIJ[A-Za-z0-9_-]{10,})"))


@dataclass
class AgentSearchResult:
    places: list[Place] = field(default_factory=list)
    # The service's natural-language summary; its "[i]" citations index into `places` in order.
    summary: str = ""
    # Required attribution per place id ({"title", "url"}); show it with any grounded output.
    attributions: dict[str, dict[str, str]] = field(default_factory=dict)
    # Per place id: the summary sentences citing that place; kept apart because no Place field fits.
    notes: dict[str, str] = field(default_factory=dict)
    raw_text: str = ""  # first RAW_TEXT_LIMIT chars of the tool's text, for debugging and attribution


# -- pure helpers ---------------------------------------------------------------------------------


def parse_response_body(body: str, content_type: str, request_id: int | None) -> dict[str, Any] | None:
    """Decode a JSON or SSE-framed JSON-RPC reply; None when the body is empty.

    With SSE the stream may carry notifications before the reply, so the message matching the
    request id wins, else the last message that has a result or error.
    """
    body = body.strip()
    if not body:
        return None
    if "text/event-stream" in content_type or body.startswith(("event:", "data:", ":")):
        messages: list[dict[str, Any]] = []
        for block in re.split(r"\r?\n\r?\n", body):
            data = "\n".join(
                line[5:].lstrip(" ") if line.startswith("data:") else ""
                for line in block.splitlines()
                if line.startswith("data:")
            )
            if not data.strip():
                continue
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if isinstance(msg, dict):
                messages.append(msg)
        replies = [m for m in messages if "result" in m or "error" in m]
        for m in replies:
            if request_id is not None and m.get("id") == request_id:
                return m
        return replies[-1] if replies else None
    try:
        msg = json.loads(body)
    except ValueError:
        raise ProviderError("google_agent_tools: response is not JSON or SSE") from None
    if isinstance(msg, list):  # a batch reply; pick ours
        msg = next((m for m in msg if isinstance(m, dict) and m.get("id") == request_id), None)
    if not isinstance(msg, dict):
        raise ProviderError("google_agent_tools: unexpected payload (expected a JSON-RPC object)")
    return msg


def _resolve(node: Any, root: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Follow local `$ref`s ("#/$defs/X") so nested sub-schemas can be inspected."""
    while isinstance(node, dict) and isinstance(node.get("$ref"), str) and depth < 8:
        target: Any = root
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target.get(part) if isinstance(target, dict) else None
        node, depth = target, depth + 1
    return node if isinstance(node, dict) else {}


def _properties(node: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """Properties of a schema node, folding in oneOf/anyOf/allOf branches."""
    node = _resolve(node, root)
    props = dict(node.get("properties") or {})
    for key in ("oneOf", "anyOf", "allOf"):
        for branch in node.get(key) or []:
            props.update(_properties(branch, root))
    return props


def _first_key(props: dict[str, Any], *candidates: str) -> str | None:
    return next((c for c in candidates if c in props), None)


def build_location_bias(
    prop_schema: dict[str, Any], root: dict[str, Any], bbox: BBox
) -> dict[str, Any] | None:
    """Fill a circle bias per its sub-schema (the only shape offered); None when not recognisable."""
    sub = _properties(prop_schema, root)
    circle = sub.get("circle")
    if circle is not None:
        cprops = _properties(circle, root)
        ckey = _first_key(cprops, "center", "centre")
        if ckey:
            kprops = _properties(cprops[ckey], root)
            lat, lng = _first_key(kprops, "latitude", "lat"), _first_key(kprops, "longitude", "lng", "lon")
            if lat and lng:
                lat0, lng0 = bbox.center
                out: dict[str, Any] = {ckey: {lat: lat0, lng: lng0}}
                rkey = _first_key(cprops, "radius", "radius_meters", "radiusMeters")
                if rkey:
                    out[rkey] = min(max(bbox.circumradius_m, 1.0), MAX_BIAS_RADIUS_M)
                return {"circle": out}
    return None


@dataclass
class _Found:
    place_id: str
    lat: float | None = None
    lng: float | None = None
    url: str | None = None
    attribution: dict[str, str] | None = None
    index: int = -1  # position in the service's `places`, which the summary's citations use


def _normalise_id(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    v = v.removeprefix("places/")
    return v if _PLACE_ID_RE.match(v) else None


def _number(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _text(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _from_place_view(view: Any) -> _Found | None:
    """One PlaceView of the real outputSchema; None when it carries no usable place id."""
    if not isinstance(view, dict):
        return None
    pid = _normalise_id(view.get("id")) or _normalise_id(view.get("place"))
    if pid is None:
        return None
    loc = view.get("location") if isinstance(view.get("location"), dict) else {}
    links = view.get("googleMapsLinks") if isinstance(view.get("googleMapsLinks"), dict) else {}
    attr = view.get("attribution") if isinstance(view.get("attribution"), dict) else {}
    attribution = None
    if attr:
        attribution = {"title": _text(attr.get("title")) or "", "url": _text(attr.get("url")) or ""}
    return _Found(
        pid,
        _number(loc.get("latitude")),
        _number(loc.get("longitude")),
        _text(links.get("placeUrl")),
        attribution,
    )


def _notes_from_summary(summary: str, count: int) -> dict[int, str]:
    """For place index i, the summary sentences containing "[i]", joined by a space."""
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(summary.strip()) if s]
    notes: dict[int, str] = {}
    for i in range(count):
        cited = [s for s in sentences if f"[{i}]" in s]
        if cited:
            notes[i] = " ".join(cited)
    return notes


# -- provider -------------------------------------------------------------------------------------


class MapsAgentTools:
    """Semantic place discovery through Google's MCP server for Maps."""

    name = "google_agent_tools"

    def __init__(
        self,
        budget: Budget,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        endpoint: str = DEFAULT_ENDPOINT,
    ) -> None:
        self.budget = budget
        self._key = api_key if api_key is not None else os.environ.get("GOOGLE_MAPS_API_KEY", "")
        self._client = client or make_client(timeout=TIMEOUT_S)
        self._endpoint = endpoint
        self._next_id = 0
        self._session_id: str | None = None
        self._protocol: str | None = None
        self._initialized = False
        self._schema: dict[str, Any] | None = None

    def available(self) -> bool:
        return bool(self._key)

    # -- JSON-RPC over HTTP -------------------------------------------------------------------

    def _scrub(self, text: str) -> str:
        """Google sometimes echoes the key in error text; never let it reach a message."""
        return text.replace(self._key, "***") if self._key else text

    def _post(self, payload: dict[str, Any], *, notification: bool = False) -> dict[str, Any] | None:
        policy.check_google_key(self._key)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-Goog-Api-Key": self._key,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol:
            headers["MCP-Protocol-Version"] = self._protocol
        try:
            resp = self._client.post(
                self._endpoint,
                json=payload,
                headers=headers,
                timeout=request_timeout(self.budget, TIMEOUT_S),
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"google_agent_tools: request failed ({type(e).__name__})") from None
        detail = ""
        if not 200 <= resp.status_code < 300:
            try:
                err = resp.json().get("error")
                detail = str(err.get("message") or "") if isinstance(err, dict) else str(err or "")
            except (ValueError, AttributeError):
                detail = resp.text[:200]
            reason = policy.detect_enforcement(resp.status_code, policy.enforcement_text(resp.text))
            if reason:
                raise PolicyStop(self.name, self._scrub(f"HTTP {resp.status_code}: {detail or reason}"))
        if resp.status_code == 429:
            raise QuotaExceeded("google_agent_tools: HTTP 429, the provider's quota was reached")
        if not 200 <= resp.status_code < 300:
            msg = f"google_agent_tools: HTTP {resp.status_code}" + (f": {detail}" if detail else "")
            raise ProviderError(self._scrub(msg))
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
        if notification:
            return None
        msg = parse_response_body(resp.text, resp.headers.get("content-type", ""), payload.get("id"))
        if msg is None:
            raise ProviderError("google_agent_tools: empty response")
        err = msg.get("error")
        if err:
            if isinstance(err, dict):
                text = f"JSON-RPC error {err.get('code')}: {err.get('message')}"
            else:
                text = f"JSON-RPC error: {err}"
            reason = policy.detect_enforcement(200, policy.enforcement_text(err))
            if reason:
                raise PolicyStop(self.name, self._scrub(text[:300]))
            raise ProviderError(self._scrub(f"google_agent_tools: {text}"))
        return msg

    def _charge_setup(self, payload: dict[str, Any]) -> None:
        """A setup request is still a request: charged (at zero price) before it is sent."""
        self.budget.charge(SETUP_SKU, fingerprint=Budget.fingerprint("POST", self._endpoint, payload))

    def _rpc(
        self, method: str, params: dict[str, Any] | None = None, *, setup: bool = True
    ) -> dict[str, Any]:
        """Send one JSON-RPC request. `setup=False` only for `tools/call`, which its caller charged."""
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        if setup:
            self._charge_setup(payload)
        msg = self._post(payload)
        assert msg is not None
        result = msg.get("result")
        return result if isinstance(result, dict) else {}

    def _ensure_session(self) -> None:
        if self._initialized:
            return
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "maps-research", "version": "0"},
            },
        )
        proto = result.get("protocolVersion")
        self._protocol = proto if isinstance(proto, str) and proto else PROTOCOL_VERSION
        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self._charge_setup(note)
        self._post(note, notification=True)
        self._initialized = True

    def input_schema(self) -> dict[str, Any]:
        """The `search_places` inputSchema from `tools/list`, fetched once per instance."""
        if self._schema is None:
            self._ensure_session()
            tools: list[Any] = []
            cursor: str | None = None
            for _ in range(10):  # tools/list may paginate
                result = self._rpc("tools/list", {"cursor": cursor} if cursor else None)
                tools += result.get("tools") or []
                cursor = result.get("nextCursor")
                if not cursor or any(isinstance(t, dict) and t.get("name") == TOOL_NAME for t in tools):
                    break
            tool = next((t for t in tools if isinstance(t, dict) and t.get("name") == TOOL_NAME), None)
            if tool is None:
                names = [t.get("name") for t in tools if isinstance(t, dict)]
                raise ProviderError(f"google_agent_tools: tool {TOOL_NAME!r} not offered; found {names}")
            schema = tool.get("inputSchema")
            self._schema = schema if isinstance(schema, dict) else {}
        return self._schema

    # -- argument mapping and result parsing --------------------------------------------------

    def _arguments(
        self, text: str, bbox: BBox | None, language: str | None, page_size: int | None = None
    ) -> dict[str, Any]:
        schema = self.input_schema()
        props = _properties(schema, schema)
        qkey = _first_key(props, *_QUERY_NAMES)
        if qkey is None:
            required = [r for r in schema.get("required") or [] if r in props]
            strings = [r for r in required if _resolve(props[r], schema).get("type") == "string"]
            if len(strings) != 1:
                raise ProviderError(
                    f"google_agent_tools: cannot tell which {TOOL_NAME} property takes the query; "
                    f"properties found: {sorted(props)}"
                )
            qkey = strings[0]
        args: dict[str, Any] = {qkey: text}
        lkey = next((k for k in props if "language" in k.lower()), None)
        if language and lkey:
            args[lkey] = language
        bkey = _first_key(props, *_BIAS_NAMES)
        if bbox is not None and bkey:
            bias = build_location_bias(props[bkey], schema, bbox)
            if bias:
                args[bkey] = bias
        pkey = _first_key(props, *_PAGE_SIZE_NAMES)
        if page_size is not None and pkey:
            args[pkey] = page_size
        return args

    @staticmethod
    def _parse(result: dict[str, Any]) -> tuple[list[_Found], str, str, str]:
        """(places, summary, raw text, source) from structuredContent, else JSON text, else regex."""
        texts = [
            b["text"]
            for b in result.get("content") or []
            if isinstance(b, dict) and isinstance(b.get("text"), str)
        ]
        blob = "\n".join(texts)
        doc: Any = result.get("structuredContent")
        if not (isinstance(doc, dict) and isinstance(doc.get("places"), list)):
            doc = None
            if texts:
                try:
                    inner = json.loads(texts[0])
                except ValueError:
                    inner = None
                if isinstance(inner, dict) and isinstance(inner.get("places"), list):
                    doc = inner
        if doc is not None:
            found: dict[str, _Found] = {}
            for n, view in enumerate(doc["places"]):
                f = _from_place_view(view)
                if f:
                    f.index = n
                    found.setdefault(f.place_id, f)
            summary = doc.get("summary")
            return list(found.values()), summary if isinstance(summary, str) else "", blob, "json"
        ids: dict[str, _Found] = {}
        for rx in _TEXT_ID_RES:
            for m in rx.finditer(blob):
                ids.setdefault(m.group(1), _Found(m.group(1)))
        return list(ids.values()), "", blob, "regex"

    # -- public API ---------------------------------------------------------------------------

    def search_places(
        self,
        text: str,
        *,
        bbox: BBox | None = None,
        language: str | None = None,
        page_size: int | None = None,
    ) -> AgentSearchResult:
        """Semantic place search. Free-first: `pageSize` is sent only when asked, clamped to 1..5.

        Asking for more than 5 raises ValueError (larger pages are billed at a higher tier that this
        toolkit does not use). `includeExtendedDetails` is never sent (it triggers the Pro tier).
        """
        if page_size is not None:
            if page_size > MAX_FREE_PAGE_SIZE:
                raise ValueError(
                    f"page_size {page_size} > {MAX_FREE_PAGE_SIZE}: larger pages are billed at a higher "
                    "tier that this toolkit does not use"
                )
            page_size = max(page_size, 1)
        return self._search(text, bbox=bbox, language=language, level=0, page_size=page_size)

    def _search(
        self,
        text: str,
        *,
        bbox: BBox | None,
        language: str | None,
        level: int,
        page_size: int | None = None,
    ) -> AgentSearchResult:
        if not self._key:
            raise ProviderError("google_agent_tools: GOOGLE_MAPS_API_KEY is not set")
        # Refuse before the free setup calls (initialize, tools/list) when the gateway would refuse the search.
        self.budget.check(SKU)
        args = self._arguments(text, bbox, language, page_size)
        call = {"name": TOOL_NAME, "arguments": args}
        self.budget.charge(SKU, fingerprint=Budget.fingerprint("POST", self._endpoint, call))
        result = self._rpc("tools/call", call, setup=False)
        if result.get("isError"):
            blocks = [b["text"] for b in result.get("content") or [] if isinstance(b, dict) and b.get("text")]
            detail = " ".join(blocks)
            if policy.detect_enforcement(200, policy.enforcement_text(blocks)):
                raise PolicyStop(self.name, self._scrub(f"tool error: {detail[:300]}"))
            raise ProviderError(self._scrub(f"google_agent_tools: tool error: {detail[:300]}".rstrip(": ")))
        found, summary, blob, _source = self._parse(result)
        out = AgentSearchResult(summary=summary, raw_text=blob[:RAW_TEXT_LIMIT])
        by_index = _notes_from_summary(summary, max((f.index for f in found), default=-1) + 1)
        for i, f in enumerate(found, start=1):
            place = Place(place_id=f.place_id, lat=f.lat, lng=f.lng, maps_url=f.url)
            place.hits.append(
                Hit(
                    provider=self.name,
                    query=text,
                    language=language,
                    cell=bbox.key() if bbox else None,
                    level=level,
                    rank=i,
                )
            )
            out.places.append(place)
            if f.attribution:
                out.attributions[f.place_id] = f.attribution
            if f.index in by_index:
                out.notes[f.place_id] = by_index[f.index]
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
        """Adapter for the Provider protocol. Never saturated: the tool returns a short curated list."""
        text, language = (query.text, query.language) if isinstance(query, Query) else (str(query), None)
        res = self._search(text, bbox=bbox, language=language, level=level)
        return SearchPage(places=res.places[:max_results], saturated=False)

    def reviews(self, place_id: str, *, limit: int = 5, language: str | None = None) -> list[Review]:
        """This tool offers no reviews."""
        return []
