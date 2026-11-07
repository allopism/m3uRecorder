import os

import pytest

from m3urecorder import create_app

TOKEN = "token-de-test"


@pytest.fixture
def auth_client(settings):
    settings.app_token = TOKEN
    app = create_app(settings, start_background=False)
    for name in ("video.mp4", "otro.mp4"):
        with open(os.path.join(settings.output_dir, name), "wb") as f:
            f.write(b"\x00" * 100)
    return app.test_client()


def test_without_token_everything_is_protected(auth_client):
    assert auth_client.get("/status").status_code == 401
    assert auth_client.get("/history").status_code == 401
    assert auth_client.post("/record").status_code == 401
    r = auth_client.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")


def test_public_endpoints(auth_client):
    assert auth_client.get("/healthz").status_code == 200
    assert auth_client.get("/login").status_code == 200
    assert auth_client.get("/static/css/app.css").status_code == 200


def test_login_flow(auth_client):
    assert auth_client.post("/login", data={"token": "malo"}).status_code == 401
    assert auth_client.post("/login", data={"token": TOKEN}).status_code == 302
    assert auth_client.get("/status").status_code == 200
    assert auth_client.get("/login").status_code == 302          # con sesión, /login lleva a la app
    auth_client.get("/logout")
    assert auth_client.get("/status").status_code == 401


def test_bearer_token(auth_client):
    assert auth_client.get("/status", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert auth_client.get("/status", headers={"Authorization": "Bearer otro"}).status_code == 401


def test_signed_links(auth_client):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    sig = auth_client.get("/api/media-info/video.mp4", headers=headers).get_json()["sig"]
    assert sig
    assert auth_client.get(f"/stream/video.mp4?sig={sig}").status_code == 200
    assert auth_client.get(f"/download/video.mp4?sig={sig}").status_code == 200
    assert auth_client.get(f"/stream/otro.mp4?sig={sig}").status_code == 401   # firma de otro archivo
    assert auth_client.get(f"/status?sig={sig}").status_code == 401            # solo sirve para archivos
    assert auth_client.get("/stream/video.mp4?sig=inventada").status_code == 401
