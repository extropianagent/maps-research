"""Environment-driven settings, read on every call so tests and agents can redirect state."""

from __future__ import annotations

import math
import os
from pathlib import Path

from .geography import BBox

# The simulator's default world: a box in Riyadh, matching sim.py's Arabic/English phrase banks.
SIM_AREA = BBox(24.5, 46.5, 24.9, 46.9)


def state_dir() -> Path:
    """Where runs and the area cache live (outside the repo, see docs/permissions.md)."""
    return Path(os.environ.get("MAPS_STATE_DIR") or "~/.maps-research").expanduser()


REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_RETENTION_DAYS = 30.0  # Maps content may not be kept longer (docs/permissions.md)


_policy_dir_override: Path | None = None


def set_policy_dir_for_tests(path: Path | str | None) -> None:
    """Redirect the policy folder. For tests only: there is deliberately no environment variable."""
    global _policy_dir_override
    _policy_dir_override = Path(path) if path is not None else None


def _is_source_checkout() -> bool:
    return (REPO_ROOT / "pyproject.toml").is_file()


def policy_dir() -> Path:
    """Approvals and stops. In a source checkout: its `policy/` folder (committed, so it survives
    ephemeral machines). Installed as a package: a fixed per-user folder, never under the state
    directory (`purge --all` must not delete it). Not configurable by any MAPS_* variable."""
    if _policy_dir_override is not None:
        return _policy_dir_override
    if _is_source_checkout():
        return REPO_ROOT / "policy"
    return Path.home() / ".maps-research-policy"


def policy_location() -> str:
    """Which case `policy_dir` is in."""
    if _policy_dir_override is not None:
        return "test override"
    return "source checkout" if _is_source_checkout() else "per-user folder"


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
    """The usage counters. In the policy folder, not the state directory: the state directory is lost on
    ephemeral machines and the counters must survive (they hold no Maps content)."""
    return policy_dir() / "usage.json"
