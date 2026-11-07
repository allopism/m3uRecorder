"""Rutas de la API. El formato de /status es un contrato con la app Android."""
import os
from datetime import timedelta

from m3urecorder.timeutils import now_local

FMT = "%Y-%m-%dT%H:%M:%S"


def test_index_and_static(client):
    r = client.get("/")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert 'id="channelGrid"' in html and 'id="libraryGrid"' in html
    # Los estáticos llevan una versión que cambia con el archivo (sin cachés viejas tras actualizar)
    assert "/static/js/app.js?v=" in html
    assert client.get("/static/js/app.js").status_code == 200


def test_status_contract(client):
    data = client.get("/status").get_json()
    expected = {"running", "pid", "progress", "start_time", "duration_requested", "filename", "mode",
                "vpn_country", "last_result", "server_time", "schedules_pending", "history_count",
                "latest_notification_id", "vpn"}
    assert expected <= set(data)
    assert data["running"] is False
    assert data["vpn"]["enabled"] is False


def test_channels_api(client):
    assert len(client.get("/api/channels").get_json()) == 3
    assert client.get("/api/channels?q=deportes").get_json()[0]["name"] == "Deportes HD"
    assert client.get("/api/channels/groups").get_json() == ["News", "Sports"]


def test_record_validation(client, services):
    def post(**data):
        base = {"url": "http://example.com/a.m3u8", "duration": "00:00:10", "filename": "x", "format": "mp4"}
        base.update(data)
        return client.post("/record", data=base)

    assert post(url="file:///etc/passwd").status_code == 400
    assert post(url="concat:/etc/passwd").status_code == 400
    assert post(format="webm").status_code == 400
    assert post(duration="abc").status_code == 400
    assert services.recorder.is_busy() is False  # los errores no dejan el grabador reservado


def test_record_rejected_while_busy(client, services):
    assert services.recorder.try_reserve()
    r = client.post("/record", data={"url": "http://e/a", "duration": "00:00:10", "filename": "x"})
    assert r.status_code == 400 and "en curso" in r.get_json()["error"]


def test_delete_only_media_files(client, services):
    assert client.post("/history/delete/schedules.json").status_code == 400
    assert client.post("/history/delete/recordings.json").status_code == 400
    video = os.path.join(services.settings.output_dir, "borrame.mp4")
    open(video, "wb").close()
    assert client.post("/history/delete/borrame.mp4").status_code == 200
    assert not os.path.exists(video)


def _schedule(client, start, end, **extra):
    data = {"url": "http://example.com/a.m3u8", "start": start.strftime(FMT), "end": end.strftime(FMT),
            "filename": "prog", "format": "mp4"}
    data.update(extra)
    return client.post("/schedule", data=data)


def test_schedule_lifecycle(client):
    now = now_local().replace(tzinfo=None)
    r = _schedule(client, now + timedelta(hours=1), now + timedelta(hours=2))
    assert r.status_code == 200, r.get_json()
    jobs = client.get("/schedules").get_json()
    assert len(jobs) == 1 and jobs[0]["status"] == "pending"

    # Solapada con la anterior
    assert _schedule(client, now + timedelta(minutes=90), now + timedelta(hours=3)).status_code == 400
    # Borrar una pendiente no se permite; cancelarla sí, y luego borrarla
    job_id = jobs[0]["id"]
    assert client.post(f"/schedules/delete/{job_id}").status_code == 400
    assert client.post(f"/schedules/cancel/{job_id}").status_code == 200
    assert client.post(f"/schedules/delete/{job_id}").status_code == 200
    assert client.get("/schedules").get_json() == []


def test_schedule_validation(client):
    now = now_local().replace(tzinfo=None)
    hour = timedelta(hours=1)
    assert _schedule(client, now - 2 * hour, now - hour).status_code == 400          # ya terminada
    assert _schedule(client, now + 2 * hour, now + hour).status_code == 400          # fin antes del inicio
    assert _schedule(client, now + hour, now + 2 * hour, url="file:///etc/passwd").status_code == 400
    assert _schedule(client, now + hour, now + 2 * hour, format="webm").status_code == 400
    bad_dates = {"url": "http://e/a", "start": "mañana", "end": "luego", "filename": "x"}
    assert client.post("/schedule", data=bad_dates).status_code == 400


def test_notifications_api(client):
    data = client.get("/api/notifications?since_id=0").get_json()
    assert set(data) == {"latest_id", "events"}
    assert client.get("/api/notifications?since_id=abc").status_code == 200
