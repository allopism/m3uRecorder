import json
from datetime import timedelta

from m3urecorder.storage import NotificationStore, ScheduleStore
from m3urecorder.timeutils import now_local


def test_schedule_load_repairs_state(tmp_path):
    path = tmp_path / "schedules.json"
    path.write_text(json.dumps([
        {"id": "a", "status": "running", "start_time": "2026-01-10T10:00", "end_time": "2026-01-10T11:00",
         "_pre_switched_done": True},
    ]), encoding="utf-8")
    store = ScheduleStore(str(path))
    store.load()
    job = store.get("a")
    assert job["status"] == "failed"                    # la app se reinició grabando
    assert job["start_time"].endswith("+01:00")         # se le añade la zona horaria
    assert "_pre_switched_done" not in job              # datos internos fuera
    assert json.loads(path.read_text(encoding="utf-8"))[0]["status"] == "failed"


def test_corrupt_file_starts_empty(tmp_path):
    path = tmp_path / "schedules.json"
    path.write_text("{no es json", encoding="utf-8")
    store = ScheduleStore(str(path))
    store.load()
    assert store.items == []


def _job(job_id, start, end, status="pending"):
    return {"id": job_id, "status": status, "start_time": start.isoformat(), "end_time": end.isoformat()}


def test_overlap_and_next_due(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.json"))
    now = now_local()
    a = _job("a", now + timedelta(hours=1), now + timedelta(hours=2))
    b = _job("b", now + timedelta(seconds=1), now + timedelta(minutes=5))
    store.items = [a, b]
    assert store.find_overlap(now + timedelta(minutes=90), now + timedelta(hours=3)) is a
    assert store.find_overlap(now + timedelta(hours=2), now + timedelta(hours=3)) is None  # contiguas: OK
    assert store.next_due() is b
    assert store.next_due(exclude=b) is None


def test_notifications_ids_and_limit(tmp_path):
    store = NotificationStore(str(tmp_path / "n.json"))
    for i in range(130):
        store.emit("t", f"b{i}")
    assert len(store.items) == 120
    assert store.latest_id() == 130
    assert [n["id"] for n in store.since(128)] == [129, 130]
