import os
import time

from maps.models import Hit, Place, Review
from maps.store import RunStore, list_runs, purge


def place(pid, **kw):
    return Place(pid, name=kw.pop("name", pid), hits=[Hit("sim", "bakery", rank=1)], **kw)


def test_create_writes_spec_and_open_reads_it_back(tmp_path):
    run = RunStore.create(tmp_path, {"queries": ["bakery"], "area": "1,2,3,4"})
    assert RunStore.open(tmp_path, run.run_id).spec["queries"] == ["bakery"]


def test_recorded_task_is_reported_done_with_its_saturation(tmp_path):
    run = RunStore.create(tmp_path, {})
    assert run.done("k1") is None
    run.record("k1", [place("p1")], saturated=True)
    reopened = RunStore.open(tmp_path, run.run_id)
    rec = reopened.done("k1")
    assert rec is not None and rec.saturated is True
    assert [p.place_id for p in rec.places] == ["p1"]


def test_places_merges_sightings_of_the_same_place_across_tasks(tmp_path):
    run = RunStore.create(tmp_path, {})
    run.record("k1", [place("p1", rating=4.5)], saturated=False)
    run.record("k2", [place("p1"), place("p2")], saturated=False)
    places = RunStore.open(tmp_path, run.run_id).places()
    assert set(places) == {"p1", "p2"}
    assert len(places["p1"].hits) == 2
    assert places["p1"].rating == 4.5


def test_reviews_are_attached_to_their_place_on_reload(tmp_path):
    run = RunStore.create(tmp_path, {})
    run.record("k1", [place("p1")], saturated=False)
    run.add_reviews("p1", [Review("p1", text="flaky croissant", rating=5, source="sim")])
    places = RunStore.open(tmp_path, run.run_id).places()
    assert [r.text for r in places["p1"].reviews] == ["flaky croissant"]


def test_a_torn_final_line_from_a_crash_is_ignored(tmp_path):
    run = RunStore.create(tmp_path, {})
    run.record("k1", [place("p1")], saturated=False)
    with open(run.dir / "tasks.jsonl", "a") as f:
        f.write('{"key": "k2", "saturated": false, "places": [{"place_')
    reopened = RunStore.open(tmp_path, run.run_id)
    assert reopened.done("k1") is not None
    assert reopened.done("k2") is None


def test_list_runs_is_newest_first(tmp_path):
    a = RunStore.create(tmp_path, {"label": "a"})
    os.utime(a.dir / "spec.json", (time.time() - 100, time.time() - 100))
    b = RunStore.create(tmp_path, {"label": "b"})
    assert [r["run_id"] for r in list_runs(tmp_path)] == [b.run_id, a.run_id]


def test_purge_deletes_only_runs_older_than_the_retention_window(tmp_path):
    old = RunStore.create(tmp_path, {})
    stale = time.time() - 31 * 86400
    os.utime(old.dir / "spec.json", (stale, stale))
    fresh = RunStore.create(tmp_path, {})
    assert purge(tmp_path, older_than_days=30) == [old.run_id]
    assert not old.dir.exists() and fresh.dir.exists()


def test_run_ids_are_unique_within_the_same_second(tmp_path):
    assert RunStore.create(tmp_path, {}).run_id != RunStore.create(tmp_path, {}).run_id
