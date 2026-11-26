"""Command-line interface. Built for AI agents driving it from a shell.

Conventions, for every command:
- the result is ONE JSON document on stdout (`--format md` prints markdown where offered);
- progress and warnings go to stderr, so stdout can always be piped into a JSON parser;
- user errors exit 2 with a one-line message on stderr; so does an account-protection refusal
  (the line starts "Account protection:") and a spending-gateway refusal before the first request
  (the line starts "Spending:");
- stopping early because of the budget is not an error: exit 0, and the JSON says why;
- a provider's enforcement signal (suspension, abuse, terms warning) exits 3, and the JSON carries a
  top-level `policy_stop` block.
"""

from __future__ import annotations

import dataclasses
import functools
import importlib.util
import json
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from . import __version__, config, evals, filters, policy, render, retrieval, scoring, store
from .budget import DEPTH_CEILINGS, Budget, BudgetExceeded, GatewayRefusal, Ledger, current_month
from .geography import BBox
from .models import Hit, Place, Query
from .pricing import PRICES_CHECKED, SKUS
from .providers import http as provider_http
from .providers.apify import ApifyProvider, parse_contributor
from .providers.base import PolicyStop, ProviderError, QuotaExceeded
from .providers.dataforseo import DataForSEOProvider
from .providers.gemini_maps import GeminiMaps
from .providers.google_agent_tools import MapsAgentTools
from .providers.google_aggregate import PlacesAggregate
from .providers.google_places import GooglePlaces
from .reviews import ReviewIndex, co_review_graph, contributor_overlap, integrity_report
from .semantic import (
    DEFAULT_MODEL,
    FastEmbedEmbedder,
    SemanticIndex,
    SemanticUnavailable,
    aggregate_by_place,
    hybrid_search,
)
from .sim import SimProvider
from .store import RunStore

# Environment variables each provider needs. Only names are ever printed, never values.
ENV_VARS: dict[str, list[str]] = {
    "google": ["GOOGLE_MAPS_API_KEY"],
    "google_aggregate": ["GOOGLE_MAPS_API_KEY"],
    "google_agent_tools": ["GOOGLE_MAPS_API_KEY"],
    "gemini_maps": ["GEMINI_API_KEY"],
    "apify": ["APIFY_TOKEN"],
    "dataforseo": ["DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"],
    "sim": [],
}
SEARCH_PROVIDERS = ["google", "apify", "dataforseo", "sim"]
AREA_CACHE_DAYS = 30
DEFAULT_RADIUS_M = 2000.0
SIM_WORLD_PLACES = 600
SIM_WORLD_SEED = 7


class CliError(click.ClickException):
    """A user error: bad flags, missing credentials, unknown run. Exit code 2, one line."""

    exit_code = 2


STOP_ACTION = (
    "Stop. Do not retry or work around this. Tell the owner what the provider said. "
    "The provider stays blocked until the owner clears it with `maps policy clear-stop`."
)


def _stop_block(provider: str, detail: str) -> dict[str, str]:
    """Record an enforcement signal (the provider is blocked from now on) and describe it for the output."""
    policy.record_stop(provider, detail)
    return {"provider": provider, "detail": detail, "action": STOP_ACTION}


def _exit_on_stop(
    doc: dict[str, Any], stop: dict[str, str] | None, show: Callable[[dict[str, Any]], None] | None = None
) -> None:
    """Print the document (with the policy_stop block) and exit 3 when a provider signalled enforcement."""
    if stop is None:
        return
    doc["policy_stop"] = _stop_block(stop["provider"], stop["detail"])
    (show or emit)(doc)
    raise click.exceptions.Exit(3)


