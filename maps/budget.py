"""The spending gateway. Free-first means the tool refuses a request *before* it would cost money.

Every billable request is charged against a SKU through `Budget.charge`, the one gateway every provider
calls immediately before each request (and before each retry). One locked operation reads this month's
totals, evaluates every limit, records the reservation and releases the lock. A reservation is never
released automatically: a request that then fails stays counted.

What the gateway enforces for every real provider (the simulator `sim` is exempt from all of it):
- the kill switch (`policy/KILL.json`) is off;
- the owner recorded a spending authorization for the SKU's billing subject (`maps policy approve
  SUBJECT --kind spend`). `billing: none` means the credential cannot be charged: no price knowledge is
  needed and nothing counts as out-of-pocket. `billing: enabled` needs prices confirmed within 90 days and
  a SKU whose price is known (unknown price means block);
- free mode: nothing out-of-pocket, and a stop at 90% of each free allowance; quality mode: the run's
  `max_usd` (which can never exceed the owner's per-search cap) and the owner's cap for the billing month;
- per-run ceilings: total requests (hard maximum 5,000), wall-clock time, and no more than three
  identical requests;
- limits across runs, kept in the store: a daily request cap per billing subject (default 2,000, only
  the owner's spend record can change it, at most 20,000) and at most 2 runs active at once.

Every HTTP request is charged first: the free setup requests of the agent-tools client and the task
polls of DataForSEO have zero-price SKUs of their own, so they count, obey the kill switch and the daily
cap. Units and money are exact (`decimal.Decimal`) and bounded: a counter outside 0..1,000,000,000 makes
the store unusable, and the month's out-of-pocket total is DERIVED from the recorded units (list price
beyond the free allowance, less the provider's credit), never read from a stored figure. `billing: none`
changes only what the owner has to confirm (no prices); it does not switch a limit off: free-mode rules
apply, `--cost quality` is refused, and a SKU whose price is unknown is capped at 1,000 units a month.

The counters live in one JSON file, `usage.json`, in the policy folder (the state directory is lost on
ephemeral machines). It holds counters only, never response content. Every read-modify-write holds an
exclusive `flock` on a sibling lock file (waited for at most 10 seconds) and ends with an atomic replace.
A file that cannot be parsed or used refuses every chargeable request (fail closed). A store that is
missing, empty or without `initialized_on` is not "zero usage": under `billing: enabled` it refuses too,
until the owner restores the committed file or runs `maps budget restore` with the console's figures.

Limits of this guard, stated plainly:
- It only sees requests made through this toolkit with this policy folder. Usage of the same Google
  project or Apify account from anywhere else is invisible. Reconcile with `maps budget reconcile`.
- Prices and free allowances live in pricing.py and can go stale.
The hard stop is a quota cap set in the provider's own console (docs/setup.md).
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
import socket
import tempfile
import threading
import time
import uuid
import weakref
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .pricing import PROVIDER_FREE_USD, SKUS, Sku

# Google bills by Pacific-time calendar month.
BILLING_TZ = ZoneInfo("America/Los_Angeles")

DEFAULT_MAX_REQUESTS = 400
HARD_MAX_REQUESTS = 5_000  # no option can exceed this
DEFAULT_MAX_ELAPSED_S = 900.0
MAX_REPEATS = 3  # the same request more than this many times in one run is refused
MAX_UNITS = 10**9  # per SKU per month; a counter or an argument outside 0..MAX_UNITS is refused
MAX_MONEY = 10**9  # dollars: a cap or a stored money figure outside 0..MAX_MONEY is refused
UNKNOWN_PRICE_MONTHLY_UNITS = 1_000  # under billing none, a SKU whose price is unknown: units per month
DEFAULT_DAILY_REQUEST_CAP = 2_000  # requests per billing subject per day; only the owner's record changes it
MAX_DAILY_REQUEST_CAP = 20_000
MAX_ACTIVE_RUNS = 2  # runs that may hold a lease on one store at once
LEASE_GRACE_S = 60.0  # a lease older than the run's max_elapsed_s plus this is ignored
LOCK_TIMEOUT_S = 10.0  # how long a request waits for the store lock before it is refused
DAYS_KEPT = 8
# depth -> (max_requests, max_elapsed_s)
DEPTH_CEILINGS: dict[str, tuple[int, float]] = {
    "quick": (30, 900.0),
    "standard": (200, 900.0),
    "deep": (1_000, 900.0),
    "exhaustive": (5_000, 3_600.0),
}
# The SKUs each provider can charge, so a command can be refused before its first request.
PROVIDER_SKUS: dict[str, tuple[str, ...]] = {
    "google": tuple(n for n in SKUS if n.startswith(("google.text_search.", "google.details."))),
    "google_aggregate": ("google.aggregate",),
    "google_agent_tools": ("google.agent_tools.search", "google.agent_tools.setup"),
    "gemini_maps": ("google.gemini_maps_grounding",),
    "apify": ("apify.place", "apify.review"),
    "apify_contributor": ("apify.contributor",),
    "dataforseo": ("dataforseo.maps_search", "dataforseo.reviews", "dataforseo.poll"),
}


class BudgetExceeded(RuntimeError):
    """Raised before a request that the current cost policy does not allow."""


class GatewayRefusal(BudgetExceeded):
    """A refusal that happens before the first request of a command (the CLI exits 2 with `Spending:`)."""


class LedgerUnavailable(GatewayRefusal):
    """The usage store cannot be parsed or used: every chargeable request is refused."""


def current_month() -> str:
    return datetime.now(BILLING_TZ).strftime("%Y-%m")


def current_day() -> str:
    """The calendar day in the billing time zone, which the daily request cap counts in."""
    return datetime.now(BILLING_TZ).strftime("%Y-%m-%d")


def _today() -> date:
    return date.today()


# --- exact numbers -------------------------------------------------------------------------------


def to_decimal(value: Any, *, name: str = "units", maximum: int = MAX_UNITS) -> Decimal:
    """An exact, finite number in 0..maximum, or ValueError. Floats are read by their shortest text."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    d = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    if not d.is_finite() or d < 0 or d > maximum:
        raise ValueError(f"{name} must be a finite number from 0 to {maximum:,}, got {value!r}")
    return d


