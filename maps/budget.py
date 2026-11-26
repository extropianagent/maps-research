"""Spend control. Free-first means the tool refuses a request *before* it would cost money.

Every billable request is charged against a SKU. The ledger (SQLite, one row per
month+SKU) counts units used this month; `Budget.charge` compares that with the
SKU's free monthly allowance and the provider's free monthly credit.

Limits of this guard, stated plainly:
- It only sees requests made through this toolkit with this state directory.
  Usage of the same Google project or Apify account from anywhere else is invisible.
- Prices and free allowances live in pricing.py and can go stale.
The hard stop is a quota cap set in the provider's own console (docs/setup.md).
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .pricing import PROVIDER_FREE_USD, SKUS, Sku

# Google bills by Pacific-time calendar month.
BILLING_TZ = ZoneInfo("America/Los_Angeles")


class BudgetExceeded(RuntimeError):
    """Raised before a request that the current cost policy does not allow."""


def current_month() -> str:
    return datetime.now(BILLING_TZ).strftime("%Y-%m")


class Ledger:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS usage ("
            " month TEXT NOT NULL, sku TEXT NOT NULL,"
            " units REAL NOT NULL DEFAULT 0, usd REAL NOT NULL DEFAULT 0,"
            " PRIMARY KEY (month, sku))"
        )
        self.db.commit()

    def units(self, sku: str, month: str | None = None) -> float:
        row = self.db.execute(
            "SELECT units FROM usage WHERE month=? AND sku=?", (month or current_month(), sku)
        ).fetchone()
        return row[0] if row else 0.0

    def provider_usd(self, provider: str, month: str | None = None) -> float:
        """List-price value of this month's billable units for one provider."""
        month = month or current_month()
        total = 0.0
        for sku, usd in self.db.execute("SELECT sku, usd FROM usage WHERE month=?", (month,)):
            if sku in SKUS and SKUS[sku].provider == provider:
                total += usd
        return total

    def add(self, sku: str, units: float, usd: float, month: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO usage (month, sku, units, usd) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(month, sku) DO UPDATE SET units = units + excluded.units, usd = usd + excluded.usd",
            (month or current_month(), sku, units, usd),
        )
        self.db.commit()

    def rows(self, month: str | None = None) -> list[tuple[str, float, float]]:
        return list(
            self.db.execute(
                "SELECT sku, units, usd FROM usage WHERE month=? ORDER BY sku", (month or current_month(),)
            )
        )


@dataclass
class Quote:
    sku: Sku
    units: float
    billable_usd: float  # list-price value of the units beyond the SKU's free allowance
    out_of_pocket_usd: float  # what is left after the provider's free monthly credit


class Budget:
    """Cost policy for one run.

    mode="free":    refuse anything with out-of-pocket cost, and stop at
                    `reserve` (default 90%) of each free allowance to leave headroom
                    for usage this ledger cannot see.
    mode="quality": allow out-of-pocket cost up to max_usd for this run.
    """

    def __init__(
        self, ledger: Ledger, *, mode: str = "free", max_usd: float = 0.0, reserve: float = 0.9
    ) -> None:
        if mode not in ("free", "quality"):
            raise ValueError("mode must be 'free' or 'quality'")
        if not (math.isfinite(max_usd) and max_usd >= 0):
            raise ValueError("max_usd must be a finite number >= 0")
        if not (0 < reserve <= 1):
            raise ValueError("reserve must be in (0, 1]")
        if mode == "free" and max_usd:
            raise ValueError("max_usd only applies to quality mode")
        self.ledger = ledger
        self.mode = mode
        self.max_usd = max_usd
        self.reserve = reserve
        self.run_usd = 0.0  # out-of-pocket spend in this run
        self.run_units: dict[str, float] = {}

    @staticmethod
    def _check_units(units: float) -> None:
        if not (math.isfinite(units) and units >= 0):
            raise ValueError(f"units must be a finite number >= 0, got {units!r}")

    def quote(self, sku_name: str, units: float = 1) -> Quote:
        self._check_units(units)
        sku = SKUS[sku_name]
        used = self.ledger.units(sku_name)
        free = sku.free_units
        paid_before = max(0.0, used - free)
        paid_after = max(0.0, used + units - free)
        billable = (paid_after - paid_before) * sku.usd_per_unit
        credit_left = max(
            0.0, PROVIDER_FREE_USD.get(sku.provider, 0.0) - self.ledger.provider_usd(sku.provider)
        )
        return Quote(sku, units, billable, max(0.0, billable - credit_left))

    def check(self, sku_name: str, units: float = 1) -> Quote:
        """Raise BudgetExceeded if this charge is not allowed; do not record it."""
        self._check_units(units)
        q = self.quote(sku_name, units)
        sku = q.sku
        if self.mode == "free":
            if q.out_of_pocket_usd > 0:
                raise BudgetExceeded(
                    f"{sku_name}: free allowance exhausted this month; "
                    f"this request would cost ${q.out_of_pocket_usd:.4f}. "
                    "Re-run with --cost quality --max-usd N to authorize spending."
                )
            if sku.free_units and self.ledger.units(sku_name) + units > sku.free_units * self.reserve:
                raise BudgetExceeded(
                    f"{sku_name}: reached {self.reserve:.0%} of the {sku.free_units:g} free units "
                    "this month (headroom kept for usage this ledger cannot see)."
                )
            credit = PROVIDER_FREE_USD.get(sku.provider, 0.0)
            if (
                credit
                and q.billable_usd
                and (self.ledger.provider_usd(sku.provider) + q.billable_usd > credit * self.reserve)
            ):
                raise BudgetExceeded(
                    f"{sku_name}: reached {self.reserve:.0%} of the ${credit:g} free monthly "
                    f"{sku.provider} credit."
                )
        elif self.run_usd + q.out_of_pocket_usd > self.max_usd + 1e-9:
            raise BudgetExceeded(
                f"{sku_name}: this request (${q.out_of_pocket_usd:.4f}) would exceed the "
                f"authorized ${self.max_usd:.2f} for this run (spent ${self.run_usd:.4f})."
            )
        return q

    def charge(self, sku_name: str, units: float = 1) -> Quote:
        """Check, then record. Call immediately before the billable request."""
        self._check_units(units)
        q = self.check(sku_name, units)
        self.ledger.add(sku_name, units, q.billable_usd)
        self.run_usd += q.out_of_pocket_usd
        self.run_units[sku_name] = self.run_units.get(sku_name, 0.0) + units
        return q

    def summary(self) -> dict:
        return {
            "mode": self.mode,
            "max_usd": self.max_usd,
            "run_out_of_pocket_usd": round(self.run_usd, 4),
            "run_units": self.run_units,
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
                }
            )
        return out
