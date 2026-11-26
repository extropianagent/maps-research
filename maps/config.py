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


def retention_days() -> float:
    """Runs older than this are deleted at the start of every search."""
    raw = os.environ.get("MAPS_RETENTION_DAYS")
    try:
        return float(raw) if raw else 30.0
    except ValueError:
        return 30.0


def ledger_path() -> Path:
    return state_dir() / "ledger.sqlite"
