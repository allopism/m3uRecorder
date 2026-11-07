import pytest

from m3urecorder import create_app
from m3urecorder.settings import Settings

SAMPLE_M3U = """#EXTM3U
#EXTINF:-1 tvg-logo="https://example.com/a.png" group-title="News",Canal Noticias
http://example.com/news.m3u8
#EXTINF:-1 group-title="Sports",Deportes HD
https://example.com/sports.m3u8
#EXTINF:-1,Sin Grupo
http://example.com/plain.ts
"""


@pytest.fixture
def settings(tmp_path):
    m3u = tmp_path / "lista.m3u"
    m3u.write_text(SAMPLE_M3U, encoding="utf-8")
    out = tmp_path / "output"
    return Settings(output_dir=str(out), m3u_list_file=str(m3u), vpn_enabled=False,
                    gluetun_servers_dirs=(str(tmp_path / "no-existe"),))


@pytest.fixture
def app(settings):
    return create_app(settings, start_background=False)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def services(app):
    return app.extensions['m3u']
