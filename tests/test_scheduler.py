from datetime import timedelta

import pytest

from m3urecorder.timeutils import now_local


@pytest.fixture
def started(services, monkeypatch):
    """Sustituye el arranque real de ffmpeg por una lista de planes lanzados."""
    plans = []
    monkeypatch.setattr(services.recorder, "start", plans.append)
    return plans


def _add(services, job_id, start_delta, end_delta):
    now = now_local()
    job = {"id": job_id, "url": "http://e/a", "filename": f"{job_id}.mp4", "format": "mp4",
           "status": "pending", "start_time": (now + start_delta).isoformat(),
           "end_time": (now + end_delta).isoformat()}
    services.schedules.items.append(job)
    return job


def test_due_job_starts(services, started):
    job = _add(services, "a", timedelta(seconds=-1), timedelta(minutes=5))
    services.scheduler.tick()
    assert job["status"] == "running"
    assert len(started) == 1 and started[0].filename == "a.mp4" and started[0].scheduled
    assert services.recorder.is_busy()


def test_future_job_waits(services, started):
    job = _add(services, "a", timedelta(minutes=10), timedelta(minutes=20))
    services.scheduler.tick()
    assert job["status"] == "pending" and started == []


def test_expired_job_fails(services, started):
    job = _add(services, "a", timedelta(minutes=-20), timedelta(minutes=-10))
    services.scheduler.tick()
    assert job["status"] == "failed" and "expirado" in job["status_reason"]


def test_busy_recorder_tolerance(services, started):
    services.recorder.try_reserve()  # otra grabación en curso
    recent = _add(services, "a", timedelta(seconds=-30), timedelta(minutes=5))
    old = _add(services, "b", timedelta(minutes=-5), timedelta(minutes=5))
    services.scheduler.tick()
    assert recent["status"] == "pending"      # dentro del margen: espera a que acabe la otra
    assert old["status"] == "failed"          # fuera del margen: se da por perdida
    assert started == []
