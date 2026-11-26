"""Environment-driven settings, read on every call so tests and agents can redirect state."""

from __future__ import annotations

import math
import os
from pathlib import Path

from .geography import BBox

# The simulator's default world: a box in Riyadh, matching sim.py's Arabic/English phrase banks.
SIM_AREA = BBox(24.5, 46.5, 24.9, 46.9)


def state_dir() -> Path:
    """Where runs, the ledger and the area cache live (outside the repo, see docs/permissions.md)."""
    return Path(os.environ.get("MAPS_STATE_DIR") or "~/.maps-research").expanduser()


REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_RETENTION_DAYS = 30.0  # Maps content may not be kept longer (docs/permissions.md)


_policy_dir_override: Path | None = None


def set_policy_dir_for_tests(path: Path | str | None) -> None:
    """Redirect the policy folder. For tests only: there is deliberately no environment variable."""
    global _policy_dir_override
    _policy_dir_override = Path(path) if path is not None else None


def policy_dir() -> Path:
    """Approvals and stops live in the repo so they survive ephemeral machines. Not configurable."""
    return _policy_dir_override or REPO_ROOT / "policy"


def _requested_retention() -> float:
    """The configured days, or the cap when unset or unusable (non-finite, zero, negative, unparsable)."""
    raw = os.environ.get("MAPS_RETENTION_DAYS")
    try:
        days = float(raw) if raw else MAX_RETENTION_DAYS
    except ValueError:
        return MAX_RETENTION_DAYS
    return days if math.isfinite(days) and days > 0 else MAX_RETENTION_DAYS


def retention_clamped() -> bool:
    """True when MAPS_RETENTION_DAYS asked for more than the cap."""
    return _requested_retention() > MAX_RETENTION_DAYS


def retention_days() -> float:
    """Runs older than this are deleted on every command. Always finite, in (0, 30]."""
    return min(_requested_retention(), MAX_RETENTION_DAYS)


def ledger_path() -> Path:
    return state_dir() / "ledger.sqlite"
