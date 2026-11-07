"""Proxy de logos: caché, enlace con la lista y protección contra SSRF."""
import pytest

from m3urecorder import logos
from m3urecorder.logos import BlockedURL, LogoCache, check_public_url, logo_id


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/v1/vpn/settings",     # API de control de Gluetun
    "http://localhost/admin",
    "http://192.168.1.1/",                        # router de casa
    "http://10.0.0.5/",
    "http://169.254.169.254/latest/meta-data",    # metadatos de la nube
    "http://[::1]/",
    "file:///etc/passwd",
    "ftp://example.com/logo.png",
])
def test_internal_and_non_http_urls_are_blocked(url):
    with pytest.raises(BlockedURL):
        check_public_url(url)


def test_cached_logo_is_served_without_network(tmp_path):
    cache = LogoCache(str(tmp_path))
    lid = logo_id("https://example.com/a.png")
    (tmp_path / f"{lid}.png").write_bytes(b"\x89PNG")
    path, mime = cache.get(lid, "https://example.com/a.png")
    assert path.endswith(".png") and mime == "image/png"


def test_failed_download_is_not_retried(tmp_path, monkeypatch):
    calls = []

    def fake_check(url):
        calls.append(url)
        raise BlockedURL("bloqueada")

    monkeypatch.setattr(logos, "check_public_url", fake_check)
    cache = LogoCache(str(tmp_path))
    assert cache.get("abc123", "http://example.com/x.png") is None
    assert cache.get("abc123", "http://example.com/x.png") is None
    assert len(calls) == 1          # el segundo intento usa la marca de fallo


def test_channels_expose_logo_ids(client):
    channels = client.get("/api/channels").get_json()
    noticias = next(c for c in channels if c["name"] == "Canal Noticias")
    assert noticias["logo_id"] == logo_id("https://example.com/a.png")
    assert next(c for c in channels if c["name"] == "Deportes HD")["logo_id"] == ""


def test_logo_route_only_serves_known_ids(client, services):
    # Un id que no está en la lista nunca provoca una descarga
    assert client.get("/logo/0123456789abcdef0123").status_code == 404
    assert client.get("/logo/..%2F..%2Fetc").status_code == 404
    # Un logo de la lista ya en caché se sirve, aislado por si es un SVG con scripts
    lid = logo_id("https://example.com/a.png")
    with open(f"{services.logos.cache_dir}/{lid}.png", "wb") as f:
        f.write(b"\x89PNG")
    r = client.get(f"/logo/{lid}")
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert "sandbox" in r.headers["Content-Security-Policy"]


def test_schedule_records_channel(client):
    from datetime import timedelta

    from m3urecorder.timeutils import now_local
    now = now_local().replace(tzinfo=None)
    client.post("/schedule", data={
        "url": "http://example.com/news.m3u8", "filename": "n", "format": "mp4",
        "start": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),
        "end": (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")})
    job = client.get("/schedules").get_json()[0]
    assert job["channel"] == "Canal Noticias" and job["logo_id"]