class MapsGroup(click.Group):
    """Turns provider and budget failures that escape a command into one-line errors."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except policy.PolicyViolation as e:
            click.echo(f"Account protection: {e}", err=True)
            raise click.exceptions.Exit(2) from e
        except PolicyStop as e:
            emit({"policy_stop": _stop_block(e.provider, e.detail)})
            raise click.exceptions.Exit(3) from e
        except ProviderError as e:
            raise click.ClickException(str(e)) from e
        except GatewayRefusal as e:
            click.echo(f"Spending: {e}", err=True)
            raise click.exceptions.Exit(2) from e
        except BudgetExceeded as e:
            raise CliError(f"budget: {e}") from e


# --- output helpers ---------------------------------------------------------------------------


def _clean(obj: Any) -> Any:
    """Make a document valid JSON: unlimited allowances (inf) become the string 'unlimited'."""
    if isinstance(obj, float) and not math.isfinite(obj):
        return "unlimited" if obj > 0 else None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def emit(doc: Any) -> None:
    click.echo(json.dumps(_clean(doc), ensure_ascii=False, indent=2))


def warn(msg: str) -> None:
    click.echo(msg, err=True)


def _open_run(run_id: str) -> RunStore:
    """Open a run for a command that shows or extends its places.

    A run with at least one sighting or review from a real provider (anything but the simulator) may
    only be shown to an approved AI host, so it is gated here, once, for every command that uses it.
    """
    try:
        run = RunStore.open(config.state_dir(), run_id)
    except FileNotFoundError:
        raise CliError("no such run: not a run ID in this state directory (see `maps runs`)") from None
    if _has_provider_data(run):
        policy.require_ai_host()
    return run


def _has_provider_review(run: RunStore) -> bool:
    """True when the run holds a review that did not come from the simulator."""
    return any(r.source != "sim" for p in run.places().values() for r in p.reviews)


def _has_provider_data(run: RunStore) -> bool:
    """True when the run holds a sighting or a review that did not come from the simulator."""
    places = run.places().values()
    return any(h.provider != "sim" for p in places for h in p.hits) or any(
        r.source != "sim" for p in places for r in p.reviews
    )


def _touch_spec(run: RunStore, **changes: Any) -> None:
    """Update a run's spec without refreshing its age.

    Retention counts from the spec's mtime. Replaying old results must not extend how long Maps
    content is kept, so the original mtime is restored after the write.
    """
    path = run.dir / "spec.json"
    st = path.stat()
    run.update_spec(**changes)
    os.utime(path, (st.st_atime, st.st_mtime))


# --- shared option groups -----------------------------------------------------------------------


def _option_group(key: str, names: list[str], options: list[Callable], build: Callable[..., Any]) -> Callable:
    """Add `options` to a command and hand their values to it as one object named `key`."""

    def decorate(f: Callable) -> Callable:
        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            parts = {n: kwargs.pop(n) for n in names}
            return f(*args, **{key: build(**parts)}, **kwargs)

        for opt in reversed(options):
            wrapper = opt(wrapper)
        return wrapper

    return decorate


@dataclass
class Location:
    area: str | None = None
    bbox: str | None = None
    near: str | None = None
    radius: float | None = None

    @property
    def given(self) -> bool:
        return bool(self.area or self.bbox or self.near)


def _build_location(
    area: str | None, bbox_raw: str | None, near: str | None, radius: float | None
) -> Location:
    if sum(bool(x) for x in (area, bbox_raw, near)) > 1:
        raise CliError("give only one of --area, --bbox, --near")
    if radius is not None and not near:
        raise CliError("--radius only applies with --near")
    return Location(area, bbox_raw, near, radius)


def location_options(f: Callable) -> Callable:
    return _option_group(
        "loc",
        ["area", "bbox_raw", "near", "radius"],
        [
            click.option(
                "--area",
                "area",
                metavar="NAME",
                help="Search area by name, e.g. 'Riyadh'. Costs 1 Google Text Search Pro call the "
                "first time (cached 30 days in the state dir).",
            ),
            click.option(
                "--bbox",
                "bbox_raw",
                metavar="S,W,N,E",
                help="Search area as south,west,north,east in degrees. Free.",
            ),
            click.option(
                "--near",
                "near",
                metavar="LAT,LNG",
                help="Centre of a circular search area; use with --radius. Free.",
            ),
            click.option(
                "--radius",
                "radius",
                type=float,
                metavar="METERS",
                help=f"Radius for --near (default {DEFAULT_RADIUS_M:g}).",
            ),
        ],
        _build_location,
    )(f)


def _build_filters(
    min_rating: float | None,
    max_rating: float | None,
    min_reviews: int | None,
    max_reviews: int | None,
    types: tuple[str, ...],
    exclude_types: tuple[str, ...],
    open_only: bool,
    name_regex: str | None,
) -> filters.Filters:
    if name_regex:
        try:
            re.compile(name_regex)
        except re.error as e:
            raise CliError(f"--name-regex is not a valid regular expression: {e}") from None
    return filters.Filters(
        min_rating=min_rating,
        max_rating=max_rating,
        min_reviews=min_reviews,
        max_reviews=max_reviews,
        types=list(types),
        exclude_types=list(exclude_types),
        open_only=open_only,
        name_regex=name_regex,
    )


def filter_options(f: Callable) -> Callable:
    return _option_group(
        "flt",
        [
            "min_rating",
            "max_rating",
            "min_reviews",
            "max_reviews",
            "types",
            "exclude_types",
            "open_only",
            "name_regex",
        ],
        [
            click.option(
                "--min-rating", "min_rating", type=float, help="Keep rating >= this (exact, inclusive)."
            ),
            click.option(
                "--max-rating", "max_rating", type=float, help="Keep rating <= this (exact, inclusive)."
            ),
            click.option("--min-reviews", "min_reviews", type=int, help="Keep review count >= this."),
            click.option("--max-reviews", "max_reviews", type=int, help="Keep review count <= this."),
            click.option(
                "--type",
                "types",
                multiple=True,
                metavar="TYPE",
                help="Keep places of this Google type, e.g. bakery. Repeatable (any of).",
            ),
            click.option(
                "--exclude-type",
                "exclude_types",
                multiple=True,
                metavar="TYPE",
                help="Drop places of this type. Repeatable.",
            ),
            click.option("--open-only", "open_only", is_flag=True, help="Drop permanently closed places."),
            click.option(
                "--name-regex",
                "name_regex",
                metavar="REGEX",
                help="Keep places whose name matches this case-insensitive regex.",
            ),
        ],
        _build_filters,
    )(f)


@dataclass
class Ranking:
    objective: str
    top: int | None


def ranking_options(default_top: int | None = 20) -> Callable:
    """Ranking flags. Objectives are spelled out so `--help` alone is enough for an agent."""
    objectives = "; ".join(f"{k}: {v}" for k, v in scoring.OBJECTIVES.items())
    return _option_group(
        "rk",
        ["objective", "top"],
        [
            click.option(
                "--objective",
                "objective",
                type=click.Choice(list(scoring.OBJECTIVES)),
                default="best",
                show_default=True,
                help=f"How to rank. {objectives}",
            ),
            click.option(
                "--top",
                "top",
                type=click.IntRange(min=1),
                default=default_top,
                show_default=default_top is not None,
                help="How many results to show.",
            ),
        ],
        Ranking,
    )


@dataclass
class Cost:
    mode: str
    max_usd: float | None


def _build_cost(cost_mode: str, max_usd: float | None) -> Cost:
    if max_usd is not None and not (math.isfinite(max_usd) and max_usd >= 0):
        raise CliError("--max-usd must be a finite number >= 0")
    if max_usd is not None and cost_mode != "quality":
        raise CliError("--max-usd only applies with --cost quality")
    return Cost(cost_mode, max_usd)


def cost_options(f: Callable) -> Callable:
    return _option_group(
        "cost",
        ["cost_mode", "max_usd"],
        [
            click.option(
                "--cost",
                "cost_mode",
                type=click.Choice(["free", "quality"]),
                default="free",
                show_default=True,
                help="free: refuse any request that would cost money and keep 10% headroom on "
                "free allowances. quality: allow paid requests up to --max-usd.",
            ),
            click.option(
                "--max-usd",
                "max_usd",
                type=float,
                metavar="USD",
                help="Spending authorised for this run; only with --cost quality.",
            ),
        ],
        _build_cost,
    )(f)


def format_option(f: Callable) -> Callable:
    return click.option(
        "--format",
        "fmt",
        type=click.Choice(["json", "md"]),
        default="json",
        show_default=True,
        help="json: one JSON document. md: a markdown report.",
    )(f)


def make_budget(cost: Cost, providers: list[str] | None = None, depth: str | None = None) -> Budget:
    """The spending gateway for one command.

    `providers` names who will be used, so the owner's per-search cap is checked against their billing
    subjects only; `depth` sets the per-run request and time ceilings.
    """
    ceilings: dict[str, Any] = {}
    if depth is not None:
        ceilings = dict(zip(("max_requests", "max_elapsed_s"), DEPTH_CEILINGS[depth], strict=True))
    subjects = None
    if providers is not None:
        subjects = sorted({s for n in providers if (s := policy.spend_subject(n))})
    return Budget(
        Ledger(config.ledger_path()),
        mode=cost.mode,
        max_usd=cost.max_usd or 0.0,
        subjects=subjects,
        **ceilings,
    )


def _area_users(loc: Location) -> list[str]:
    """Naming an area by --area costs a Google call, whatever else the command uses."""
    return ["google"] if loc.area else []


# --- providers and locations ------------------------------------------------------------------


def _require_ai_host_for_provider_data(names: list[str]) -> None:
    """Output of any real provider may only reach an approved AI host; only the simulator is exempt."""
    if any(n != "sim" for n in names):
        policy.require_ai_host()


def build_providers(names: list[str], budget: Budget, tier: str | None, bbox: BBox | None) -> list[Any]:
    """Construct providers by name after the account-protection check. One function so tests can swap in fakes.

    The simulator generates its world inside `bbox`, so the same box must be passed again when
    reviews are fetched later.
    """
    for name in names:
        policy.require_provider(name)
    _require_ai_host_for_provider_data(names)
    return _construct_providers(names, budget, tier, bbox)


def _gated(names: list[str], budget: Budget, tier: str | None, bbox: BBox | None) -> list[Any]:
    """Every use of a provider goes through here, so the checks hold even when tests swap the builder:
    the owner's approvals, the AI host, and the spending gateway (refused before the first request)."""
    for name in names:
        policy.require_provider(name)
    _require_ai_host_for_provider_data(names)
    for name in names:
        budget.preflight(name)
    return build_providers(names, budget, tier, bbox)


def _construct_providers(names: list[str], budget: Budget, tier: str | None, bbox: BBox | None) -> list[Any]:
    out: list[Any] = []
    for name in names:
        if name == "google":
            out.append(GooglePlaces(budget, tier=tier or "enterprise"))
        elif name == "google_aggregate":
            out.append(PlacesAggregate(budget))
        elif name == "google_agent_tools":
            out.append(MapsAgentTools(budget))
        elif name == "gemini_maps":
            out.append(GeminiMaps(budget))
        elif name == "apify":
            out.append(ApifyProvider(budget))
        elif name == "dataforseo":
            out.append(DataForSEOProvider(budget))
        elif name == "sim":
            out.append(
                SimProvider(
                    budget, area=bbox or config.SIM_AREA, n_places=SIM_WORLD_PLACES, seed=SIM_WORLD_SEED
                )
            )
        else:
            raise CliError(f"unknown provider {name!r}")
    return out


def build_agent_tools(budget: Budget) -> Any:
    """The Maps Experience agent-tools client. One function so tests can swap in a fake."""
    return MapsAgentTools(budget)


def build_discoverer(via: str, budget: Budget) -> Any:
    """The client behind `maps discover --via`. One function so tests can swap in a fake."""
    if via == "gemini":
        return GeminiMaps(budget)
    return build_agent_tools(budget)


def build_embedder(model: str) -> Any:
    """The local embedding model (loaded lazily). One function so tests can swap in a fake."""
    return FastEmbedEmbedder(model)


def build_contributor_provider(budget: Budget) -> Any:
    """The provider that looks up a contributor's reviews. One function so tests can swap in a fake."""
    return ApifyProvider(budget)


def _importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _unavailable(name: str) -> str:
    return f"{name}: set {' and '.join(ENV_VARS.get(name, []))}"


def _usable(providers: list[Any]) -> list[Any]:
    """Keep configured providers; warn about the rest; exit 2 naming the variables if none remain."""
    ok = [p for p in providers if p.available()]
    for p in providers:
        if p not in ok:
            warn(f"skipping provider {_unavailable(p.name)}")
    if not ok:
        raise CliError("no usable provider (" + "; ".join(_unavailable(p.name) for p in providers) + ")")
    return ok


def _single(name: str, budget: Budget, tier: str | None, bbox: BBox | None) -> Any:
    return _usable(_gated([name], budget, tier, bbox))[0]


def _area_cache_path() -> Path:
    return config.state_dir() / "areas.json"


