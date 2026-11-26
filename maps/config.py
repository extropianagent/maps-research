"""Environment-driven settings, read on every call so tests and agents can redirect state."""

from __future__ import annotations

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


def policy_dir() -> Path:
    """Approvals and stops live in the repo so they survive ephemeral machines."""
    return Path(os.environ.get("MAPS_POLICY_DIR") or REPO_ROOT / "policy")


def _requested_retention() -> float:
    raw = os.environ.get("MAPS_RETENTION_DAYS")
    try:
        return float(raw) if raw else MAX_RETENTION_DAYS
    except ValueError:
        return MAX_RETENTION_DAYS


def retention_clamped() -> bool:
    """True when MAPS_RETENTION_DAYS asked for more than the cap."""
    return _requested_retention() > MAX_RETENTION_DAYS


def retention_days() -> float:
    """Runs older than this are deleted at the start of every search. Never more than 30."""
    return min(_requested_retention(), MAX_RETENTION_DAYS)


def ledger_path() -> Path:
    return state_dir() / "ledger.sqlite"
