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
  identical requests.

The counters live in one JSON file, `usage.json`, in the policy folder (the state directory is lost on
ephemeral machines). It holds counters only, never response content. Every read-modify-write holds an
exclusive `flock` on a sibling lock file and ends with an atomic replace. A file that cannot be parsed or
a store that cannot be used refuses every chargeable request (fail closed).

Limits of this guard, stated plainly:
- It only sees requests made through this toolkit with this policy folder. Usage of the same Google
  project or Apify account from anywhere else is invisible. Reconcile with `maps budget reconcile`.
- Prices and free allowances live in pricing.py and can go stale.
The hard stop is a quota cap set in the provider's own console (docs/setup.md).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
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
    "google_agent_tools": ("google.agent_tools.search",),
    "gemini_maps": ("google.gemini_maps_grounding",),
    "apify": ("apify.place", "apify.review"),
    "apify_contributor": ("apify.contributor",),
    "dataforseo": ("dataforseo.maps_search", "dataforseo.reviews"),
}


class BudgetExceeded(RuntimeError):
    """Raised before a request that the current cost policy does not allow."""


class GatewayRefusal(BudgetExceeded):
    """A refusal that happens before the first request of a command (the CLI exits 2 with `Spending:`)."""


class LedgerUnavailable(GatewayRefusal):
    """The usage store cannot be parsed or used: every chargeable request is refused."""


def current_month() -> str:
    return datetime.now(BILLING_TZ).strftime("%Y-%m")


def _today() -> date:
    return date.today()


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def _empty() -> dict[str, Any]:
    return {"months": {}, "reconciliations": []}


def _units(data: dict[str, Any], month: str, sku: str) -> float:
    return data["months"].get(month, {}).get(sku, {}).get("units", 0.0)


def _provider_usd(data: dict[str, Any], month: str, provider: str) -> float:
    return sum(
        row["usd"]
        for sku, row in data["months"].get(month, {}).items()
        if sku in SKUS and SKUS[sku].provider == provider
    )


def _subject_usd(data: dict[str, Any], month: str, subject: str) -> float:
    from . import policy

    return sum(
        row["usd"]
        for sku, row in data["months"].get(month, {}).items()
        if sku in SKUS and policy.spend_subject(sku) == subject
    )


def subject_out_of_pocket(data: dict[str, Any], month: str, subject: str) -> float:
    """This month's out-of-pocket total for a billing subject: list-price value beyond the free
    allowances, less the provider's free monthly credit."""
    from . import policy

    providers = {s.provider for n, s in SKUS.items() if policy.spend_subject(n) == subject}
    credit = sum(PROVIDER_FREE_USD.get(p, 0.0) for p in providers)
    return max(0.0, _subject_usd(data, month, subject) - credit)


class Ledger:
    """Monthly usage counters in one JSON file (or in memory for unit tests)."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self.memory = self.path == ":memory:"
        self._mem = _empty()
        self._tlock = threading.RLock()
        self._active: dict[str, Any] | None = None

    # -- the file -----------------------------------------------------------------------------

    def _fail(self, why: str) -> LedgerUnavailable:
        return LedgerUnavailable(
            f"the usage store {self.path} {why}; every chargeable request is refused until the owner "
            "repairs or removes the file"
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
                if not isinstance(row, dict) or not _num(row.get("units")) or not _num(row.get("usd")):
                    raise self._fail("has a counter that is not a non-negative number")
        if not isinstance(doc.setdefault("reconciliations", []), list):
            raise self._fail("has no valid 'reconciliations' list")
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
            return self._validate(json.loads(text))
        except ValueError:
            raise self._fail("is not valid JSON") from None

    def _save(self, data: dict[str, Any]) -> None:
        path = Path(self.path)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".usage.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        """Exclusive read-modify-write: the data is saved (atomically) when the block ends normally and changed it."""
        with self._tlock:
            if self.memory:
                self._active = self._mem
                try:
                    yield self._mem
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
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except OSError as e:
                    raise self._fail(f"cannot be locked ({type(e).__name__})") from None
                data = self._load()
                before = json.dumps(data, sort_keys=True)
                self._active = data
                try:
                    yield data
                finally:
                    self._active = None
                if json.dumps(data, sort_keys=True) != before:
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
            return json.loads(json.dumps(data))

    def _view(self) -> dict[str, Any]:
        with self._tlock:
            return self._active if self._active is not None else self._load()

    # -- queries ------------------------------------------------------------------------------

    def units(self, sku: str, month: str | None = None) -> float:
        return _units(self._view(), month or current_month(), sku)

    def provider_usd(self, provider: str, month: str | None = None) -> float:
        """List-price value of this month's billable units for one provider."""
        return _provider_usd(self._view(), month or current_month(), provider)

    def rows(self, month: str | None = None) -> list[tuple[str, float, float]]:
        months = self._view()["months"].get(month or current_month(), {})
        return [(sku, row["units"], row["usd"]) for sku, row in sorted(months.items())]

    def add(self, sku: str, units: float, usd: float, month: str | None = None) -> None:
        with self.transaction() as data:
            _add(data, month or current_month(), sku, units, usd)