def _read_area_cache() -> dict[str, Any]:
    try:
        data = json.loads(_area_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve_area(name: str, budget: Budget) -> tuple[BBox, str, bool]:
    """Name -> (box, label, from_cache). Costs one Text Search Pro call on a cache miss."""
    cache = _read_area_cache()
    entry = cache.get(name.casefold().strip())
    if entry and time.time() - entry.get("ts", 0) < AREA_CACHE_DAYS * 86400:
        return BBox.parse(entry["bbox"]), entry["label"], True
    google = _single("google", budget, "pro", None)
    try:
        bbox, label = google.resolve_area(name)
    except BudgetExceeded as e:
        raise CliError(f"cannot resolve area {name!r}: {e}") from None
    cache[name.casefold().strip()] = {"label": label, "bbox": bbox.key(), "ts": time.time()}
    _area_cache_path().parent.mkdir(parents=True, exist_ok=True)
    _area_cache_path().write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return bbox, label, False


def resolve_bbox(loc: Location, budget: Budget) -> tuple[BBox | None, str | None]:
    """The search box named by the location flags, or None when no flag was given."""
    try:
        if loc.bbox:
            return BBox.parse(loc.bbox), None
        if loc.near:
            parts = [float(p) for p in loc.near.split(",")]
            if len(parts) != 2:
                raise ValueError("--near must be 'LAT,LNG'")
            return BBox.around(parts[0], parts[1], loc.radius or DEFAULT_RADIUS_M), None
    except ValueError as e:
        raise CliError(f"bad location: {e}") from None
    if loc.area:
        bbox, label, _ = resolve_area(loc.area, budget)
        return bbox, label
    return None, None


NO_LOCATION = "a location is required: give --area NAME, --bbox S,W,N,E or --near LAT,LNG [--radius M]"


# --- ranking output ---------------------------------------------------------------------------


def _deeper(depth: str) -> str:
    return {"quick": "deep", "standard": "deep", "deep": "exhaustive"}.get(depth, "exhaustive")


def _next_steps(run_id: str, objective: str, coverage: dict[str, Any] | None, depth: str | None) -> list[str]:
    other = "gems" if objective != "gems" else "best"
    steps = [
        f"maps rank {run_id} --objective {other} --top 20",
        f"maps followups {run_id}",
        f"maps reviews fetch {run_id} --top 10",
    ]
    if coverage and not coverage.get("complete"):
        steps.append(f"maps search --resume {run_id} --depth {_deeper(depth or 'standard')}")
        if coverage.get("stopped"):
            steps.append(f"maps search --resume {run_id} --cost quality --max-usd 1  # authorises spend")
    return steps


def _results_doc(
    run_id: str,
    places: dict[str, Place],
    flt: filters.Filters,
    rk: Ranking,
    *,
    coverage: dict[str, Any] | None,
    budget: dict[str, Any] | None,
    depth: str | None,
) -> dict[str, Any]:
    kept = filters.apply(places.values(), flt)
    ranked = scoring.rank(kept, rk.objective, top=rk.top)
    doc: dict[str, Any] = {"run_id": run_id}
    if coverage is not None:
        doc["coverage"] = coverage
    if budget is not None:
        doc["budget"] = budget
    doc.update(
        filters=flt.active(),
        objective=rk.objective,
        matched=len(kept),
        results=[render.place_row(s, index=i) for i, s in enumerate(ranked, 1)],
        next=_next_steps(run_id, rk.objective, coverage, depth),
    )
    return doc


def _markdown_report(doc: dict[str, Any]) -> str:
    lines = [f"# Maps results for run `{doc['run_id']}`", ""]
    cov = doc.get("coverage")
    if cov:
        state = "complete" if cov.get("complete") else "INCOMPLETE"
        lines.append(
            f"- Coverage: {cov['places']} places from {cov['searches_run']} searches "
            f"({cov['searches_resumed']} replayed), {state}"
        )
        if cov.get("stopped"):
            lines.append(f"- Stopped early: {cov['stopped']}")
        if cov.get("gaps"):
            lines.append(f"- Gaps: {cov['gaps']}")
    if doc.get("policy_stop"):
        lines.append(f"- POLICY STOP: {doc['policy_stop']['provider']}: {doc['policy_stop']['detail']}")
        lines.append(f"- {doc['policy_stop']['action']}")
    bud = doc.get("budget")
    if bud:
        lines.append(f"- Spend: ${bud['run_out_of_pocket_usd']:.4f} out of pocket ({bud['mode']} mode)")
    lines.append(
        f"- Ranking: {doc['objective']}; {doc['matched']} matched filters {doc['filters'] or '(none)'}; "
        f"showing {len(doc['results'])}"
    )
    nxt = " | ".join(f"`{n}`" for n in doc["next"][:3])
    lines += ["", render.to_markdown(doc["results"]), "", f"Next: {nxt}"]
    return "\n".join(lines)


def _output(doc: dict[str, Any], fmt: str) -> None:
    click.echo(_markdown_report(doc)) if fmt == "md" else emit(doc)


# --- commands ---------------------------------------------------------------------------------


@click.group(cls=MapsGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="maps")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Google Maps research toolkit, free-first, built for AI agents.

    Typical flow: `maps doctor` (what is configured), `maps search -q bakery --area Riyadh`
    (discover places; prints a run id), then `maps rank RUN` (re-rank for free),
    `maps reviews fetch RUN` and `maps reviews search RUN "slow service"` (read reviews).
    `maps discover "quiet bakery known for sourdough"` finds places by meaning; `maps reviews search
    RUN "phrase" --mode hybrid` searches review text by meaning as well as keywords.

    Every command prints ONE JSON document on stdout (or markdown with --format md); progress and
    warnings go to stderr. User errors exit 2 with one line on stderr. Stopping early because of the
    budget exits 0 and the JSON says why. Each command's --help states what it costs.

    Cost modes: --cost free (default) never spends money and stops at 90% of each free monthly
    allowance; --cost quality --max-usd N authorises paid calls up to N dollars for that command, never
    above the owner's recorded per-search cap. Nothing chargeable runs until the owner has recorded a
    spending authorization (`maps policy approve SUBJECT --kind spend`); a refusal that starts
    "Spending:" is final. Every run also has hard ceilings (requests, minutes, repeated requests), and
    `maps policy kill --reason TEXT` stops every real request at once. `maps budget` shows all of it.

    State lives in $MAPS_STATE_DIR (default ~/.maps-research); runs older than $MAPS_RETENTION_DAYS
    (default and maximum 30) are deleted at the start of every command, per the Maps Platform terms.

    Account protection (`maps policy status`): every real provider is blocked until the owner records
    an approval (Google's services: their terms checked; Apify, DataForSEO: data rights), nothing a
    real provider returned reaches an AI that is not an approved AI host, reviewer matching is off until
    approved, and a provider's enforcement signal stops everything (exit 3). A refusal starting
    "Account protection:" is final: report it.
    """
    if ctx.invoked_subcommand == "policy":
        return  # `maps policy status` must keep working to explain a refusal
    policy.state_dir_check(config.state_dir())
    if config.retention_clamped():
        warn(
            f"MAPS_RETENTION_DAYS is capped at {config.MAX_RETENTION_DAYS:g} days (Maps content expires); using {config.MAX_RETENTION_DAYS:g}"
        )
    if ctx.invoked_subcommand:
        # Expiry runs for every command that touches the state directory, not only `search`.
        purged = store.purge(config.state_dir(), older_than_days=config.retention_days())
        if purged:
            warn(
                f"purged {len(purged)} run(s) past the {config.retention_days():g}-day retention: "
                + ", ".join(purged)
            )


def _provider_info(p: Any) -> dict[str, Any]:
    info: dict[str, Any] = {"available": p.available(), "env": ENV_VARS[p.name]}
    info.update(
        policy.provider_status(p.name)
    )  # class, approval kind, approved, spend subject, spend_recorded
    return info


@main.command()
def doctor() -> None:
    """Show what is configured. Free: makes no API call and prints no secret values."""
    with provider_http.unchecked_proxy():  # doctor makes no request; the proxy check is reported below
        probe = _construct_providers(list(ENV_VARS), Budget(Ledger(":memory:")), None, None)
    emit(
        {
            "version": __version__,
            "state_dir": str(config.state_dir()),
            "retention_days": config.retention_days(),
            "account_protection": policy.status(),
            "providers": {p.name: _provider_info(p) for p in probe},
            "features": {
                "semantic": {"installed": _importable("fastembed"), "install": "uv sync --extra semantic"},
                "mcp": {"installed": _importable("mcp"), "install": "uv sync --extra mcp"},
            },
            "prices_checked": PRICES_CHECKED,
            "runs": len(store.list_runs(config.state_dir())),
        }
    )


@main.group("budget", invoke_without_command=True)
@click.pass_context
def budget_cmd(ctx: click.Context) -> None:
    """This month's usage against free allowances, the owner's spending caps and the per-run ceilings.

    Free: reads the local counters only (`usage.json` in the policy folder). Shows each spend record
    with its caps, this month's out-of-pocket total per billing subject against its period cap, the
    request and time ceilings, and the kill-switch state. `maps budget reconcile` brings the counters up
    to what the provider's own console says.

    The counters only see requests made through this toolkit; usage elsewhere on the same Google
    project is invisible, so keep a quota cap in the provider's console too.
    """
    if ctx.invoked_subcommand is not None:
        return
    gateway = Budget(Ledger(config.ledger_path()))
    rows = gateway.month_report()
    shown = [r for r in rows if r["used"] > 0 or (math.isfinite(r["free_units"]) and r["free_units"] > 0)]
    emit(
        {
            "month": current_month(),
            "note": (
                "Mode note: --cost free refuses anything that would cost money and stops at 90% of each "
                "free allowance; --cost quality --max-usd N authorizes paid calls within the owner's caps. "
                f"Prices checked {PRICES_CHECKED}; they go stale, and a spend record's prices must be "
                "confirmed within 90 days."
            ),
            "rows": shown,
            **gateway.gateway_report(),
        }
    )


@budget_cmd.command("reconcile")
@click.option("--sku", required=True, metavar="SKU", help="The SKU as `maps budget` lists it.")
@click.option(
    "--units",
    required=True,
    type=float,
    metavar="N",
    help="The units the provider's console shows for this month.",
)
@click.option(
    "--owner-statement", required=True, metavar="TEXT", help="The owner's own words (the source of N)."
)
def budget_reconcile(sku: str, units: float, owner_statement: str) -> None:
    """Raise this month's recorded units for SKU to max(current, N). Free.

    The owner copies N from the provider's console, because usage from outside this toolkit is
    invisible here. The counter never goes down. An entry (date, sku, units, statement) is appended to
    the reconciliation log in usage.json. This records the owner's figure, not yours: run it only with
    the owner's own statement from the current conversation.
    """
    try:
        emit(Budget(Ledger(config.ledger_path())).reconcile(sku, units, owner_statement=owner_statement))
    except ValueError as e:
        raise CliError(str(e)) from None


@main.command()
@click.argument("name")
def area(name: str) -> None:
    """Resolve a place name to a search box. Cost: 1 Google Text Search Pro call, cached 30 days."""
    policy.require_ai_host()  # even a cached area is Google output that goes to the AI
    budget = make_budget(Cost("free", None), ["google"])
    bbox, label, cached = resolve_area(name, budget)
    emit({"name": name, "label": label, "bbox": bbox.key(), "cached": cached})


@main.command()
@click.option(
    "-q",
    "--query",
    "queries",
    multiple=True,
    metavar="QUERY",
    help="Keyword to search; repeatable. 'text:lang' sets the language, e.g. 'مخبز:ar'. "
    "Searching each language of a keyword finds places the other misses.",
)
@location_options
@click.option(
    "--depth",
    type=click.Choice(list(retrieval.DEPTHS)),
    default=None,
    help="How hard to look (default standard). A search returns at most 60 results, so crowded "
    "areas are split into smaller cells: quick = 1 search; standard/deep/exhaustive split "
    "up to 2/4/7 times.",
)
@cost_options
@click.option(
    "--provider",
    "providers",
    multiple=True,
    type=click.Choice(SEARCH_PROVIDERS),
    help="Data source; repeatable (default google). sim is an offline simulator: free, no key.",
)
@click.option(
    "--tier",
    type=click.Choice(["ids", "pro", "enterprise"]),
    default=None,
    help=f"Google only (default enterprise). Selects the billed field set: ids "
    f"({SKUS['google.text_search.ids'].note}, free), pro "
    f"({SKUS['google.text_search.pro'].free_units:g} free calls/month), enterprise "
    f"({SKUS['google.text_search.enterprise'].free_units:g} free calls/month, adds rating and "
    "review count).",
)
@click.option(
    "--max-tasks",
    type=click.IntRange(min=1),
    default=None,
    help="Override the depth's ceiling on searches in this run.",
)
@click.option(
    "--service-area-businesses",
    "service_area",
    is_flag=True,
    help="Google only: also return pure service-area businesses (plumbers, cleaners) that have no "
    "address on Maps. Such places may have no coordinates.",
)
@click.option(
    "--resume",
    "resume_id",
    metavar="RUN_ID",
    help="Continue a stored run: finished searches are replayed for free, -q/--depth/--provider "
    "default to the run's own.",
)
@filter_options
@ranking_options(20)
@format_option
def search(
    queries: tuple[str, ...],
    loc: Location,
    depth: str | None,
    cost: Cost,
    providers: tuple[str, ...],
    tier: str | None,
    max_tasks: int | None,
    service_area: bool,
    resume_id: str | None,
    flt: filters.Filters,
    rk: Ranking,
    fmt: str,
) -> None:
    """Discover places for keywords in an area, then filter and rank them.

    Cost: each search request is billed to the chosen provider: Google Text Search per page of 20
    (see --tier for free allowances); the simulator is free. With --cost free the run stops cleanly
    before exceeding a free allowance and reports what it could not cover (exit 0). Results are
    stored as a run; `maps rank RUN` re-ranks them without further cost.

    Output: run_id, coverage (what was and was not searched), budget, matched, results, next steps.
    """
    run = _open_run(resume_id) if resume_id else None
    spec = run.spec if run else {}
    # On resume, new -q/--provider are added to the run's own (original order first).
    qkeys = list(dict.fromkeys([*(spec.get("queries") or []), *queries]))
    if not qkeys:
        raise CliError("give at least one -q QUERY (or --resume a run that has queries)")
    names = list(dict.fromkeys([*(spec.get("providers") or []), *providers])) or ["google"]
    foreign = [n for n in names if n not in SEARCH_PROVIDERS]
    if foreign:
        raise CliError(
            f"search cannot use or resume provider(s) {', '.join(foreign)}: this run was made by another "
            f"command. Add to it with `maps discover TEXT --into {resume_id}` instead."
            if resume_id
            else f"search cannot use provider(s) {', '.join(foreign)}"
        )
    depth_name = depth or spec.get("depth") or "standard"
    if run and tier and spec.get("tier") and tier != spec["tier"]:
        raise CliError(
            f"--tier {tier} differs from this run's tier {spec['tier']}; "
            "searches already checkpointed at one tier would be replayed at another. Start a new run."
        )
    tier = tier or spec.get("tier") or "enterprise"
    service_area = service_area or bool(spec.get("service_area_businesses"))

    budget = make_budget(cost, [*names, *_area_users(loc)], depth_name)
    bbox, label = resolve_bbox(loc, budget)
    if bbox is None and run and spec.get("bbox"):
        bbox = BBox.parse(spec["bbox"])
    if bbox is None:
        if set(names) != {"sim"}:
            raise CliError(NO_LOCATION)
        bbox = config.SIM_AREA
    usable = _usable(_gated(names, budget, tier, bbox))

    wanted = {
        "queries": qkeys,
        "bbox": bbox.key(),
        "depth": depth_name,
        "providers": names,
        "cost": cost.mode,
        "tier": tier,
        "area": label or spec.get("area"),
        "service_area_businesses": service_area,
    }
    if run is None:
        run = RunStore.create(config.state_dir(), wanted)
    depth_cfg = retrieval.DEPTHS[depth_name]
    if max_tasks:
        depth_cfg = dataclasses.replace(depth_cfg, max_tasks=max_tasks)

    result = retrieval.discover(
        usable,
        [Query.parse(q) for q in qkeys],
        bbox,
        depth_cfg,
        store=run,
        progress=warn,
        # In the checkpoint key too: a search with the flag is a different search.
        search_kwargs={"include_service_area_businesses": True} if service_area else None,
    )
    coverage = retrieval.coverage_summary(result)
    _touch_spec(run, **wanted, coverage=coverage)
    doc = _results_doc(
        run.run_id, run.places(), flt, rk, coverage=coverage, budget=budget.summary(), depth=depth_name
    )
    _exit_on_stop(doc, result.policy_stop, lambda d: _output(d, fmt))
    _output(doc, fmt)


@main.command()
@click.argument("run_id")
@filter_options
@ranking_options(20)
@format_option
def rank(run_id: str, flt: filters.Filters, rk: Ranking, fmt: str) -> None:
    """Re-filter and re-rank a stored run. Cost: free, makes no API call."""
    run = _open_run(run_id)
    spec = run.spec
    doc = _results_doc(
        run_id, run.places(), flt, rk, coverage=spec.get("coverage"), budget=None, depth=spec.get("depth")
    )
    _output(doc, fmt)


@main.command()
@click.argument("run_id")
def followups(run_id: str) -> None:
    """Suggest what to search next from a run's results. Cost: free, makes no API call.

    Lists frequent place types and name words that the run's queries did not use. You decide which
    deserve a new `maps search`.
    """
    run = _open_run(run_id)
    queries = [Query.parse(q) for q in run.spec.get("queries", [])]
    found = retrieval.suggest_followups(list(run.places().values()), queries)
    emit(
        {
            "run_id": run_id,
            "queries": run.spec.get("queries", []),
            "types": [{"type": t, "count": n} for t, n in found["types"]],
            "name_tokens": [{"token": t, "count": n} for t, n in found["name_tokens"]],
        }
    )


@main.command()
@location_options
@click.option(
    "--type",
    "types",
    multiple=True,
    required=True,
    metavar="TYPE",
    help="Google place type, e.g. bakery. Repeatable.",
)
@click.option("--min-rating", type=float, help="Count only places rated >= this.")
@click.option("--max-rating", type=float, help="Count only places rated <= this.")
def census(loc: Location, types: tuple[str, ...], min_rating: float | None, max_rating: float | None) -> None:
    """Count places of a type with exact rating bounds. Cost: 1 Google Places Aggregate call.

    Text Search cannot filter by maximum rating; this can, and counts without paging.
    """
    budget = make_budget(Cost("free", None), ["google_aggregate", *_area_users(loc)])
    bbox, _ = resolve_bbox(loc, budget)
    if bbox is None:
        raise CliError(NO_LOCATION)
    agg = _single("google_aggregate", budget, None, bbox)
    used = {"types": list(types), "min_rating": min_rating, "max_rating": max_rating}
    try:
        count: int | None = agg.count(bbox, **used)
        stopped = None
    except BudgetExceeded as e:
        count, stopped = None, str(e)
    emit(
        {
            "count": count,
            "filter": used,
            "bbox": bbox.key(),
            "stopped": stopped,
            "note": "Counts may be cached for 30 days under the Maps terms.",
        }
    )


@main.command("enumerate")
@location_options
@click.option(
    "--type",
    "types",
    multiple=True,
    required=True,
    metavar="TYPE",
    help="Google place type, e.g. bakery. Repeatable.",
)
@click.option("--min-rating", type=float, help="List only places rated >= this.")
@click.option("--max-rating", type=float, help="List only places rated <= this.")
@cost_options
@click.option("--into", "into_id", metavar="RUN_ID", help="Add the IDs to this run instead of a new one.")
def enumerate_cmd(
    loc: Location,
    types: tuple[str, ...],
    min_rating: float | None,
    max_rating: float | None,
    cost: Cost,
    into_id: str | None,
) -> None:
    """List place IDs of a type by exact rating bounds. Cost: Google Places Aggregate calls.

    Walks the area in cells (a count and a list call per cell, 100 IDs per list) and stores the IDs
    as ID-only places in a run. Follow with `maps details RUN --missing` to fill in names (billed).
    """
    budget = make_budget(cost, ["google_aggregate", *_area_users(loc)])
    bbox, _ = resolve_bbox(loc, budget)
    if bbox is None:
        raise CliError(NO_LOCATION)
    agg = _single("google_aggregate", budget, None, bbox)
    found = agg.enumerate(bbox, types=list(types), min_rating=min_rating, max_rating=max_rating)
    run = (
        _open_run(into_id)
        if into_id
        else RunStore.create(
            config.state_dir(),
            {
                "queries": [],
                "types": list(types),
                "bbox": bbox.key(),
                "depth": None,
                "providers": ["google_aggregate"],
                "cost": cost.mode,
                "tier": None,
            },
        )
    )
    tag = "type:" + ",".join(types)
    lo, hi = ("" if v is None else v for v in (min_rating, max_rating))
    key = f"google_aggregate|{','.join(types)}|{bbox.key()}|{lo}|{hi}"
    run.record(
        key,
        [
            Place(pid, hits=[Hit(provider="google_aggregate", query=tag, cell=bbox.key())])
            for pid in found.place_ids
        ],
        saturated=False,
    )
    emit(
        {
            "run_id": run.run_id,
            "total_count": found.total_count,
            "ids_found": len(found.place_ids),
            "requests": found.requests,
            "unresolved": found.unresolved,
            "stopped": found.stopped,
            "budget": budget.summary(),
            "next": [f"maps details {run.run_id} --missing"],
        }
    )


SOURCE_NOTE = (
    "Results come from Google's Maps Experience agent tools; keep the Google Maps links with anything "
    "you pass on, and show each place's attribution with grounded output."
)


GEMINI_NOTE = (
    "Results come from Gemini grounded with Google Maps; keep the Google Maps links with anything you "
    "pass on, and show the attribution Google provides with grounded output."
)
DISCOVER_SOURCES = {"agent_tools": "google_agent_tools", "gemini": "gemini_maps"}
DISCOVER_KEYS = {"agent_tools": "GOOGLE_MAPS_API_KEY", "gemini": "GEMINI_API_KEY"}


@main.command()
@click.argument("text")
@location_options
@click.option("--into", "into_id", metavar="RUN_ID", help="Add the places to this run instead of a new one.")
@click.option("--lang", "language", metavar="CODE", help="Language of the results, e.g. en or ar.")
@click.option(
    "--via",
    type=click.Choice(["agent_tools", "gemini"]),
    default="agent_tools",
    show_default=True,
    help="agent_tools: Google's Maps Experience agent tools (GOOGLE_MAPS_API_KEY). gemini: Gemini "
    "grounded with Google Maps (GEMINI_API_KEY).",
)
@cost_options
def discover(
    text: str, loc: Location, into_id: str | None, language: str | None, via: str, cost: Cost
) -> None:
    """Find places by meaning, e.g. "quiet bakery known for sourdough". Cost: free while Experimental.

    Asks Google's Maps Experience agent tools, which understand descriptions rather than keywords,
    and returns a short curated list (not a full census). The location is optional; the text may name
    the place. Google offers the tools free while they stay Experimental (unconfirmed; the budget
    guard still records each call). Places arrive as IDs, coordinates and Maps links only (the service gives no names): follow with `maps details RUN
    --missing`, then `maps rank RUN`. Unverified against the live service. Stops cleanly (exit 0) on
    budget or quota.
    """
    source = DISCOVER_SOURCES[via]
    policy.require_provider(source)
    policy.require_ai_host()  # grounded Maps output goes to the AI host: its data settings must be approved
    budget = make_budget(cost, [source, *_area_users(loc)])
    budget.preflight(source)
    bbox, label = resolve_bbox(loc, budget)
    tools = build_discoverer(via, budget)
    if not tools.available():
        via_flag = " --via gemini" if via == "gemini" else ""
        raise CliError(f"discover{via_flag} needs {DISCOVER_KEYS[via]} (see docs/setup.md)")
    run = _open_run(into_id) if into_id else None
    stopped = None
    found = None
    stop = None
    try:
        found = tools.search_places(text, bbox=bbox, language=language)
    except PolicyStop as e:
        stop = {"provider": e.provider, "detail": e.detail}
    except (BudgetExceeded, QuotaExceeded) as e:
        stopped = str(e)
    places = list(found.places) if found else []
    notes = dict(found.notes) if found else {}
    attributions = dict(found.attributions) if found else {}
    if run is None and found is not None:
        run = RunStore.create(
            config.state_dir(),
            {
                "queries": [],
                "discover": [text],
                "bbox": bbox.key() if bbox else None,
                "depth": None,
                "providers": [source],
                "cost": cost.mode,
                "tier": None,
                "area": label,
            },
        )
    elif run is not None:
        spec_discover = list(run.spec.get("discover") or [])
        if text not in spec_discover:
            _touch_spec(run, discover=[*spec_discover, text])
    if run is not None and found is not None:
        run.record(f"{source}|{text}|{bbox.key() if bbox else '-'}", places, saturated=False)
    run_id = run.run_id if run else None
    doc = {
        "run_id": run_id,
        "query": text,
        "found": len(places),
        "places": [
            {
                "place_id": p.place_id,
                "name": p.name,
                "maps_url": p.maps_url,
                "attribution": attributions.get(p.place_id),
                "note": notes.get(p.place_id),
            }
            for p in places
        ],
        "model": getattr(tools, "model", None),
        "summary": found.summary if found else "",
        "source_note": GEMINI_NOTE if via == "gemini" else SOURCE_NOTE,
        "stopped": stopped,
        "budget": budget.summary(),
        "next": [f"maps details {run_id} --missing", f"maps rank {run_id}"] if run_id else [],
    }
    _exit_on_stop(doc, stop)
    emit(doc)


@main.command()
@click.argument("run_id")
@click.option("--missing", is_flag=True, help="Fetch every place that has no name yet (ID-only places).")
@click.option(
    "--tier",
    type=click.Choice(["ids", "pro", "enterprise"]),
    default="enterprise",
    show_default=True,
    help="Billed field set; enterprise adds rating and review count.",
)
@cost_options
@filter_options
@ranking_options(None)
def details(run_id: str, missing: bool, tier: str, cost: Cost, flt: filters.Filters, rk: Ranking) -> None:
    """Fetch Google place details. Cost: 1 Place Details call per place (see --tier).

    Fetches places without a name (--missing) or the top N of the ranked, filtered places (--top N,
    default 20). Results are stored in the run. Stops cleanly when the budget or the provider quota refuses.
    """
    if missing and rk.top is not None:
        raise CliError("use either --missing or --top N, not both")
    run = _open_run(run_id)
    places = run.places()
    if missing:
        targets = [p for p in places.values() if not p.name]
    else:
        ranked = scoring.rank(filters.apply(places.values(), flt), rk.objective, top=rk.top or 20)
        targets = [s.place for s in ranked]
    todo = [p for p in targets if run.done(f"google_details|{p.place_id}|{tier}") is None]

    budget = make_budget(cost, ["google"])
    google = _single("google", budget, tier, None)
    fetched, stopped, errors, stop = 0, None, [], None
    for p in todo:
        try:
            got = google.details(p.place_id, tier=tier)
        except PolicyStop as e:
            stop = {"provider": e.provider, "detail": e.detail}
            break
        except (BudgetExceeded, QuotaExceeded) as e:
            stopped = str(e)
            break
        except ProviderError as e:
            errors.append(f"{p.place_id}: {e}")
            if len(errors) >= retrieval.MAX_CONSECUTIVE_ERRORS:
                stopped = f"stopped after {len(errors)} errors"
                break
            continue
        run.record(f"google_details|{p.place_id}|{tier}", [got], saturated=False)
        fetched += 1
        warn(f"[{fetched}/{len(todo)}] details {p.place_id}")
    doc = {
        "run_id": run_id,
        "tier": tier,
        "fetched": fetched,
        "remaining": len(todo) - fetched,
        "stopped": stopped,
        "errors": errors,
        "budget": budget.summary(),
        "next": [f"maps rank {run_id}"],
    }
    _exit_on_stop(doc, stop)
    emit(doc)


@main.group("reviews")
def reviews_group() -> None:
    """Fetch, search and audit reviews of the places in a run."""


@reviews_group.command("fetch")
@click.argument("run_id")
@filter_options
@ranking_options(10)
@cost_options
@click.option(
    "--provider",
    "provider",
    type=click.Choice(SEARCH_PROVIDERS),
    default="google",
    show_default=True,
    help="Where to fetch reviews from (sim is offline and free).",
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=5,
    show_default=True,
    help="Reviews per place (Google returns at most 5).",
)
def reviews_fetch(
    run_id: str, flt: filters.Filters, rk: Ranking, cost: Cost, provider: str, limit: int
) -> None:
    """Fetch reviews for the top-ranked places that have none yet.

    Cost: Google Place Details Enterprise + Atmosphere per place (1,000 free calls/month); Apify and
    DataForSEO bill per review; sim is free. Picks the first --top ranked, filtered places without
    stored reviews, so repeating the command moves down the ranking. Stops cleanly on budget or provider quota.
    """
    run = _open_run(run_id)
    spec = run.spec
    places = run.places()
    ranked = scoring.rank(filters.apply(places.values(), flt), rk.objective)
    todo = [s.place for s in ranked if not s.place.reviews][: rk.top]

    budget = make_budget(cost, [provider])
    bbox = BBox.parse(spec["bbox"]) if spec.get("bbox") else None
    prov = _single(provider, budget, spec.get("tier"), bbox)
    done: list[dict[str, Any]] = []
    stopped, errors, stop = None, [], None
    for p in todo:
        try:
            got = prov.reviews(p.place_id, limit=limit)
        except PolicyStop as e:
            stop = {"provider": e.provider, "detail": e.detail}
            break
        except (BudgetExceeded, QuotaExceeded) as e:
            stopped = str(e)
            break
        except (ProviderError, KeyError) as e:
            errors.append(f"{p.place_id}: {e}")
            continue
        if got:
            run.add_reviews(p.place_id, got)
        done.append({"place_id": p.place_id, "name": p.name, "reviews": len(got)})
        warn(f"[{len(done)}/{len(todo)}] {p.name or p.place_id}: {len(got)} reviews")
    doc = {
        "run_id": run_id,
        "provider": provider,
        "places": done,
        "stopped": stopped,
        "errors": errors,
        "budget": budget.summary(),
        "next": [f'maps reviews search {run_id} "your phrase"'],
    }
    _exit_on_stop(doc, stop)
    emit(doc)


def _places_with_reviews(run: RunStore) -> dict[str, Place]:
    return {pid: p for pid, p in run.places().items() if p.reviews}


@reviews_group.command("search")
@click.argument("run_id")
@click.argument("query")
@click.option("--top", type=click.IntRange(min=1), default=20, show_default=True)
@click.option("--min-rating", type=float, help="Only reviews whose own star rating is >= this.")
@click.option("--max-rating", type=float, help="Only reviews whose own star rating is <= this.")
@click.option("--by-place", is_flag=True, help="Group matches by place instead of listing reviews.")
@click.option(
    "--mode",
    type=click.Choice(["keyword", "semantic", "hybrid"]),
    default="keyword",
    show_default=True,
    help="keyword: BM25 word matching. semantic: matches by meaning, so paraphrases and other "
    "languages are found. hybrid: both rankings fused.",
)
@click.option(
    "--model",
    default=DEFAULT_MODEL,
    show_default=True,
    help="Embedding model for semantic and hybrid.",
)
def reviews_search(
    run_id: str,
    query: str,
    top: int,
    min_rating: float | None,
    max_rating: float | None,
    by_place: bool,
    mode: str,
    model: str,
) -> None:
    """Search review text. Cost: free, makes no API call.

    Modes: keyword = BM25 word matching; semantic = meaning (finds paraphrases and other languages);
    hybrid = both rankings fused. The embedding model runs locally, so review text never leaves this
    machine; the first semantic or hybrid use downloads the model (needs `uv sync --extra semantic`);
    vectors are cached in the run directory.

    Only searches reviews already fetched into this run (`maps reviews fetch`); Google returns at
    most 5 per place, so absence of a match is weak evidence.
    """
    run = _open_run(run_id)
    with_reviews = _places_with_reviews(run)
    index = ReviewIndex(with_reviews.values())
    if mode == "keyword":
        if by_place:
            results = [
                {
                    "place_id": m.place_id,
                    "name": m.place_name,
                    "score": round(m.score, 3),
                    "n_matches": m.n_matches,
                    "best": render.review_block(m.best, m.place_name),
                }
                for m in index.search_places(query, top=top)
            ]
        else:
            results = [
                render.review_block(m, m.place_name)
                for m in index.search(query, top=top, min_rating=min_rating, max_rating=max_rating)
            ]
    else:
        try:
            sem = SemanticIndex(
                with_reviews.values(), build_embedder(model), cache_path=run.dir / "embeddings.json"
            )
            depth = max(len(sem), 1) if by_place else top
            bounds = {"min_rating": min_rating, "max_rating": max_rating}
            if mode == "semantic":
                matches = sem.search(query, top=depth, **bounds)
            else:
                matches = hybrid_search(index, sem, query, top=depth, **bounds)
        except SemanticUnavailable as e:
            raise CliError(str(e)) from None
        if by_place:
            results = [
                {
                    "place_id": m.place_id,
                    "name": m.place_name,
                    "score": round(m.score, 3),
                    "n_matches": m.n_matches,
                    "best": render.review_block(m.best, m.place_name),
                }
                for m in aggregate_by_place(matches, top=top)
            ]
        else:
            results = [render.review_block(m, m.place_name) for m in matches]
    doc: dict[str, Any] = {
        "run_id": run_id,
        "query": query,
        "mode": mode,
        "reviews_indexed": len(index),
        "places_with_reviews": len(with_reviews),
        "coverage_note": (
            "Only reviews fetched into this run were searched "
            f"({len(index)} reviews across {len(with_reviews)} of {len(run.places())} places); "
            "run `maps reviews fetch` for more places."
        ),
        "results": results,
    }
    if mode != "keyword":
        doc["model"] = model
    emit(doc)


@reviews_group.command("integrity")
@click.argument("run_id")
@click.option("--place", "place_ids", multiple=True, metavar="PLACE_ID", help="Place to check; repeatable.")
@click.option(
    "--top",
    type=click.IntRange(min=1),
    default=None,
    help="Without --place: keep only the first N places, most flags first (default: all).",
)
def reviews_integrity(run_id: str, place_ids: tuple[str, ...], top: int | None) -> None:
    """Signals that a place's reviews may be manipulated. Cost: free, makes no API call.

    Without --place, covers every place in the run with at least one fetched review, most flags
    first then by name; --top N keeps the first N. Evidence to weigh (rating shape, bursts,
    repeated text, one-time reviewers), never a verdict.
    """
    run = _open_run(run_id)
    with_reviews = _places_with_reviews(run)
    if place_ids:
        missing = [p for p in place_ids if p not in with_reviews]
        if missing:
            raise CliError(f"no fetched reviews in this run for: {', '.join(missing)}")
        chosen = [with_reviews[p] for p in place_ids]
        reports = [{"name": p.name, **integrity_report(p)} for p in chosen]
    else:
        reports = [{"name": p.name, **integrity_report(p)} for p in with_reviews.values()]
        reports.sort(key=lambda r: (-len(r["flags"]), r["name"] or ""))
        if top is not None:
            reports = reports[:top]
    emit({"run_id": run_id, "places_checked": len(reports), "places": reports})


PRIVACY_NOTE = (
    "This looks up individual reviewers' public review histories through a scraper; see docs/permissions.md."
)


def _with_contributor_ids(places: list[Place]) -> list[Place]:
    """Copies whose reviewer ids are normalised, so a URL and a bare id of one person match."""
    out = []
    for place in places:
        copy = Place.from_dict(place.to_dict())
        for r in copy.reviews:
            if r.author_id:
                try:
                    r.author_id = parse_contributor(r.author_id)[0]
                except ValueError:
                    pass
        out.append(copy)
    return out


def _expansion_candidates(places: list[Place], liked: set[str], min_rating: float) -> list[str]:
    """Contributor ids who rated a liked place >= min_rating, most liked places first, then by id."""
    endorsed: dict[str, set[str]] = {}
    for place in places:
        if place.place_id not in liked:
            continue
        for r in place.reviews:
            if not r.author_id or r.rating is None or r.rating < min_rating:
                continue
            try:
                cid, _ = parse_contributor(r.author_id)
            except ValueError:
                continue
            endorsed.setdefault(cid, set()).add(place.place_id)
    return sorted(endorsed, key=lambda c: (-len(endorsed[c]), c))


@reviews_group.command("contributors")
@click.argument("run_id")
@click.option(
    "--liked",
    "liked",
    multiple=True,
    metavar="PLACE_ID",
    help="Place you like; also list places endorsed by people who endorsed it. Repeatable.",
)
@click.option(
    "--min-shared",
    type=click.IntRange(min=2),
    default=2,
    show_default=True,
    help="Minimum places a reviewer must have reviewed to be listed.",
)
@click.option(
    "--expand",
    "expand",
    type=click.IntRange(min=1),
    metavar="N",
    help="Look up the public review histories of N contributors who rated a --liked place highly, "
    "and add the places they reviewed to the run. Needs --liked and APIFY_TOKEN.",
)
@click.option(
    "--min-rating",
    type=float,
    default=4.0,
    show_default=True,
    help="With --expand/--liked: a rating at or above this counts as endorsing a place.",
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=10,
    show_default=True,
    help="With --expand: reviews to fetch per contributor.",
)
@cost_options
def reviews_contributors(
    run_id: str,
    liked: tuple[str, ...],
    min_shared: int,
    expand: int | None,
    min_rating: float,
    limit: int,
    cost: Cost,
) -> None:
    """Reviewers who reviewed several places in the run. Cost: free, unless --expand is used.

    --expand N looks up N contributors through Apify, billed per result (--limit per contributor);
    --cost free allows it only inside the monthly free credit. Contributors already looked up in this
    run are replayed for free. Unverified against the live actor. It reads individual reviewers'
    public histories: see docs/permissions.md.
    """
    run = _open_run(run_id)
    if expand and not liked:
        raise CliError("--expand needs at least one --liked PLACE_ID")
    if expand or _has_provider_review(run):
        # Matching individual reviewers across places is off until the owner records the right to do it.
        policy.require_feature("reviewer_matching")
    extra: dict[str, Any] = {}
    stop = None
    if expand:
        policy.require_provider("apify_contributor")
        budget = make_budget(cost, ["apify_contributor"])
        budget.preflight("apify_contributor")
        prov = build_contributor_provider(budget)
        if not prov.available():
            raise CliError("--expand needs APIFY_TOKEN (see docs/setup.md)")
        before = set(run.places())
        candidates = _expansion_candidates(list(_places_with_reviews(run).values()), set(liked), min_rating)[
            :expand
        ]
        expanded: list[dict[str, Any]] = []
        errors: list[str] = []
        stopped = None
        for cid in candidates:
            key = f"apify_contributor|{cid}|{limit}"
            if run.done(key) is not None:
                continue
            try:
                got = prov.contributor_reviews(cid, limit=limit)
            except PolicyStop as e:
                stop = {"provider": e.provider, "detail": e.detail}
                break
            except (BudgetExceeded, QuotaExceeded) as e:
                stopped = str(e)
                break
            except ProviderError as e:
                errors.append(f"{cid}: {e}")
                continue
            run.record(key, got, saturated=False)
            expanded.append({"author_id": cid, "places_added": len(got)})
            warn(f"[{len(expanded)}/{len(candidates)}] contributor {cid}: {len(got)} places")
        extra = {
            "expanded": expanded,
            "new_places": len(set(run.places()) - before),
            "stopped": stopped,
            "errors": errors,
            "budget": budget.summary(),
            "privacy_note": PRIVACY_NOTE,
        }
    places = list(_places_with_reviews(run).values())
    if expand:
        places = _with_contributor_ids(places)
    doc: dict[str, Any] = {"run_id": run_id, "contributors": co_review_graph(places, min_shared=min_shared)}
    if liked:
        doc["overlap"] = contributor_overlap(places, set(liked), min_rating=min_rating)
    doc.update(extra)
    _exit_on_stop(doc, stop)
    emit(doc)


@main.group("policy")
def policy_group() -> None:
    """Account protection: what is permitted, what the owner approved, which providers are stopped."""


@policy_group.command("status")
def policy_status() -> None:
    """Show the provider registry (class, approval, spend record), approvals, open stops, the kill switch,
    the AI host and gated features. Free."""
    emit(policy.status())


@policy_group.command("approve")
@click.argument("subject")
@click.option("--kind", type=click.Choice(list(policy.KINDS)), required=True)
@click.option(
    "--owner-statement", required=True, metavar="TEXT", help="The owner's own words approving this."
)
@click.option("--data-rights", metavar="TEXT", help="provider: what its terms let you do with the data.")
@click.option(
    "--retrieval-method", metavar="TEXT", help="provider: how the data is retrieved (no Google login)."
)
@click.option("--downstream-use", metavar="TEXT", help="provider: what the data is used for afterwards.")
@click.option("--data-handling", metavar="TEXT", help="ai_host: what its data settings say about training.")
@click.option("--purpose", metavar="TEXT", help="egress_proxy: why this network proxy is needed.")
@click.option(
    "--terms-checked",
    metavar="TEXT",
    help="service_use: what the owner checked about using this Google service for this purpose.",
)
@click.option("--rights", metavar="TEXT", help="feature: what gives the owner the right to use this feature.")
@click.option(
    "--billing",
    type=click.Choice(["none", "enabled"]),
    help="spend: none = the credential cannot be charged (e.g. Google's Maps Demo Key); enabled = it can.",
)
@click.option(
    "--per-search-cap-usd", type=float, metavar="N", help="spend, billing enabled: most one run may spend."
)
@click.option(
    "--period-cap-usd", type=float, metavar="N", help="spend, billing enabled: most per billing month."
)
@click.option(
    "--prices-checked-on",
    metavar="YYYY-MM-DD",
    help="spend, billing enabled: when the owner last read the provider's prices (blocks after 90 days).",
)
@click.option("--prices-source", metavar="TEXT", help="spend, billing enabled: where the prices were read.")
def policy_approve(
    subject: str,
    kind: str,
    owner_statement: str,
    data_rights: str | None,
    retrieval_method: str | None,
    downstream_use: str | None,
    data_handling: str | None,
    purpose: str | None,
    terms_checked: str | None,
    rights: str | None,
    billing: str | None,
    per_search_cap_usd: float | None,
    period_cap_usd: float | None,
    prices_checked_on: str | None,
    prices_source: str | None,
) -> None:
    """Record the OWNER's approval. Free.

    This records the owner's decision, not yours: run it only with the owner's own statement from the
    current conversation, quoted verbatim in --owner-statement. Every kind and what it needs:

    \b
    provider      SUBJECT apify, dataforseo, apify_contributor...; --data-rights, --retrieval-method,
                  --downstream-use
    ai_host       SUBJECT the AI host's name; --data-handling
    egress_proxy  SUBJECT the proxy's host name; --purpose
    service_use   SUBJECT google, google_aggregate, google_agent_tools or gemini_maps; --terms-checked
    feature       SUBJECT reviewer_matching; --rights
    spend         SUBJECT google, gemini, apify or dataforseo; --billing none|enabled and, when
                  enabled: --per-search-cap-usd, --period-cap-usd, --prices-checked-on,
                  --prices-source
    """
    if kind == "provider":
        details: dict[str, Any] = {
            "data_rights": data_rights,
            "retrieval_method": retrieval_method,
            "downstream_use": downstream_use,
        }
    elif kind == "egress_proxy":
        details = {"purpose": purpose}
    elif kind == "service_use":
        details = {"terms_checked": terms_checked}
    elif kind == "feature":
        details = {"rights": rights}
    elif kind == "spend":
        details = {
            "billing": billing,
            "per_search_cap_usd": per_search_cap_usd,
            "period_cap_usd": period_cap_usd,
            "prices_checked_on": prices_checked_on,
            "prices_source": prices_source,
        }
        if billing != "enabled":
            details = {"billing": billing}
    else:
        details = {"data_handling": data_handling}
    emit(policy.approve(subject, kind, owner_statement=owner_statement, details=details))


@policy_group.command("kill")
@click.option(
    "--reason", required=True, metavar="TEXT", help="Why. Shown in every refusal while the switch is on."
)
def policy_kill(reason: str) -> None:
    """Stop every request to a real provider, at once. Free.

    Anyone may run this and it needs no owner statement. While it is on, the spending gateway and the
    provider check refuse every request except the simulator's. Only the owner lifts it (`maps policy
    resume`).
    """
    emit(policy.kill(reason))


@policy_group.command("resume")
@click.option("--owner-statement", required=True, metavar="TEXT", help="The owner's own words lifting it.")
def policy_resume(owner_statement: str) -> None:
    """Lift the kill switch. Free.

    This records the owner's decision, not yours: run it only with the owner's own statement from the
    current conversation, quoted verbatim in --owner-statement.
    """
    emit({"resumed": policy.resume(owner_statement=owner_statement)})


@policy_group.command("revoke")
@click.argument("subject")
def policy_revoke(subject: str) -> None:
    """Withdraw every approval of SUBJECT. Free."""
    emit({"subject": subject, "revoked": policy.revoke(subject)})


@policy_group.command("clear-stop")
@click.argument("provider")
@click.option("--owner-statement", required=True, metavar="TEXT", help="The owner's own words releasing it.")
def policy_clear_stop(provider: str, owner_statement: str) -> None:
    """Release a provider that was stopped by an enforcement signal. Free.

    This records the owner's decision, not yours: run it only with the owner's own statement from the
    current conversation, quoted verbatim in --owner-statement, after the owner has read what the
    provider said (`maps policy status`).
    """
    emit({"provider": provider, "cleared": policy.clear_stop(provider, owner_statement=owner_statement)})


@main.command()
def runs() -> None:
    """List stored runs, newest first. Cost: free."""
    root = config.state_dir()
    rows = []
    for r in store.list_runs(root):
        spec = r["spec"]
        rows.append(
            {
                "run_id": r["run_id"],
                "age_days": r["age_days"],
                "queries": spec.get("queries", []),
                "depth": spec.get("depth"),
                "places": len(RunStore.open(root, r["run_id"]).places()),
            }
        )
    emit({"runs": rows})


@main.command()
@click.option(
    "--older-than",
    type=float,
    metavar="DAYS",
    help="Delete runs older than this many days (default: the retention window).",
)
@click.option("--all", "everything", is_flag=True, help="Delete every run and the area cache.")
def purge(older_than: float | None, everything: bool) -> None:
    """Delete stored run data. Cost: free. Search already purges runs past the retention window."""
    if everything and older_than is not None:
        raise CliError("use either --all or --older-than DAYS, not both")
    root = config.state_dir()
    days = -1.0 if everything else (older_than if older_than is not None else config.retention_days())
    deleted = store.purge(root, older_than_days=days)
    cleared = False
    if everything and _area_cache_path().exists():
        _area_cache_path().unlink()
        cleared = True
    emit({"deleted": deleted, "area_cache_cleared": cleared})


@main.group("eval")
def eval_group() -> None:
    """Benchmarks for the improve-or-revert loop."""


@eval_group.command("sim")
def eval_sim() -> None:
    """Recall by depth on simulated cities with known ground truth. Cost: free, offline.

    Rerun after changing retrieval or scoring; keep the change only if recall holds or improves.
    """
    emit({"rows": evals.sim_benchmark()})


@eval_group.command("check")
@click.argument("run_id")
@click.option(
    "--expect",
    "expect_file",
    required=True,
    type=click.Path(dir_okay=False),
    help='JSON file {"name": str, "expected_place_ids": [..]}.',
)
def eval_check(run_id: str, expect_file: str) -> None:
    """Score a run against places a good search must find. Cost: free."""
    try:
        spec = json.loads(Path(expect_file).read_text(encoding="utf-8"))
        name, expected = str(spec["name"]), list(spec["expected_place_ids"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise CliError(f"cannot read expectations from {expect_file}: {e}") from None
    run = _open_run(run_id)
    emit({"name": name, "run_id": run_id, **evals.expected_recall(run.places(), expected)})


@eval_group.command("compare")
@click.argument("run_id")
@click.option(
    "--baseline",
    "baseline_file",
    required=True,
    type=click.Path(dir_okay=False),
    help='JSON file {"name": str, "source": str, "places": [{"name", "maps_url", "place_id"}]}; each place '
    "needs at least one of the three.",
)
@filter_options
@ranking_options(20)
def eval_compare(run_id: str, baseline_file: str, flt: filters.Filters, rk: Ranking) -> None:
    """Compare a run's top results with a hand-made list. Cost: free, makes no API call.

    Matches by place ID, else by the ID in a Maps URL, else by name (casefolded, punctuation
    ignored; equal, or one containing the other when the shorter has 5+ characters). Reports the
    overlap, the toolkit's top results the list lacks, and the list's places the top results lack,
    each with why: not retrieved, retrieved but filtered out, or retrieved at a lower rank.
    """
    try:
        baseline = evals.load_baseline(baseline_file)
    except ValueError as e:
        raise CliError(f"baseline: {e}") from None
    run = _open_run(run_id)
    places = run.places()
    scored = scoring.rank(filters.apply(places.values(), flt), rk.objective)
    top = rk.top or 20
    res = evals.compare_to_baseline(baseline, [s.place for s in scored], places.values(), top=top)
    rows = {
        r["place_id"]: render.place_row(scored[r["rank"] - 1], index=r["rank"]) for r in res["only_toolkit"]
    }
    emit(
        {
            "run_id": run_id,
            "baseline": {"name": baseline.name, "source": baseline.source, "count": len(baseline.places)},
            "top": top,
            "objective": rk.objective,
            "filters": flt.active(),
            "overlap": res["overlap"],
            "only_toolkit": [rows[r["place_id"]] for r in res["only_toolkit"]],
            "only_baseline": res["only_baseline"],
            "counts": res["counts"],
        }
    )


if __name__ == "__main__":
    main()