def _stored(v: Any, maximum: int) -> bool:
    """True for a number read from the store that is exact, finite and inside 0..maximum."""
    if isinstance(v, bool) or not isinstance(v, (int, Decimal)):
        return False
    return Decimal(v).is_finite() and 0 <= v <= maximum


def json_number(v: Any) -> int | float:
    """A Decimal (or int) as a JSON number: an int when whole."""
    d = Decimal(v)
    if not d.is_finite():
        return float(d)
    return int(d) if d == d.to_integral_value() else float(d)


def _plain(obj: Any) -> Any:
    """The same document with every Decimal as a JSON number."""
    if isinstance(obj, Decimal):
        return json_number(obj)
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_plain(v) for v in obj]
    return obj


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a number")


def _d(v: float | int | Decimal) -> Decimal:
    return v if isinstance(v, Decimal) else (Decimal(repr(v)) if isinstance(v, float) else Decimal(v))


ZERO = Decimal(0)


def _free_units(sku: Sku) -> Decimal:
    return _d(sku.free_units)


def _price(sku: Sku) -> Decimal:
    return _d(sku.usd_per_unit)


def _list_usd(sku: Sku, units: Decimal | int) -> Decimal:
    """List-price value of `units` beyond the SKU's free allowance."""
    return max(ZERO, _d(units) - _free_units(sku)) * _price(sku)


# --- the store's documents -------------------------------------------------------------------------


def _empty() -> dict[str, Any]:
    return {"months": {}, "reconciliations": [], "days": {}, "leases": []}


def _units(data: dict[str, Any], month: str, sku: str) -> Decimal:
    return _d(data["months"].get(month, {}).get(sku, {}).get("units", 0))


def _provider_usd(data: dict[str, Any], month: str, provider: str) -> Decimal:
    """List-price value of this month's units for one provider, derived from the units."""
    return sum(
        (
            _list_usd(SKUS[sku], row["units"])
            for sku, row in data["months"].get(month, {}).items()
            if sku in SKUS and SKUS[sku].provider == provider
        ),
        ZERO,
    )


def _subject_usd(data: dict[str, Any], month: str, subject: str) -> Decimal:
    from . import policy

    return sum(
        (
            _list_usd(SKUS[sku], row["units"])
            for sku, row in data["months"].get(month, {}).items()
            if sku in SKUS and policy.spend_subject(sku) == subject
        ),
        ZERO,
    )


def subject_out_of_pocket(data: dict[str, Any], month: str, subject: str) -> Decimal:
    """This month's out-of-pocket total for a billing subject: list-price value beyond the free
    allowances, less the provider's free monthly credit. Derived from the recorded units alone."""
    from . import policy

    providers = {s.provider for n, s in SKUS.items() if policy.spend_subject(n) == subject}
    credit = sum((_d(PROVIDER_FREE_USD.get(p, 0.0)) for p in providers), ZERO)
    return max(ZERO, _subject_usd(data, month, subject) - credit)


def _add(data: dict[str, Any], month: str, sku: str, units: Decimal | int) -> None:
    row = data["months"].setdefault(month, {}).setdefault(sku, {"units": 0, "usd": ZERO})
    row["units"] = row["units"] + units
    # informational only: nothing reads this figure back; it is the list-price value of the units
    row["usd"] = _list_usd(SKUS[sku], row["units"]) if sku in SKUS else ZERO


def _release_lease(ledger: Ledger, run_id: str) -> None:
    """Remove a run's lease from its store. Never raises: it also runs at interpreter exit."""
    try:
        with ledger.transaction() as data:
            data["leases"] = [lease for lease in data["leases"] if lease.get("id") != run_id]
    except Exception:  # noqa: BLE001 - nothing to release if the store is gone or unusable
        pass