def _add(data: dict[str, Any], month: str, sku: str, units: float, usd: float) -> None:
    row = data["months"].setdefault(month, {}).setdefault(sku, {"units": 0, "usd": 0.0})
    row["units"] += units
    row["usd"] += usd


@dataclass
class Quote:
    sku: Sku
    units: float
    billable_usd: float  # list-price value of the units beyond the SKU's free allowance
    out_of_pocket_usd: float  # what is left after the provider's free monthly credit


class Budget:
    """Cost policy and request ceilings for one run.

    mode="free":    refuse anything with out-of-pocket cost, and stop at
                    `reserve` (default 90%) of each free allowance to leave headroom
                    for usage this ledger cannot see.
    mode="quality": allow out-of-pocket cost up to max_usd for this run, which can never exceed the
                    owner's per-search cap, and within the owner's cap for the billing month.
    `subjects` names the billing subjects the run will use (the per-search cap is checked against
    those); None checks every recorded one.
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
    ) -> None:
        if mode not in ("free", "quality"):
            raise ValueError("mode must be 'free' or 'quality'")
        if not (math.isfinite(max_usd) and max_usd >= 0):
            raise ValueError("max_usd must be a finite number >= 0")
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
        self.reserve = reserve
        self.max_requests = max_requests
        self.max_elapsed_s = float(max_elapsed_s)
        self.max_repeats = max_repeats
        self.run_usd = 0.0  # out-of-pocket spend in this run
        self.run_units: dict[str, float] = {}
        self.requests = 0  # chargeable requests reserved in this run
        self._clock = clock
        self._t0 = clock()
        self._seen: dict[str, int] = {}
        self._lock = threading.Lock()
        if mode == "quality":
            self._check_per_search_cap(subjects)

    # -- the owner's records ------------------------------------------------------------------

    def _check_per_search_cap(self, subjects: list[str] | tuple[str, ...] | None) -> None:
        from . import policy

        for subject in subjects if subjects is not None else policy.SPEND_SUBJECTS:
            rec = policy.spend_record(subject)
            if rec is None or rec["details"].get("billing") != "enabled":
                continue
            cap = float(rec["details"]["per_search_cap_usd"])
            if self.max_usd > cap + 1e-9:
                raise GatewayRefusal(
                    f"--max-usd {self.max_usd:g} is above the owner's per-search cap of ${cap:.2f} for "
                    f"{subject}. Only the owner can raise it, by recording a new spending authorization "
                    f"(maps policy approve {subject} --kind spend ...)"
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
        if details["billing"] == "enabled":
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
                        "unknown price means block. Under billing none it is allowed."
                    )
        return details

    def preflight(self, provider: str) -> None:
        """Refuse, before a command's first request, what `charge` would refuse for every request."""
        from . import policy

        subject = policy.spend_subject(provider)
        if subject is None:
            return
        try:
            self.ledger.read()  # an unusable store refuses everything
            self._gate(subject, PROVIDER_SKUS.get(provider, ()))
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
    def _check_units(units: float) -> None:
        if not (math.isfinite(units) and units >= 0):
            raise ValueError(f"units must be a finite number >= 0, got {units!r}")

    @staticmethod
    def _quote(data: dict[str, Any], month: str, sku_name: str, units: float, billing_none: bool) -> Quote:
        sku = SKUS[sku_name]
        if billing_none or sku.provider == "sim":
            return Quote(sku, units, 0.0, 0.0)
        used = _units(data, month, sku_name)
        free = sku.free_units
        paid_before = max(0.0, used - free)
        paid_after = max(0.0, used + units - free)
        billable = (paid_after - paid_before) * sku.usd_per_unit
        credit_left = max(
            0.0, PROVIDER_FREE_USD.get(sku.provider, 0.0) - _provider_usd(data, month, sku.provider)
        )
        return Quote(sku, units, billable, max(0.0, billable - credit_left))

    def quote(self, sku_name: str, units: float = 1) -> Quote:
        """What a charge would cost. Read-only; makes no check of the owner's records."""
        from . import policy

        self._check_units(units)
        subject = policy.spend_subject(sku_name)
        rec = policy.spend_record(subject) if subject else None
        none = bool(rec and rec["details"].get("billing") == "none")
        return self._quote(self.ledger.read(), current_month(), sku_name, units, none)

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
        units: float,
        details: dict[str, Any],
        subject: str,
    ) -> Quote:
        none = details["billing"] == "none"
        q = self._quote(data, month, sku_name, units, none)
        if none:
            return q  # cannot be charged: nothing is out-of-pocket; the ceilings were checked already
        sku = q.sku
        if self.mode == "free":
            if q.out_of_pocket_usd > 0:
                raise BudgetExceeded(
                    f"{sku_name}: free allowance exhausted this month; "
                    f"this request would cost ${q.out_of_pocket_usd:.4f}. "
                    "Re-run with --cost quality --max-usd N to authorize spending."
                )
            if sku.free_units and _units(data, month, sku_name) + units > sku.free_units * self.reserve:
                raise BudgetExceeded(
                    f"{sku_name}: reached {self.reserve:.0%} of the {sku.free_units:g} free units "
                    "this month (headroom kept for usage this ledger cannot see)."
                )
            credit = PROVIDER_FREE_USD.get(sku.provider, 0.0)
            if (
                credit
                and q.billable_usd
                and (_provider_usd(data, month, sku.provider) + q.billable_usd > credit * self.reserve)
            ):
                raise BudgetExceeded(
                    f"{sku_name}: reached {self.reserve:.0%} of the ${credit:g} free monthly "
                    f"{sku.provider} credit."
                )
        else:
            if self.run_usd + q.out_of_pocket_usd > self.max_usd + 1e-9:
                raise BudgetExceeded(
                    f"{sku_name}: this request (${q.out_of_pocket_usd:.4f}) would exceed the "
                    f"authorized ${self.max_usd:.2f} for this run (spent ${self.run_usd:.4f})."
                )
            cap = float(details["period_cap_usd"])
            spent = subject_out_of_pocket(data, month, subject)
            if spent + q.out_of_pocket_usd > cap + 1e-9:
                raise BudgetExceeded(
                    f"{sku_name}: the owner's period cap for {subject} would be exceeded: ${spent:.4f} already "
                    f"spent this billing month plus ${q.out_of_pocket_usd:.4f} for this request is above "
                    f"${cap:.2f}."
                )
        return q

    def _prepare(
        self, sku_name: str, units: float, fingerprint: str | None
    ) -> tuple[str | None, dict[str, Any]]:
        """The checks that need no counters. Returns (billing subject or None for sim, spend details)."""
        from . import policy

        self._check_units(units)
        if sku_name not in SKUS:
            raise KeyError(sku_name)
        subject = policy.spend_subject(sku_name)
        if subject is None:
            return None, {}
        details = self._gate(subject, [sku_name])
        self._ceilings(fingerprint)
        return subject, details

    def check(self, sku_name: str, units: float = 1, fingerprint: str | None = None) -> Quote:
        """Raise BudgetExceeded if this charge is not allowed; do not record it."""
        with self._lock:
            subject, details = self._prepare(sku_name, units, fingerprint)
            if subject is None:
                return Quote(SKUS[sku_name], units, 0.0, 0.0)
            with self.ledger.transaction() as data:
                return self._evaluate(data, current_month(), sku_name, units, details, subject)

    def charge(self, sku_name: str, units: float = 1, fingerprint: str | None = None) -> Quote:
        """Check every limit and record the reservation as one locked operation. Call immediately
        before the billable request, and again before every retry of it."""
        with self._lock:
            subject, details = self._prepare(sku_name, units, fingerprint)
            if subject is None:  # the simulator: free, never recorded in the store
                self.run_units[sku_name] = self.run_units.get(sku_name, 0.0) + units
                return Quote(SKUS[sku_name], units, 0.0, 0.0)
            month = current_month()
            with self.ledger.transaction() as data:
                q = self._evaluate(data, month, sku_name, units, details, subject)
                _add(data, month, sku_name, units, q.billable_usd)
            self.requests += 1
            if fingerprint is not None:
                self._seen[fingerprint] = self._seen.get(fingerprint, 0) + 1
            self.run_usd += q.out_of_pocket_usd
            self.run_units[sku_name] = self.run_units.get(sku_name, 0.0) + units
            return q

    # -- reporting and reconciliation -----------------------------------------------------------

    def summary(self) -> dict:
        return {
            "mode": self.mode,
            "max_usd": self.max_usd,
            "run_out_of_pocket_usd": round(self.run_usd, 4),
            "run_units": self.run_units,
            "requests": self.requests,
            "max_requests": self.max_requests,
            "max_elapsed_s": self.max_elapsed_s,
        }

    def month_report(self) -> list[dict]:
        out = []
        used = {sku: (units, usd) for sku, units, usd in self.ledger.rows()}
        for name, sku in SKUS.items():
            units, usd = used.get(name, (0.0, 0.0))
            out.append(
                {
                    "sku": name,
                    "provider": sku.provider,
                    "used": units,
                    "free_units": sku.free_units,
                    "remaining_free": max(0.0, sku.free_units - units),
                    "billable_usd": round(usd, 4),
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
        records = {r["subject"]: r for r in policy.spend_records()}
        subjects: dict[str, Any] = {}
        for subject in policy.SPEND_SUBJECTS:
            rec = records.get(subject)
            row: dict[str, Any] = {"recorded": rec is not None}
            if rec:
                row.update(rec["details"])
                row["approved_on"] = rec.get("approved_on")
                if rec["details"]["billing"] == "enabled":
                    row["month_out_of_pocket_usd"] = round(subject_out_of_pocket(data, month, subject), 4)
                    row["period_cap_usd"] = rec["details"]["period_cap_usd"]
            subjects[subject] = row
        return {
            "spend": subjects,
            "ceilings": {
                "max_requests": {"default": DEFAULT_MAX_REQUESTS, "hard_maximum": HARD_MAX_REQUESTS},
                "max_elapsed_s": {"default": DEFAULT_MAX_ELAPSED_S, "exhaustive": 3600},
                "max_identical_requests": MAX_REPEATS,
                "by_depth": {
                    k: {"max_requests": v[0], "max_elapsed_s": v[1]} for k, v in DEPTH_CEILINGS.items()
                },
            },
            "kill_switch": policy.kill_status(),
            "reconciliations": data["reconciliations"][-10:],
        }

    def reconcile(self, sku_name: str, units: float, *, owner_statement: str) -> dict[str, Any]:
        """Raise this month's recorded units for a SKU to max(current, units), as read from the provider's console."""
        from . import policy

        self._check_units(units)
        if not (owner_statement or "").strip():
            raise ValueError("a reconciliation needs the owner's own statement")
        if sku_name not in SKUS or policy.spend_subject(sku_name) is None:
            raise ValueError(f"{sku_name!r} is not a chargeable SKU; see `maps budget` for the list")
        sku = SKUS[sku_name]
        subject = policy.spend_subject(sku_name)
        rec = policy.spend_record(subject) if subject else None
        priced = bool(rec and rec["details"].get("billing") == "enabled" and sku.price_known)
        month = current_month()
        with self.ledger.transaction() as data:
            before = _units(data, month, sku_name)
            after = max(before, units)
            usd = 0.0
            if priced and after > before:
                usd = (
                    max(0.0, after - sku.free_units) - max(0.0, before - sku.free_units)
                ) * sku.usd_per_unit
            _add(data, month, sku_name, after - before, usd)
            data["reconciliations"].append(
                {
                    "date": _today().isoformat(),
                    "month": month,
                    "sku": sku_name,
                    "units": units,
                    "statement": owner_statement.strip(),
                }
            )
            row = data["months"][month][sku_name]
            return {
                "month": month,
                "sku": sku_name,
                "previous_units": before,
                "units": row["units"],
                "usd": round(row["usd"], 4),
            }
