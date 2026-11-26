"""MCP server: a thin adapter over the `maps` CLI.

Every tool builds an argv list, runs the click command in-process, parses the one JSON document it
prints and returns it. Usage errors (exit 2) come back as tool errors carrying the CLI's one-line
message. No research logic lives here.

Run: `python -m maps.mcp_server` (stdio) or `python -m maps.mcp_server --http` (needs MAPS_MCP_TOKEN).
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Literal

from click.testing import CliRunner

from maps import cli

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError("the MCP server needs the 'mcp' package: run `uv sync --extra mcp`") from exc

MISSING_MCP_MESSAGE = "the MCP server needs the 'mcp' package: run `uv sync --extra mcp`"
EXIT_POLICY_STOP = 3  # the CLI's exit code when a provider signalled enforcement
REPO_ROOT = Path(__file__).resolve().parent.parent

Depth = Literal["quick", "standard", "deep", "exhaustive"]
Cost = Literal["free", "quality"]
Tier = Literal["ids", "pro", "enterprise"]
Objective = Literal["best", "gems", "explore", "find", "hire", "investigate"]
Provider = Literal["google", "apify", "dataforseo", "sim"]

INSTRUCTIONS = """\
Google Maps research toolkit. Free-first: every tool defaults to cost="free" and never spends money.
Method:
1. maps_doctor and maps_budget: what is configured and what allowance is left.
2. maps_search (several keywords/languages, 'text:lang') or maps_discover (by meaning); report `coverage` gaps.
3. maps_rank re-ranks a stored run for free; try other objectives and filters before searching again.
4. maps_followups suggests searches; maps_reviews_fetch, then maps_reviews_search (mode="hybrid" = by meaning).
5. maps_reviews_integrity and maps_reviews_contributors give evidence, never a verdict.
Use cost="quality" only when the owner authorised spending and gave a ceiling (max_usd). The owner's recorded caps apply: a "Spending:" refusal is final, and nothing chargeable runs without a recorded spending authorization.
Never claim coverage the run did not achieve. A budget-guard stop is a result, not a failure. Maps content is temporary: do not store review text, addresses or rating tables.
"Account protection:" refusals are final; a `policy_stop` result means stop and tell the owner.
If you have repo access, follow skills/maps-research/SKILL.md (also resource maps://skill).
"""

_cli_lock = threading.Lock()


# --- CLI adapter ----------------------------------------------------------------------------


def run_cli(argv: list[str]) -> dict[str, Any]:
    """Run `maps <argv>` in-process; return its JSON document or raise ToolError."""
    with _cli_lock:
        result = CliRunner().invoke(cli.main, argv)
    if result.exit_code not in (0, EXIT_POLICY_STOP):
        raise ToolError(_error_line(result))
    try:
        doc = json.loads(result.stdout)
    except ValueError:
        raise ToolError("maps printed output that is not JSON") from None
    doc = doc if isinstance(doc, dict) else {"result": doc}
    if result.exit_code == EXIT_POLICY_STOP and "policy_stop" not in doc:
        raise ToolError(_error_line(result))
    return doc  # a policy stop is a result (its `policy_stop` block says what to do), not a tool error


def _error_line(result: Any) -> str:
    text = (result.stderr or "").strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.startswith("Error:"):
            return ln[len("Error:") :].strip() or "error"
    if lines:
        return lines[-1]
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        return f"{type(result.exception).__name__}: {result.exception}"
    return f"maps exited with code {result.exit_code}"


# --- argv builders --------------------------------------------------------------------------


def _repeat(flag: str, values: list[str] | None) -> list[str]:
    return [x for v in values or [] for x in (flag, v)]


def _opt(flag: str, value: Any) -> list[str]:
    return [] if value is None else [flag, str(value)]


def filter_args(
    min_rating: float | None = None,
    max_rating: float | None = None,
    min_reviews: int | None = None,
    max_reviews: int | None = None,
    types: list[str] | None = None,
    exclude_types: list[str] | None = None,
    open_only: bool = False,
    name_regex: str | None = None,
) -> list[str]:
    return [
        *_opt("--min-rating", min_rating),
        *_opt("--max-rating", max_rating),
        *_opt("--min-reviews", min_reviews),
        *_opt("--max-reviews", max_reviews),
        *_repeat("--type", types),
        *_repeat("--exclude-type", exclude_types),
        *(["--open-only"] if open_only else []),
        *_opt("--name-regex", name_regex),
    ]


def cost_args(cost: str, max_usd: float | None) -> list[str]:
    """Free-first: --max-usd is only ever sent together with --cost quality."""
    out = ["--cost", cost]
    if cost == "quality" and max_usd is not None:
        out += ["--max-usd", str(max_usd)]
    return out


def location_args(
    area: str | None = None,
    bbox: str | None = None,
    near: str | None = None,
    radius: float | None = None,
) -> list[str]:
    return [*_opt("--area", area), *_opt("--bbox", bbox), *_opt("--near", near), *_opt("--radius", radius)]


def discover_argv(
    text: str,
    area: str | None = None,
    bbox: str | None = None,
    near: str | None = None,
    radius: float | None = None,
    into: str | None = None,
    lang: str | None = None,
    cost: str = "free",
    max_usd: float | None = None,
    via: str = "agent_tools",
) -> list[str]:
    return [
        "discover",
        text,
        *location_args(area, bbox, near, radius),
        *_opt("--into", into),
        *_opt("--lang", lang),
        *([] if via == "agent_tools" else ["--via", via]),
        *cost_args(cost, max_usd),
    ]


def search_argv(
    queries: list[str],
    area: str | None = None,
    bbox: str | None = None,
    near: str | None = None,
    radius: float | None = None,
    depth: str | None = None,
    cost: str = "free",
    max_usd: float | None = None,
    providers: list[str] | None = None,
    tier: str | None = None,
    objective: str | None = None,
    top: int | None = None,
    resume: str | None = None,
    max_tasks: int | None = None,
    service_area_businesses: bool = False,
    **filters: Any,
) -> list[str]:
    return [
        "search",
        *_repeat("-q", queries),
        *location_args(area, bbox, near, radius),
        *_opt("--depth", depth),
        *_repeat("--provider", providers),
        *_opt("--tier", tier),
        *_opt("--objective", objective),
        *_opt("--top", top),
        *_opt("--resume", resume),
        *_opt("--max-tasks", max_tasks),
        *(["--service-area-businesses"] if service_area_businesses else []),
        *filter_args(**filters),
        *cost_args(cost, max_usd),
    ]


# --- server ---------------------------------------------------------------------------------

server = MCPServer("maps-research", instructions=INSTRUCTIONS)


@server.tool()
def maps_doctor() -> dict[str, Any]:
    """Show what is configured. Cost: free, makes no API call and prints no secret values."""
    return run_cli(["doctor"])


@server.tool()
def maps_policy_status() -> dict[str, Any]:
    """Account protection: provider registry, excluded methods, approvals, open stops, AI host. Cost: free.

    Read-only on purpose: approvals and clearing a stop are the owner's decisions, made with the
    `maps policy` command line, never through this server.
    """
    return run_cli(["policy", "status"])


@server.tool()
def maps_budget() -> dict[str, Any]:
    """This month's usage against free allowances. Cost: free, reads the local ledger only.

    The ledger only sees requests made through this toolkit; usage elsewhere on the same Google
    project is invisible, so keep a quota cap in the provider's console too.
    """
    return run_cli(["budget"])


@server.tool()
def maps_area(name: str) -> dict[str, Any]:
    """Resolve a place name to a search box. Cost: 1 Google Text Search Pro call, cached 30 days."""
    return run_cli(["area", name])


@server.tool()
def maps_search(
    queries: list[str],
    area: str | None = None,
    bbox: str | None = None,
    near: str | None = None,
    radius: float | None = None,
    depth: Depth | None = None,
    cost: Cost = "free",
    max_usd: float | None = None,
    providers: list[Provider] | None = None,
    tier: Tier | None = None,
    objective: Objective | None = None,
    top: int | None = None,
    resume: str | None = None,
    max_tasks: int | None = None,
    service_area_businesses: bool = False,
    min_rating: float | None = None,
    max_rating: float | None = None,
    min_reviews: int | None = None,
    max_reviews: int | None = None,
    types: list[str] | None = None,
    exclude_types: list[str] | None = None,
    open_only: bool = False,
    name_regex: str | None = None,
) -> dict[str, Any]:
    """Discover places for keywords in an area, then filter and rank them.

    Cost: each search request is billed to the chosen provider: Google Text Search per page of 20
    (see tier for free allowances); the simulator (providers=["sim"]) is free. With cost="free" the
    run stops cleanly before exceeding a free allowance and reports what it could not cover.
    cost="quality" allows paid requests up to max_usd dollars; max_usd is ignored with cost="free".
    Area: give one of area (name; costs 1 Google call the first time), bbox "S,W,N,E" (free) or
    near "LAT,LNG" with radius in meters (free). Queries may be 'text:lang', e.g. 'bakery:en'.
    service_area_businesses: also return pure service-area businesses (Google only); they may have
    no coordinates.
    Output: run_id, coverage (what was and was not searched), budget, matched, results.
    Results are stored as a run; maps_rank re-ranks them without further cost.
    """
    return run_cli(
        search_argv(
            queries,
            area=area,
            bbox=bbox,
            near=near,
            radius=radius,
            depth=depth,
            cost=cost,
            max_usd=max_usd,
            providers=providers,
            tier=tier,
            objective=objective,
            top=top,
            resume=resume,
            max_tasks=max_tasks,
            service_area_businesses=service_area_businesses,
            min_rating=min_rating,
            max_rating=max_rating,
            min_reviews=min_reviews,
            max_reviews=max_reviews,
            types=types,
            exclude_types=exclude_types,
            open_only=open_only,
            name_regex=name_regex,
        )
    )


@server.tool()
def maps_discover(
    text: str,
    area: str | None = None,
    bbox: str | None = None,
    near: str | None = None,
    radius: float | None = None,
    into: str | None = None,
    lang: str | None = None,
    cost: Cost = "free",
    max_usd: float | None = None,
    via: Literal["agent_tools", "gemini"] = "agent_tools",
) -> dict[str, Any]:
    """Find places by meaning, e.g. "quiet bakery known for sourdough". Cost: free while Experimental.

    Uses Google's Maps Experience agent tools and returns a short curated list, not a census. Free
    while Google keeps the tools Experimental (unconfirmed); unverified against the live service.
    The location (area, bbox or near+radius) is optional; the text may name the place. into adds the
    places to an existing run, otherwise a new run is created. Places are ID and name only: follow
    with maps_details (missing=true), then maps_rank. Keep the Google Maps links with anything you
    pass on. via: agent_tools (default, needs GOOGLE_MAPS_API_KEY) or gemini (Gemini grounded with
    Google Maps, needs GEMINI_API_KEY). Account protection: needs MAPS_AI_HOST naming an AI host whose
    data settings the owner approved. A result with `policy_stop` means: stop and tell the owner.
    """
    return run_cli(discover_argv(text, area, bbox, near, radius, into, lang, cost, max_usd, via))


@server.tool()
def maps_rank(
    run_id: str,
    objective: Objective | None = None,
    top: int | None = None,
    min_rating: float | None = None,
    max_rating: float | None = None,
    min_reviews: int | None = None,
    max_reviews: int | None = None,
    types: list[str] | None = None,
    exclude_types: list[str] | None = None,
    open_only: bool = False,
    name_regex: str | None = None,
) -> dict[str, Any]:
    """Re-filter and re-rank a stored run. Cost: free, makes no API call.

    Objectives: best, gems (hidden high-quality places), explore (varied), find (match most keywords),
    hire (reliable quality), investigate (candidates for a closer look).
    """
    return run_cli(
        [
            "rank",
            run_id,
            *_opt("--objective", objective),
            *_opt("--top", top),
            *filter_args(
                min_rating, max_rating, min_reviews, max_reviews, types, exclude_types, open_only, name_regex
            ),
        ]
    )


@server.tool()
def maps_followups(run_id: str) -> dict[str, Any]:
    """Suggest what to search next from a run's results. Cost: free, makes no API call.

    Lists frequent place types and name words that the run's queries did not use. You decide which
    deserve a new search.
    """
    return run_cli(["followups", run_id])


@server.tool()
def maps_census(
    types: list[str],
    area: str | None = None,
    bbox: str | None = None,
    near: str | None = None,
    radius: float | None = None,
    min_rating: float | None = None,
    max_rating: float | None = None,
) -> dict[str, Any]:
    """Count places of a type with exact rating bounds. Cost: 1 Google Places Aggregate call.

    Text Search cannot filter by maximum rating; this can, and counts without paging. Area: area
    name (1 extra Google call the first time), bbox "S,W,N,E" or near "LAT,LNG" plus radius.
    """
    return run_cli(
        [
            "census",
            *location_args(area, bbox, near, radius),
            *_repeat("--type", types),
            *_opt("--min-rating", min_rating),
            *_opt("--max-rating", max_rating),
        ]
    )


@server.tool()
def maps_details(
    run_id: str,
    missing: bool = False,
    tier: Tier | None = None,
    cost: Cost = "free",
    max_usd: float | None = None,
    objective: Objective | None = None,
    top: int | None = None,
    min_rating: float | None = None,
    max_rating: float | None = None,
    min_reviews: int | None = None,
    max_reviews: int | None = None,
    types: list[str] | None = None,
    exclude_types: list[str] | None = None,
    open_only: bool = False,
    name_regex: str | None = None,
) -> dict[str, Any]:
    """Fetch Google place details. Cost: 1 Place Details call per place (see tier).

    Fetches places without a name (missing=true) or the top N of the ranked, filtered places.
    Results are stored in the run. Stops cleanly when the budget or provider quota refuses.
    cost="quality" allows paid calls up to max_usd dollars; max_usd is ignored with cost="free".
    """
    return run_cli(
        [
            "details",
            run_id,
            *(["--missing"] if missing else []),
            *_opt("--tier", tier),
            *_opt("--objective", objective),
            *_opt("--top", top),
            *filter_args(
                min_rating, max_rating, min_reviews, max_reviews, types, exclude_types, open_only, name_regex
            ),
            *cost_args(cost, max_usd),
        ]
    )


@server.tool()
def maps_reviews_fetch(
    run_id: str,
    provider: Provider | None = None,
    top: int | None = None,
    limit: int | None = None,
    cost: Cost = "free",
    max_usd: float | None = None,
    objective: Objective | None = None,
    min_rating: float | None = None,
    max_rating: float | None = None,
    min_reviews: int | None = None,
    max_reviews: int | None = None,
    types: list[str] | None = None,
    exclude_types: list[str] | None = None,
    open_only: bool = False,
    name_regex: str | None = None,
) -> dict[str, Any]:
    """Fetch reviews for the top-ranked places that have none yet.

    Cost: Google Place Details Enterprise + Atmosphere per place (1,000 free calls/month); Apify and
    DataForSEO bill per review; sim is free. Picks the first `top` ranked, filtered places without
    stored reviews, so repeating the call moves down the ranking. Google returns at most 5 reviews
    per place. Stops cleanly on budget or provider quota. max_usd is ignored with cost="free".
    """
    return run_cli(
        [
            "reviews",
            "fetch",
            run_id,
            *_opt("--provider", provider),
            *_opt("--top", top),
            *_opt("--limit", limit),
            *_opt("--objective", objective),
            *filter_args(
                min_rating, max_rating, min_reviews, max_reviews, types, exclude_types, open_only, name_regex
            ),
            *cost_args(cost, max_usd),
        ]
    )


@server.tool()
def maps_reviews_search(
    run_id: str,
    query: str,
    top: int | None = None,
    min_rating: float | None = None,
    max_rating: float | None = None,
    by_place: bool = False,
    mode: Literal["keyword", "semantic", "hybrid"] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Search review text. Cost: free, makes no API call.

    mode: keyword (BM25, default), semantic (by meaning: finds paraphrases and other languages) or
    hybrid (both fused). Semantic and hybrid run a local model, so review text never leaves this
    machine; the first use downloads it and needs `uv sync --extra semantic`. model overrides it.
    Only searches reviews already fetched into this run (maps_reviews_fetch); Google returns at most
    5 per place, so absence of a match is weak evidence. min/max_rating filter the review's own stars.
    """
    return run_cli(
        [
            "reviews",
            "search",
            run_id,
            query,
            *_opt("--top", top),
            *_opt("--min-rating", min_rating),
            *_opt("--max-rating", max_rating),
            *(["--by-place"] if by_place else []),
            *_opt("--mode", mode),
            *_opt("--model", model),
        ]
    )


@server.tool()
def maps_reviews_integrity(
    run_id: str, places: list[str] | None = None, top: int | None = None
) -> dict[str, Any]:
    """Signals that a place's reviews may be manipulated. Cost: free, makes no API call.

    Without places, covers every place in the run with at least one fetched review, most flags first;
    top keeps the first N. Evidence to weigh (rating shape, bursts, repeated text, one-time
    reviewers), never a verdict.
    """
    return run_cli(["reviews", "integrity", run_id, *_repeat("--place", places), *_opt("--top", top)])


@server.tool()
def maps_reviews_contributors(
    run_id: str,
    liked: list[str] | None = None,
    expand: int | None = None,
    min_rating: float | None = None,
    limit: int | None = None,
    cost: Cost = "free",
    max_usd: float | None = None,
) -> dict[str, Any]:
    """Reviewers who reviewed several places in the run. Cost: free, unless expand is used.

    liked: place IDs you like; also lists places endorsed by people who endorsed them.
    expand: look up N contributors who rated a liked place >= min_rating (default 4.0) and add the
    places they reviewed (limit reviews each, default 10) to the run. Needs liked and APIFY_TOKEN;
    billed by Apify per result (cost="free" allows it only inside the monthly credit); unverified
    against the live actor. It reads individual reviewers' public histories: see docs/permissions.md.
    max_usd is ignored with cost="free".
    """
    return run_cli(
        [
            "reviews",
            "contributors",
            run_id,
            *_repeat("--liked", liked),
            *_opt("--expand", expand),
            *_opt("--min-rating", min_rating),
            *_opt("--limit", limit),
            *cost_args(cost, max_usd),
        ]
    )


@server.tool()
def maps_runs() -> dict[str, Any]:
    """List stored runs, newest first. Cost: free."""
    return run_cli(["runs"])


@server.tool()
def maps_purge(older_than: int | None = None, all: bool = False) -> dict[str, Any]:
    """Delete stored run data. Cost: free. Search already purges runs past the retention window.

    older_than: delete runs older than this many days (default: the retention window).
    all: delete every run and the area cache.
    """
    return run_cli(["purge", *_opt("--older-than", older_than), *(["--all"] if all else [])])


@server.resource("maps://skill", mime_type="text/markdown", description="The research method (SKILL.md).")
def skill_text() -> str:
    return _read_repo_file("skills/maps-research/SKILL.md")


@server.resource("maps://agents", mime_type="text/markdown", description="Rules for working in this repo.")
def agents_text() -> str:
    return _read_repo_file("AGENTS.md")


def _read_repo_file(rel: str) -> str:
    try:
        return (REPO_ROOT / rel).read_text(encoding="utf-8")
    except OSError:
        return f"{rel} is not available on this server."


# --- HTTP auth ------------------------------------------------------------------------------


def check_bearer(header: str | None, token: str) -> bool:
    """True when header is exactly `Bearer <token>` (constant-time comparison)."""
    if not token or not header:
        return False
    scheme, _, given = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(given.encode(), token.encode())


class BearerAuthMiddleware:
    """Pure ASGI middleware: 401 unless the Authorization header carries the right bearer token."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"authorization"), None)
        if check_bearer(header, self.token):
            await self.app(scope, receive, send)
            return
        body = b'{"error":"unauthorized"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def http_app(token: str, host: str = "127.0.0.1") -> Any:
    app = server.streamable_http_app(host=host)
    app.add_middleware(BearerAuthMiddleware, token=token)
    return app


# --- entry point ----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m maps.mcp_server", description="maps-research MCP server")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP (needs MAPS_MCP_TOKEN)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args(argv)

    if not args.http:
        server.run()
        return

    token = os.environ.get("MAPS_MCP_TOKEN", "")
    if not token:
        print("MAPS_MCP_TOKEN is not set: refusing to serve HTTP without a token", file=sys.stderr)
        raise SystemExit(2)

    import uvicorn

    uvicorn.run(http_app(token, args.host), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