class Ledger:
    """Monthly usage counters, day counts and run leases in one JSON file (or in memory for unit tests)."""

    def __init__(
        self,
        path: Path | str,
        *,
        lock_timeout: float = LOCK_TIMEOUT_S,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = str(path)
        self.memory = self.path == ":memory:"
        self._mem = _empty()
        self._mem["initialized_on"] = _today().isoformat()  # nothing to lose: it is not a file
        self._tlock = threading.RLock()
        self._active: dict[str, Any] | None = None
        self._lock_timeout = lock_timeout
        self._sleep = sleep
        self._clock = clock

    # -- the file -----------------------------------------------------------------------------

    def _fail(self, why: str) -> LedgerUnavailable:
        return LedgerUnavailable(
            f"the usage store {self.path} {why}; every chargeable request is refused until the owner "
            "repairs the file (restore the committed usage.json) or removes it and runs `maps budget "
            "restore` with the provider console's figures"
        )

    def not_initialised(self) -> LedgerUnavailable:
        return LedgerUnavailable(
            f"the usage store {self.path} is missing, empty or not initialised (no 'initialized_on'): a "
            "missing store is not zero usage, so every chargeable request is refused under billing "
            "enabled. Recover: restore the committed usage.json, or the owner reconciles from the "
            "provider's console: maps budget restore --owner-statement \"<owner's words>\" --sku "
            "SKU=UNITS (one figure for each SKU with a finite free allowance)"
        )

    def _validate(self, doc: Any) -> dict[str, Any]:
        if not isinstance(doc, dict):
            raise self._fail("is not a JSON object")
        months = doc.setdefault("months", {})
        if not isinstance(months, dict):
            raise self._fail("has no valid 'months' object")
        for skus in months.values():
            if not isinstance(skus, dict):
                raise self._fail("has a month that is not an object")
            for row in skus.values():
                if not isinstance(row, dict) or not _stored(row.get("units"), MAX_UNITS):
                    raise self._fail(f"has a unit counter that is not a number from 0 to {MAX_UNITS:,}")
                if not _stored(row.get("usd"), MAX_MONEY):
                    raise self._fail(f"has a money figure that is not a number from 0 to {MAX_MONEY:,}")
        if not isinstance(doc.setdefault("reconciliations", []), list):
            raise self._fail("has no valid 'reconciliations' list")
        init = doc.get("initialized_on")
        if init is not None:
            try:
                date.fromisoformat(init)
            except (TypeError, ValueError):
                raise self._fail("has an 'initialized_on' that is not a date") from None
        days = doc.setdefault("days", {})
        if not isinstance(days, dict):
            raise self._fail("has no valid 'days' object")
        for day, counts in days.items():
            try:
                date.fromisoformat(day)
            except (TypeError, ValueError):
                raise self._fail("has a day that is not a date") from None
            if not isinstance(counts, dict) or not all(_stored(n, MAX_UNITS) for n in counts.values()):
                raise self._fail("has a daily request count that is not a whole number")
        leases = doc.setdefault("leases", [])
        if not isinstance(leases, list):
            raise self._fail("has no valid 'leases' list")
        for lease in leases:
            ok = (
                isinstance(lease, dict)
                and isinstance(lease.get("id"), str)
                and isinstance(lease.get("host"), str)
                and _stored(lease.get("pid"), 2**32)
                and _stored(lease.get("started"), 10**12)
                and _stored(lease.get("max_elapsed_s"), 10**9)
            )
            if not ok:
                raise self._fail("has a run lease that is not valid")
        return doc

    def _load(self) -> dict[str, Any]:
        if self.memory:
            return self._mem
        try:
            text = Path(self.path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return _empty()
        except OSError as e:
            raise self._fail(f"cannot be read ({type(e).__name__})") from None
        try:
            doc = json.loads(text, parse_float=Decimal, parse_int=int, parse_constant=_no_constant)
        except ValueError:
            raise self._fail("is not valid JSON") from None
        return self._validate(doc)

    def _save(self, data: dict[str, Any]) -> None:
        path = Path(self.path)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".usage.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(_plain(data), f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _lock(self, fd: int) -> None:
        """Take the exclusive file lock, waiting at most `lock_timeout` seconds."""
        start = self._clock()
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if self._clock() - start >= self._lock_timeout:
                    raise LedgerUnavailable(
                        f"ledger unavailable: the usage store {self.path} stayed locked by another process "
                        f"for {self._lock_timeout:g} s; the request is refused"
                    ) from None
                self._sleep(0.05)
            except OSError as e:
                raise self._fail(f"cannot be locked ({type(e).__name__})") from None

    @contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        """Exclusive read-modify-write: the data is saved (atomically) when the block ends normally and
        changed it. A block that raises changes nothing, in the file or in memory."""
        with self._tlock:
            if self.memory:
                working = copy.deepcopy(self._mem)
                self._active = working
                try:
                    yield working
                    self._mem = working  # committed only when the block ends normally
                finally:
                    self._active = None
                return
            path = Path(self.path)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(f"{self.path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
            except OSError as e:
                raise self._fail(f"cannot be locked ({type(e).__name__})") from None
            try:
                self._lock(fd)
                data = self._load()
                before = json.dumps(_plain(data), sort_keys=True)
                self._active = data
                try:
                    yield data
                finally:
                    self._active = None
                if json.dumps(_plain(data), sort_keys=True) != before:
                    try:
                        self._save(data)
                    except OSError as e:
                        raise self._fail(f"cannot be written ({type(e).__name__})") from None
            finally:
                os.close(fd)  # closing the descriptor releases the lock

    def read(self) -> dict[str, Any]:
        """The current counters (a copy). Raises LedgerUnavailable when the store is unusable."""
        with self._tlock:
            data = self._active if self._active is not None else self._load()
            return copy.deepcopy(data)

    def _view(self) -> dict[str, Any]:
        with self._tlock:
            return self._active if self._active is not None else self._load()

    def initialize(self) -> bool:
        """Create the store, or add `initialized_on` to a usable one that lacks it. True when it changed."""
        with self.transaction() as data:
            if data.get("initialized_on"):
                return False
            data["initialized_on"] = _today().isoformat()
            return True

    # -- queries ------------------------------------------------------------------------------

    def units(self, sku: str, month: str | None = None) -> Decimal:
        return _units(self._view(), month or current_month(), sku)

    def provider_usd(self, provider: str, month: str | None = None) -> Decimal:
        """List-price value of this month's billable units for one provider (derived from the units)."""
        return _provider_usd(self._view(), month or current_month(), provider)

    def rows(self, month: str | None = None) -> list[tuple[str, Decimal, Decimal]]:
        months = self._view()["months"].get(month or current_month(), {})
        return [
            (sku, _d(row["units"]), _list_usd(SKUS[sku], row["units"]) if sku in SKUS else ZERO)
            for sku, row in sorted(months.items())
        ]

    def add(self, sku: str, units: Any, usd: Any = 0, month: str | None = None) -> None:
        """Add units to a counter (the owner's reconciliation and tests). `usd` is ignored: money is
        derived from the units."""
        n = to_decimal(units)
        with self.transaction() as data:
            if _units(data, month or current_month(), sku) + n > MAX_UNITS:
                raise ValueError(f"units would pass the hard maximum of {MAX_UNITS:,}")
            _add(data, month or current_month(), sku, n)


@dataclass
class Quote:
    sku: Sku
    units: Decimal
    billable_usd: Decimal  # list-price value of the units beyond the SKU's free allowance
    out_of_pocket_usd: Decimal  # what is left after the provider's free monthly credit


def _release_on_exit(ledger: Ledger, run_id: str) -> None:
    _release_lease(ledger, run_id)


class Budget:
    """Cost policy and request ceilings for one run.

    mode="free":    refuse anything with out-of-pocket cost, and stop at
                    `reserve` (default 90%) of each free allowance to leave headroom
                    for usage this ledger cannot see.
    mode="quality": allow out-of-pocket cost up to max_usd for this run, which can never exceed the
                    owner's per-search cap, and within the owner's cap for the billing month.
                    Refused for a billing subject recorded as `none` (not chargeable).
    `subjects` names the billing subjects the run will use (the per-search cap is checked against
    those); None checks every recorded one.

    A run holds a lease in the store from its first chargeable request until `close()` (or until it is
    garbage collected or the process exits); at most MAX_ACTIVE_RUNS leases are active at once.
    """

    def __init__(
        self,
        ledger: Ledger,
        *,
        mode: str = "free",
        max_usd: float = 0.0,
        reserve: float = 0.9,
        subjects: list[str] | tuple[str, ...] | None = None,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        max_elapsed_s: float = DEFAULT_MAX_ELAPSED_S,
        max_repeats: int = MAX_REPEATS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if mode not in ("free", "quality"):
            raise ValueError("mode must be 'free' or 'quality'")
        if isinstance(max_usd, bool) or not (math.isfinite(max_usd) and 0 <= max_usd <= MAX_MONEY):
            raise ValueError(f"max_usd must be a finite number from 0 to {MAX_MONEY:,}")
        if not (0 < reserve <= 1):
            raise ValueError("reserve must be in (0, 1]")
        if mode == "free" and max_usd:
            raise ValueError("max_usd only applies to quality mode")
        if (
            isinstance(max_requests, bool)
            or not isinstance(max_requests, int)
            or not (1 <= max_requests <= HARD_MAX_REQUESTS)
        ):
            raise ValueError(
                f"max_requests must be a whole number from 1 to {HARD_MAX_REQUESTS:,} (hard maximum)"
            )
        if not (math.isfinite(max_elapsed_s) and max_elapsed_s > 0):
            raise ValueError("max_elapsed_s must be a finite number > 0")
        if max_repeats < 1:
            raise ValueError("max_repeats must be at least 1")
        self.ledger = ledger
        self.mode = mode
        self.max_usd = max_usd
        self._max_usd = _d(max_usd)
        self.reserve = reserve
        self._reserve = _d(reserve)
        self.max_requests = max_requests
        self.max_elapsed_s = float(max_elapsed_s)
        self.max_repeats = max_repeats
        self.run_usd: Decimal = ZERO  # out-of-pocket spend in this run
        self.run_units: dict[str, Decimal] = {}
        self.requests = 0  # chargeable requests reserved in this run
        self._clock = clock
        self._wall_clock = wall_clock
        self._t0 = clock()
        self._seen: dict[str, int] = {}
        self._lock = threading.Lock()
        self._run_id = uuid.uuid4().hex
        self._finalizer: weakref.finalize | None = None
        if mode == "quality":
            self._check_per_search_cap(subjects)

    def remaining_elapsed_s(self) -> float:
        """Seconds this run may still use before its wall-clock ceiling."""
        return max(0.0, self.max_elapsed_s - (self._clock() - self._t0))

    def close(self) -> None:
        """End the run: give its lease back. Safe to call twice; a later charge takes a new lease."""
        if self._finalizer is not None:
            self._finalizer()
            self._finalizer = None

    # -- the owner's records ------------------------------------------------------------------

    def _check_per_search_cap(self, subjects: list[str] | tuple[str, ...] | None) -> None:
        from . import policy

        for subject in subjects if subjects is not None else policy.SPEND_SUBJECTS:
            rec = policy.spend_record(subject)
            if rec is None:
                continue
            if rec["details"].get("billing") != "enabled":
                if subjects is not None:
                    raise GatewayRefusal(self._not_chargeable(subject))
                continue
            cap = to_decimal(
                rec["details"]["per_search_cap_usd"], name="per_search_cap_usd", maximum=MAX_MONEY
            )
            if self._max_usd > cap:
                raise GatewayRefusal(
                    f"--max-usd {self.max_usd:g} is above the owner's per-search cap of ${cap:.2f} for "
                    f"{subject}. Only the owner can raise it, by recording a new spending authorization "
                    f"(maps policy approve {subject} --kind spend ...)"
                )

    @staticmethod
    def _not_chargeable(subject: str) -> str:
        return (
            f"--cost quality is refused for {subject}: the owner recorded it as not chargeable (billing "
            "none), so there is no spending to authorize. Use --cost free, or the owner records billing "
            f"enabled with caps: maps policy approve {subject} --kind spend ..."
        )

    def _gate(self, subject: str, sku_names: tuple[str, ...] | list[str]) -> dict[str, Any]:
        """Kill switch, spending authorization, price freshness and price knowledge for one billing subject.

        Returns the owner's spend record details. Raises BudgetExceeded.
        """
        from . import policy

        kill = policy.kill_status()
        if kill["on"]:
            raise BudgetExceeded(f"kill switch is on: {kill['reason']}")
        rec = policy.spend_record(subject)
        if rec is None:
            raise BudgetExceeded(policy.spend_refusal(subject))
        details = rec["details"]
        if details["billing"] == "none":
            if self.mode == "quality":
                raise BudgetExceeded(self._not_chargeable(subject))
        else:
            try:
                age = (_today() - date.fromisoformat(details["prices_checked_on"].strip())).days
            except ValueError:
                age = -1
            if not 0 <= age <= policy.PRICE_MAX_AGE_DAYS:
                raise BudgetExceeded(
                    f"prices not confirmed within {policy.PRICE_MAX_AGE_DAYS} days: unknown price means "
                    f"block (the {subject} record says {details['prices_checked_on']}). The owner re-checks "
                    f"the provider's prices and records them again: maps policy approve {subject} --kind spend ..."
                )
            for name in sku_names:
                if not SKUS[name].price_known:
                    raise BudgetExceeded(
                        f"{name}: its price is not confirmed, and billing is enabled for {subject}: "
                        "unknown price means block. Under billing none it is allowed, with a ceiling of "
                        f"{UNKNOWN_PRICE_MONTHLY_UNITS:,} units a month."
                    )
        return details

    def preflight(self, provider: str) -> None:
        """Refuse, before a command's first request, what `charge` would refuse for every request."""
        from . import policy

        subject = policy.spend_subject(provider)
        if subject is None:
            return
        try:
            data = self.ledger.read()  # an unusable store refuses everything
            details = self._gate(subject, PROVIDER_SKUS.get(provider, ()))
            if details["billing"] == "enabled" and not data.get("initialized_on"):
                raise self.ledger.not_initialised()
        except BudgetExceeded as e:
            raise GatewayRefusal(str(e)) from None

    # -- fingerprints --------------------------------------------------------------------------

    @staticmethod
    def fingerprint(method: str, url: str, body: Any = None) -> str:
        """A short hash of one request (method + URL + body), for the repeated-request ceiling.

        Pass only the URL and body, never a header, so no key or token can enter it.
        """
        blob = json.dumps([method.upper(), url, body], sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    # -- quoting and charging ------------------------------------------------------------------

    @staticmethod
    def _check_units(units: Any) -> Decimal:
        return to_decimal(units, name="units")

    @staticmethod
    def _quote(data: dict[str, Any], month: str, sku_name: str, units: Decimal) -> Quote:
        """Priced from the SKU's table and free allowance alone: whatever the store already holds
        (reconciled, recorded under billing none, or before any record) consumes the allowance."""
        sku = SKUS[sku_name]
        if sku.provider == "sim":
            return Quote(sku, units, ZERO, ZERO)
        used = _units(data, month, sku_name)
        free = _free_units(sku)
        paid_before = max(ZERO, used - free)
        paid_after = max(ZERO, used + units - free)
        billable = (paid_after - paid_before) * _price(sku)
        credit_left = max(
            ZERO, _d(PROVIDER_FREE_USD.get(sku.provider, 0.0)) - _provider_usd(data, month, sku.provider)
        )
        return Quote(sku, units, billable, max(ZERO, billable - credit_left))

    def quote(self, sku_name: str, units: Any = 1) -> Quote:
        """What a charge would cost. Read-only; makes no check of the owner's records."""
        n = self._check_units(units)
        return self._quote(self.ledger.read(), current_month(), sku_name, n)

    def _ceilings(self, fingerprint: str | None) -> None:
        if self.requests >= self.max_requests:
            raise BudgetExceeded(
                f"max_requests ceiling ({self.max_requests}) reached: no further requests in this run"
            )
        elapsed = self._clock() - self._t0
        if elapsed > self.max_elapsed_s:
            raise BudgetExceeded(
                f"max_elapsed_s ceiling ({self.max_elapsed_s:g} s) reached after {elapsed:.0f} s: "
                "no further requests in this run"
            )
        if fingerprint is not None and self._seen.get(fingerprint, 0) >= self.max_repeats:
            raise BudgetExceeded(
                f"repeated identical request refused: the same request ({fingerprint}) was already made "
                f"{self.max_repeats} times in this run"
            )

    def _evaluate(
        self,
        data: dict[str, Any],
        month: str,
        sku_name: str,
        units: Decimal,
        details: dict[str, Any],
        subject: str,
    ) -> Quote:
        none = details["billing"] == "none"
        sku = SKUS[sku_name]
        used = _units(data, month, sku_name)
        if used + units > MAX_UNITS:
            raise BudgetExceeded(
                f"{sku_name}: this month's counter would pass the hard maximum of {MAX_UNITS:,} units"
            )
        q = self._quote(data, month, sku_name, units)
        if none and not sku.price_known:
            # Nothing can be priced, so nothing can be held to a dollar limit: a unit ceiling instead.
            if used + units > UNKNOWN_PRICE_MONTHLY_UNITS:
                raise BudgetExceeded(
                    f"{sku_name}: its price is unknown, so under billing none it is limited to "
                    f"{UNKNOWN_PRICE_MONTHLY_UNITS:,} units a month ({used:g} used)"
                )
            return Quote(sku, units, ZERO, ZERO)
        if none or self.mode == "free":
            if q.out_of_pocket_usd > 0:
                raise BudgetExceeded(
                    f"{sku_name}: free allowance exhausted this month; "
                    f"this request would cost ${q.out_of_pocket_usd:.4f}. "
                    "Re-run with --cost quality --max-usd N to authorize spending."
                )
            if sku.free_units and used + units > _free_units(sku) * self._reserve:
                raise BudgetExceeded(
                    f"{sku_name}: reached {self.reserve:.0%} of the {sku.free_units:g} free units "
                    "this month (headroom kept for usage this ledger cannot see)."
                )
            credit = _d(PROVIDER_FREE_USD.get(sku.provider, 0.0))
            if (
                credit
                and q.billable_usd
                and (_provider_usd(data, month, sku.provider) + q.billable_usd > credit * self._reserve)
            ):
                raise BudgetExceeded(
                    f"{sku_name}: reached {self.reserve:.0%} of the ${credit:g} free monthly "
                    f"{sku.provider} credit."
                )
        else:
            if self.run_usd + q.out_of_pocket_usd > self._max_usd:
                raise BudgetExceeded(
                    f"{sku_name}: this request (${q.out_of_pocket_usd:.4f}) would exceed the "
                    f"authorized ${self.max_usd:.2f} for this run (spent ${self.run_usd:.4f})."
                )
            cap = to_decimal(details["period_cap_usd"], name="period_cap_usd", maximum=MAX_MONEY)
            spent = subject_out_of_pocket(data, month, subject)
            if spent + q.out_of_pocket_usd > cap:
                raise BudgetExceeded(
                    f"{sku_name}: the owner's period cap for {subject} would be exceeded: ${spent:.4f} already "
                    f"spent this billing month plus ${q.out_of_pocket_usd:.4f} for this request is above "
                    f"${cap:.2f}."
                )
        return q

    def _prepare(
        self, items: Sequence[tuple[str, Any]], fingerprint: str | None
    ) -> tuple[str | None, dict[str, Any], list[tuple[str, Decimal]]]:
        """The checks that need no counters. Returns (billing subject or None for sim, spend details, items)."""
        from . import policy

        if not items:
            raise ValueError("nothing to charge")
        checked: list[tuple[str, Decimal]] = []
        for name, units in items:
            n = self._check_units(units)
            if name not in SKUS:
                raise KeyError(name)
            checked.append((name, n))
        subjects = {policy.spend_subject(name) for name, _ in checked}
        if len(subjects) != 1:
            raise ValueError("one request is charged to one billing subject")
        subject = subjects.pop()
        if subject is None:
            return None, {}, checked
        details = self._gate(subject, [name for name, _ in checked])
        self._ceilings(fingerprint)
        return subject, details, checked

    def _admit(
        self,
        data: dict[str, Any],
        subject: str,
        details: dict[str, Any],
        checked: list[tuple[str, Decimal]],
        *,
        record: bool,
    ) -> list[Quote]:
        """Every limit that needs the store, evaluated on `data` (which the caller commits or discards)."""
        month = current_month()
        if not data.get("initialized_on"):
            if details["billing"] == "enabled":
                raise self.ledger.not_initialised()
            data["initialized_on"] = _today().isoformat()  # billing none: a missing store is created
        if record:
            self._take_lease(data)
        day, counts = current_day(), data["days"]
        cap = details.get("daily_request_cap", DEFAULT_DAILY_REQUEST_CAP)
        today = counts.get(day, {}).get(subject, 0)
        if today >= cap:
            raise BudgetExceeded(
                f"daily request cap for {subject} reached: {today} requests today (the owner's record "
                f"allows {cap}; only the owner can change it, up to {MAX_DAILY_REQUEST_CAP:,})"
            )
        quotes: list[Quote] = []
        for name, units in checked:
            q = self._evaluate(data, month, name, units, details, subject)
            _add(data, month, name, units)
            quotes.append(q)
        if record:
            counts.setdefault(day, {})[subject] = today + 1
            for old in sorted(counts)[:-DAYS_KEPT]:
                del counts[old]
        return quotes

    def _take_lease(self, data: dict[str, Any]) -> None:
        """Hold one of the MAX_ACTIVE_RUNS leases on this store, or refuse."""
        now = self._wall_clock()
        live = [
            lease
            for lease in data["leases"]
            if now - float(lease["started"]) <= float(lease["max_elapsed_s"]) + LEASE_GRACE_S
        ]
        if not any(lease["id"] == self._run_id for lease in live):
            if len(live) >= MAX_ACTIVE_RUNS:
                data["leases"] = live
                raise BudgetExceeded(
                    f"{len(live)} runs are already active on this usage store (at most {MAX_ACTIVE_RUNS} "
                    "runs at once); wait for one to finish. A run that died is ignored after its time "
                    f"ceiling plus {LEASE_GRACE_S:g} s"
                )
            live.append(
                {
                    "id": self._run_id,
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "started": now,
                    "max_elapsed_s": self.max_elapsed_s,
                }
            )
        data["leases"] = live

    def check(self, sku_name: str, units: Any = 1, fingerprint: str | None = None) -> Quote:
        """Raise BudgetExceeded if this charge is not allowed; do not record it."""
        with self._lock:
            subject, details, checked = self._prepare([(sku_name, units)], fingerprint)
            if subject is None:
                return Quote(SKUS[sku_name], checked[0][1], ZERO, ZERO)
            return self._admit(self.ledger.read(), subject, details, checked, record=False)[0]

    def charge(self, sku_name: str, units: Any = 1, fingerprint: str | None = None) -> Quote:
        """Check every limit and record the reservation as one locked operation. Call immediately
        before the billable request, and again before every retry of it."""
        return self.charge_many([(sku_name, units)], fingerprint)[0]

    def charge_many(self, items: Sequence[tuple[str, Any]], fingerprint: str | None = None) -> list[Quote]:
        """One request that bills several SKUs (for example a place and its reviews): all are checked
        and recorded together, or none is. It counts as one request."""
        with self._lock:
            subject, details, checked = self._prepare(items, fingerprint)
            if subject is None:  # the simulator: free, never recorded in the store
                for name, units in checked:
                    self.run_units[name] = self.run_units.get(name, ZERO) + units
                return [Quote(SKUS[name], units, ZERO, ZERO) for name, units in checked]
            with self.ledger.transaction() as data:
                quotes = self._admit(data, subject, details, checked, record=True)
            self._hold_lease()
            self.requests += 1
            if fingerprint is not None:
                self._seen[fingerprint] = self._seen.get(fingerprint, 0) + 1
            for (name, units), q in zip(checked, quotes, strict=True):
                self.run_usd += q.out_of_pocket_usd
                self.run_units[name] = self.run_units.get(name, ZERO) + units
            return quotes

    def _hold_lease(self) -> None:
        if self._finalizer is None or not self._finalizer.alive:
            self._finalizer = weakref.finalize(self, _release_on_exit, self.ledger, self._run_id)

    # -- reporting and reconciliation -----------------------------------------------------------

    def summary(self) -> dict:
        return {
            "mode": self.mode,
            "max_usd": self.max_usd,
            "run_out_of_pocket_usd": round(float(self.run_usd), 4),
            "run_units": {k: json_number(v) for k, v in self.run_units.items()},
            "requests": self.requests,
            "max_requests": self.max_requests,
            "max_elapsed_s": self.max_elapsed_s,
        }

    def month_report(self) -> list[dict]:
        out = []
        used = {sku: (units, usd) for sku, units, usd in self.ledger.rows()}
        for name, sku in SKUS.items():
            units, usd = used.get(name, (ZERO, ZERO))
            out.append(
                {
                    "sku": name,
                    "provider": sku.provider,
                    "used": json_number(units),
                    "free_units": sku.free_units,
                    "remaining_free": max(0.0, sku.free_units - float(units)),
                    "billable_usd": round(float(usd), 4),
                    "usd_per_unit": sku.usd_per_unit,
                    "price_known": sku.price_known,
                }
            )
        return out

    def gateway_report(self) -> dict[str, Any]:
        """The owner's spend records with caps, this month's out-of-pocket per subject, the ceilings."""
        from . import policy

        data = self.ledger.read()
        month = current_month()
        day = current_day()
        records = {r["subject"]: r for r in policy.spend_records()}
        subjects: dict[str, Any] = {}
        for subject in policy.SPEND_SUBJECTS:
            rec = records.get(subject)
            row: dict[str, Any] = {"recorded": rec is not None}
            if rec:
                row.update(rec["details"])
                row["approved_on"] = rec.get("approved_on")
                row["requests_today"] = json_number(data["days"].get(day, {}).get(subject, 0))
                row["daily_request_cap"] = rec["details"].get("daily_request_cap", DEFAULT_DAILY_REQUEST_CAP)
                if rec["details"]["billing"] == "enabled":
                    row["month_out_of_pocket_usd"] = round(
                        float(subject_out_of_pocket(data, month, subject)), 4
                    )
                    row["period_cap_usd"] = rec["details"]["period_cap_usd"]
            subjects[subject] = row
        return {
            "spend": subjects,
            "ceilings": {
                "max_requests": {"default": DEFAULT_MAX_REQUESTS, "hard_maximum": HARD_MAX_REQUESTS},
                "max_elapsed_s": {"default": DEFAULT_MAX_ELAPSED_S, "exhaustive": 3600},
                "max_identical_requests": MAX_REPEATS,
                "daily_request_cap": {
                    "default": DEFAULT_DAILY_REQUEST_CAP,
                    "hard_maximum": MAX_DAILY_REQUEST_CAP,
                },
                "max_active_runs": MAX_ACTIVE_RUNS,
                "by_depth": {
                    k: {"max_requests": v[0], "max_elapsed_s": v[1]} for k, v in DEPTH_CEILINGS.items()
                },
            },
            "store": {"initialized_on": data.get("initialized_on"), "active_runs": len(data["leases"])},
            "kill_switch": policy.kill_status(),
            "reconciliations": _plain(data["reconciliations"][-10:]),
        }

    def reconcile(self, sku_name: str, units: Any, *, owner_statement: str) -> dict[str, Any]:
        """Raise this month's recorded units for a SKU to max(current, units), as read from the provider's console."""
        from . import policy

        n = self._check_units(units)
        if not (owner_statement or "").strip():
            raise ValueError("a reconciliation needs the owner's own statement")
        if sku_name not in SKUS or policy.spend_subject(sku_name) is None:
            raise ValueError(f"{sku_name!r} is not a chargeable SKU; see `maps budget` for the list")
        month = current_month()
        with self.ledger.transaction() as data:
            before = _units(data, month, sku_name)
            after = max(before, n)
            _add(data, month, sku_name, after - before)
            data["reconciliations"].append(
                {
                    "date": _today().isoformat(),
                    "month": month,
                    "sku": sku_name,
                    "units": n,
                    "statement": owner_statement.strip(),
                }
            )
            row = data["months"][month][sku_name]
            return {
                "month": month,
                "sku": sku_name,
                "previous_units": json_number(before),
                "units": json_number(row["units"]),
                "usd": round(float(row["usd"]), 4),
            }

    @staticmethod
    def restore_skus(subject: str) -> list[str]:
        """The SKUs of a billing subject that have a finite free allowance: a restore needs a figure for each."""
        from . import policy

        return [
            n
            for n, s in SKUS.items()
            if s.provider != "sim" and policy.spend_subject(n) == subject and math.isfinite(s.free_units)
        ]

    def restore(
        self, figures: dict[str, Any], *, owner_statement: str, subjects: Sequence[str]
    ) -> dict[str, Any]:
        """Re-initialise a missing or emptied store from the provider console's figures (never lowers).

        `figures` maps SKU to this month's units. Every SKU with a finite free allowance of each of
        `subjects` needs a figure, so an unknown is never taken as zero.
        """
        from . import policy

        if not (owner_statement or "").strip():
            raise ValueError("a restore needs the owner's own statement")
        if not subjects:
            raise ValueError("no billing subject has billing enabled: there is nothing to restore")
        parsed: dict[str, Decimal] = {}
        for sku, units in figures.items():
            if sku not in SKUS or policy.spend_subject(sku) is None:
                raise ValueError(f"{sku!r} is not a chargeable SKU; see `maps budget` for the list")
            parsed[sku] = to_decimal(units, name=f"units for {sku}")
        missing = [n for s in subjects for n in self.restore_skus(s) if n not in parsed]
        if missing:
            raise ValueError(
                "a figure from the provider's console is needed for every SKU with a finite free "
                "allowance, so an unknown is never taken as zero; missing: "
                + ", ".join(f"--sku {n}=UNITS" for n in missing)
            )
        month = current_month()
        with self.ledger.transaction() as data:
            for sku, n in parsed.items():
                before = _units(data, month, sku)
                _add(data, month, sku, max(before, n) - before)
                data["reconciliations"].append(
                    {
                        "date": _today().isoformat(),
                        "month": month,
                        "sku": sku,
                        "units": n,
                        "statement": f"restore: {owner_statement.strip()}",
                    }
                )
            data.setdefault("initialized_on", _today().isoformat())
            if not data["initialized_on"]:
                data["initialized_on"] = _today().isoformat()
            return {
                "month": month,
                "initialized_on": data["initialized_on"],
                "restored": {k: json_number(v) for k, v in sorted(parsed.items())},
            }
