"""Run storage: checkpoints that make a search resumable, with a retention window.

A run is a directory holding an append-only `tasks.jsonl` (one line per completed
search task, with the places it returned) and `reviews.jsonl`. The place set is a
fold over those lines, so a crash loses at most the task in flight.

Runs hold Google Maps content, which the Maps Platform terms do not let you keep
indefinitely. `purge` deletes runs past the retention window; the CLI calls it on
every search. Anything worth keeping long-term belongs in knowledge/ as place IDs
plus your own notes (docs/permissions.md).
"""

from __future__ import annotations

import json
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Place, Review


@dataclass
class TaskRecord:
    key: str
    saturated: bool
    places: list[Place]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read complete lines; a torn final line from a crash is skipped."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    # A crash can leave a torn final line with no newline; start on a fresh line so
    # the new record is not glued onto it.
    needs_break = False
    if path.exists() and path.stat().st_size:
        with open(path, "rb") as fb:
            fb.seek(-1, 2)
            needs_break = fb.read(1) != b"\n"
    with open(path, "a", encoding="utf-8") as f:
        if needs_break:
            f.write("\n")
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


class RunStore:
    def __init__(self, root: Path | str, run_id: str) -> None:
        self.root = Path(root)
        self.run_id = run_id
        self.dir = self.root / "runs" / run_id
        self._tasks: dict[str, TaskRecord] | None = None

    @classmethod
    def create(cls, root: Path | str, spec: dict[str, Any]) -> "RunStore":
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        run = cls(root, run_id)
        run.dir.mkdir(parents=True)
        (run.dir / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        return run

    @classmethod
    def open(cls, root: Path | str, run_id: str) -> "RunStore":
        run = cls(root, run_id)
        if not (run.dir / "spec.json").exists():
            raise FileNotFoundError(f"no such run: {run_id}")
        return run

    @property
    def spec(self) -> dict[str, Any]:
        return json.loads((self.dir / "spec.json").read_text(encoding="utf-8"))

    def update_spec(self, **changes: Any) -> None:
        spec = self.spec
        spec.update(changes)
        (self.dir / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load(self) -> dict[str, TaskRecord]:
        if self._tasks is None:
            self._tasks = {
                row["key"]: TaskRecord(
                    row["key"], bool(row["saturated"]), [Place.from_dict(p) for p in row["places"]]
                )
                for row in _read_jsonl(self.dir / "tasks.jsonl")
            }
        return self._tasks

    def done(self, key: str) -> TaskRecord | None:
        return self._load().get(key)

    def record(self, key: str, places: list[Place], *, saturated: bool) -> None:
        _append_jsonl(
            self.dir / "tasks.jsonl",
            {"key": key, "saturated": saturated, "places": [p.to_dict() for p in places]},
        )
        self._load()[key] = TaskRecord(key, saturated, [Place.from_dict(p.to_dict()) for p in places])

    def add_reviews(self, place_id: str, reviews: list[Review]) -> None:
        _append_jsonl(
            self.dir / "reviews.jsonl", {"place_id": place_id, "reviews": [r.to_dict() for r in reviews]}
        )

    def places(self) -> dict[str, Place]:
        """All places seen in this run, deduplicated by place ID, with reviews attached."""
        merged: dict[str, Place] = {}
        for rec in self._load().values():
            for p in rec.places:
                copy = Place.from_dict(p.to_dict())
                if copy.place_id in merged:
                    merged[copy.place_id].merge(copy)
                else:
                    merged[copy.place_id] = copy
        for row in _read_jsonl(self.dir / "reviews.jsonl"):
            holder = Place(row["place_id"], reviews=[Review.from_dict(r) for r in row["reviews"]])
            if holder.place_id in merged:
                merged[holder.place_id].merge(holder)
            else:
                merged[holder.place_id] = holder
        return merged


def _age_days(run_dir: Path) -> float:
    return (time.time() - (run_dir / "spec.json").stat().st_mtime) / 86400


def list_runs(root: Path | str) -> list[dict[str, Any]]:
    runs_dir = Path(root) / "runs"
    if not runs_dir.exists():
        return []
    rows = []
    for d in runs_dir.iterdir():
        spec_path = d / "spec.json"
        if not spec_path.exists():
            continue
        rows.append(
            {
                "run_id": d.name,
                "created": spec_path.stat().st_mtime,
                "age_days": round(_age_days(d), 1),
                "spec": json.loads(spec_path.read_text(encoding="utf-8")),
            }
        )
    return sorted(rows, key=lambda r: r["created"], reverse=True)


def purge(root: Path | str, *, older_than_days: float) -> list[str]:
    """Delete runs older than the retention window. Returns the deleted run IDs."""
    deleted = []
    for row in list_runs(root):
        if row["age_days"] > older_than_days:
            shutil.rmtree(Path(root) / "runs" / row["run_id"])
            deleted.append(row["run_id"])
    return deleted
