"""Google "Maps Experience agent tools" (formerly "Maps Grounding Lite") MCP provider.

Why: Google's own MCP server answers natural-language place questions ("quiet bakery known for
sourdough"). The toolkit uses it for SEMANTIC DISCOVERY next to keyword Text Search, which cannot
express that kind of intent. It returns a short curated list (documented as up to 5 places), so
`text_search` never reports `saturated`: there is no cap-ranking to subdivide.

Transport: MCP Streamable HTTP, spoken directly with httpx (no new dependency): JSON-RPC 2.0 POSTs
to one endpoint, `initialize` -> `notifications/initialized` -> `tools/list` -> `tools/call`.
Replies are plain JSON or SSE-framed; both are parsed. Auth is the `X-Goog-Api-Key` header only.

What could be CONFIRMED (search excerpts of developers.google.com, 2026-11-26; the site itself is
blocked from this machine): the endpoint, the tool name `search_places`, that `text_query` is the
mandatory input, that `location_bias` carries a `circle` (center and optional radius), and that
the proto-style names are camelCase (`textQuery`, `languageCode`) while prose uses snake_case.

What could NOT be confirmed: the exact `inputSchema` JSON, the result shape (`structuredContent`
vs text), the field names for place ids, names and links, whether a rectangle bias exists, the
protocol version the server negotiates, and billing. Nothing here is verified against a live call.
So the input schema is DISCOVERED from `tools/list` at runtime and mapped by property name, and
results are parsed defensively (JSON walk plus regex). When a live call settles these, update
`docs/live-verification.md` and this module together.

Terms (from the same excerpts, unverified): keep Google Maps source links with any output, and do
not send the output to a model that trains on or retains its input. Callers own that duty; this
module keeps `maps_url` and the tool's summary text (`notes`) so they can.

Every `tools/call` is charged to the budget immediately before it is sent; `initialize` and
`tools/list` are not billable.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..budget import Budget
from ..geography import BBox
from ..models import Hit, Place, Query, Review
from .base import ProviderError, QuotaExceeded, SearchPage

DEFAULT_ENDPOINT = "https://mapstools.googleapis.com/mcp"
PROTOCOL_VERSION = "2025-06-18"
TOOL_NAME = "search_places"
SKU = "google.agent_tools.search"
RAW_TEXT_LIMIT = 2_000
MAX_BIAS_RADIUS_M = 50_000.0  # Places APIs cap circle radii at 50 km; assumed to hold here too

_QUERY_NAMES = ("text_query", "textQuery", "query", "text")
_BIAS_NAMES = ("location_bias", "locationBias")
_ID_KEYS = ("place_id", "placeId", "id", "place")
_NAME_KEYS = ("displayName", "display_name", "name", "title")
_URL_KEYS = ("googleMapsUri", "google_maps_uri", "googleMapsUrl", "mapsUri", "maps_url", "url", "link", "uri")
_NOTE_KEYS = ("summary", "editorialSummary", "description", "snippet", "text")
_PLACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
_TEXT_ID_RES = (re.compile(r"places/([A-Za-z0-9_-]{10,})"), re.compile(r"\b(ChIJ[A-Za-z0-9_-]{10,})"))


@dataclass
class AgentSearchResult:
    places: list[Place] = field(default_factory=list)
    # The tool's own summary text per place id; kept apart because no Place field fits it.
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


def build_location_bias(prop_schema: dict[str, Any], root: dict[str, Any], bbox: BBox) -> dict[str, Any] | None:
    """Fill a circle or rectangle bias per its sub-schema; None when it is not recognisable."""
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
    rect = sub.get("rectangle")
    if rect is not None:
        rprops = _properties(rect, root)
        if "low" in rprops and "high" in rprops:
            low = _properties(rprops["low"], root)
            lat, lng = _first_key(low, "latitude", "lat"), _first_key(low, "longitude", "lng", "lon")
            if lat and lng:
                return {
                    "rectangle": {
                        "low": {lat: bbox.south, lng: bbox.west},
                        "high": {lat: bbox.north, lng: bbox.east},
                    }
                }
    return None


@dataclass
class _Found:
    place_id: str
    name: str = ""
    url: str | None = None
    note: str = ""


def _text_of(v: Any) -> str:
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        return _text_of(v.get("text"))
    return ""


def _normalise_id(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    v = v.removeprefix("places/")
    return v if _PLACE_ID_RE.match(v) else None


def _is_google_url(v: Any) -> bool:
    return isinstance(v, str) and bool(re.match(r"https?://([\w-]+\.)*(google\.[a-z.]+|goo\.gl|g\.co)(/|$)", v))


def _walk(node: Any, found: dict[str, _Found]) -> None:
    """Collect places from any JSON shape: a dict with an id-like key describes one place."""
    if isinstance(node, list):
        for item in node:
            _walk(item, found)
    elif isinstance(node, dict):
        pid = next((p for k in _ID_KEYS if (p := _normalise_id(node.get(k)))), None)
        if pid:
            f = found.setdefault(pid, _Found(pid))
            f.name = f.name or next((t for k in _NAME_KEYS if (t := _text_of(node.get(k)))), "")
            f.name = "" if f.name.startswith("places/") else f.name
            f.url = f.url or next((u for k in _URL_KEYS if _is_google_url(u := node.get(k))), None)
            f.note = f.note or next((t for k in _NOTE_KEYS if (t := _text_of(node.get(k)))), "")
        for value in node.values():
            if isinstance(value, dict | list):
                _walk(value, found)


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
        self._client = client or httpx.Client(timeout=60.0)
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
            resp = self._client.post(self._endpoint, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise ProviderError(f"google_agent_tools: request failed ({type(e).__name__})") from None
        if resp.status_code == 429:
            raise QuotaExceeded("google_agent_tools: HTTP 429, the provider's quota was reached")
        if not 200 <= resp.status_code < 300:
            detail = ""
            try:
                err = resp.json().get("error")
                detail = str(err.get("message") or "") if isinstance(err, dict) else str(err or "")
            except (ValueError, AttributeError):
                detail = resp.text[:200]
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
            raise ProviderError(self._scrub(f"google_agent_tools: {text}"))
        return msg

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
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
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, notification=True)
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

    def _arguments(self, text: str, bbox: BBox | None, language: str | None) -> dict[str, Any]:
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
        return args

    @staticmethod
    def _parse(result: dict[str, Any]) -> tuple[list[_Found], str]:
        found: dict[str, _Found] = {}
        texts: list[str] = []
        _walk(result.get("structuredContent"), found)
        for block in result.get("content") or []:
            if not (isinstance(block, dict) and isinstance(block.get("text"), str)):
                continue
            text = block["text"]
            texts.append(text)
            try:
                _walk(json.loads(text), found)
            except ValueError:
                pass
        blob = "\n".join(texts)
        for rx in _TEXT_ID_RES:
            for m in rx.finditer(blob):
                found.setdefault(m.group(1), _Found(m.group(1)))
        # Regex hits found before a JSON walk of the same id would lose order; dict order is by
        # first sighting, structured content first, which is the order that carries intent.
        return list(found.values()), blob

    # -- public API ---------------------------------------------------------------------------

    def search_places(
        self, text: str, *, bbox: BBox | None = None, language: str | None = None
    ) -> AgentSearchResult:
        return self._search(text, bbox=bbox, language=language, level=0)

    def _search(self, text: str, *, bbox: BBox | None, language: str | None, level: int) -> AgentSearchResult:
        if not self._key:
            raise ProviderError("google_agent_tools: GOOGLE_MAPS_API_KEY is not set")
        args = self._arguments(text, bbox, language)
        self.budget.charge(SKU)
        result = self._rpc("tools/call", {"name": TOOL_NAME, "arguments": args})
        if result.get("isError"):
            detail = " ".join(
                b["text"] for b in result.get("content") or [] if isinstance(b, dict) and b.get("text")
            )
            raise ProviderError(self._scrub(f"google_agent_tools: tool error: {detail[:300]}".rstrip(": ")))
        found, blob = self._parse(result)
        out = AgentSearchResult(raw_text=blob[:RAW_TEXT_LIMIT])
        for i, f in enumerate(found, start=1):
            place = Place(place_id=f.place_id, name=f.name, maps_url=f.url)
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
            if f.note:
                out.notes[f.place_id] = f.note
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
