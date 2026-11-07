"""Grabaciones reales con ffmpeg contra un servidor HTTP local (se saltan si no hay ffmpeg)."""
import functools
import http.server
import os
import shutil
import subprocess
import threading
import time

import pytest

from m3urecorder import media
from m3urecorder.recorder import RecordingPlan

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg no está instalado")


def _make_ts(path, seconds=12):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:v", "mpeg2video", "-c:a", "mp2", "-f", "mpegts", str(path)], check=True)


@pytest.fixture
def stream_url(tmp_path):
    """Sirve un .ts de prueba por HTTP en un puerto libre."""
    src = tmp_path / "src"
    src.mkdir()
    _make_ts(src / "live.ts")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(src))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/live.ts"
    server.shutdown()


def _wait_idle(recorder, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not recorder.status["running"] and recorder._workers == 0:
            return
        time.sleep(0.3)
    raise AssertionError("la grabación no terminó a tiempo")


@needs_ffmpeg
@pytest.mark.parametrize("fmt", ["mp4", "mkv", "ts"])
def test_immediate_recording(client, services, stream_url, fmt):
    r = client.post("/record", data={"url": stream_url, "duration": "00:00:05",
                                     "filename": "prueba", "format": fmt})
    assert r.status_code == 200, r.get_json()
    _wait_idle(services.recorder)

    entry = services.history.items[-1]
    assert entry["status"] == "ok", entry["status_reason"]
    assert entry["filename"] == f"prueba.{fmt}"
    out = services.settings.output_dir
    assert media.probe_duration(os.path.join(out, entry["filename"])) >= 4
    # Ni temporales ni fragmentos sueltos
    assert sorted(f for f in os.listdir(out) if not f.endswith(".json")) == [f"prueba.{fmt}"]
    assert services.recorder.status["last_result"]["status"] == "ok"


@needs_ffmpeg
def test_capture_slot_is_freed_before_conversion(client, services, stream_url, monkeypatch):
    """
    Mientras una grabación se convierte al formato final, ya puede empezar otra;
    pero la interfaz sigue mostrando "guardando" hasta tener el resultado.
    """
    rec = services.recorder
    converting, release = threading.Event(), threading.Event()
    original = media.finalize_recording

    def slow_finalize(*args, **kwargs):
        converting.set()
        release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(media, "finalize_recording", slow_finalize)
    client.post("/record", data={"url": stream_url, "duration": "00:00:03", "filename": "a", "format": "mp4"})
    assert converting.wait(30)

    assert rec.is_busy() is False                  # el hueco de captura ya está libre...
    assert rec.status["running"] is True           # ...pero la interfaz aún muestra la grabación
    assert "Guardando" in rec.status["progress"]
    assert rec.unique_filename("a", "mp4") == "a_2.mp4"   # el nombre sigue reservado
    assert rec.try_reserve() is True               # otra grabación podría empezar ya
    rec.cancel_reservation()

    release.set()
    _wait_idle(rec)
    assert rec.status["running"] is False
    assert rec.status["last_result"]["status"] == "ok"


@needs_ffmpeg
def test_finalize_joins_segments(tmp_path):
    a, b = tmp_path / "x.mp4.rec.ts", tmp_path / "x.mp4.part2.ts"
    _make_ts(a, 3)
    _make_ts(b, 3)
    final, note = media.finalize_recording([str(a), str(b)], str(tmp_path / "x.mp4"), "mp4",
                                           lambda: str(tmp_path / "x.ts"))
    assert note == "" and final.endswith("x.mp4")
    assert media.probe_duration(final) >= 5
    assert not a.exists() and not b.exists()


@needs_ffmpeg
def test_finalize_falls_back_to_ts(tmp_path):
    """Si la conversión falla, la grabación se conserva en .ts en lugar de perderse."""
    seg = tmp_path / "x.mp4.rec.ts"
    seg.write_bytes(b"esto no es video" * 100)
    final, note = media.finalize_recording([str(seg)], str(tmp_path / "x.mp4"), "mp4",
                                           lambda: str(tmp_path / "x.ts"))
    assert final.endswith("x.ts") and "guardada en .ts" in note
    assert os.path.getsize(final) == 1600


def test_unique_filename_respects_reservations(services):
    rec = services.recorder
    first = rec.unique_filename("show", "mp4", reserve=True)
    assert first == "show.mp4"
    assert rec.unique_filename("show", "mp4") == "show_2.mp4"
    assert rec.is_in_progress("show.mp4")
    rec.release_filename("show.mp4")
    assert rec.unique_filename("show", "mp4") == "show.mp4"


def test_plan_kind():
    assert RecordingPlan(kind="scheduled", url="u", filename="f", format="mp4").scheduled
    assert not RecordingPlan(kind="immediate", url="u", filename="f", format="mp4").scheduled
