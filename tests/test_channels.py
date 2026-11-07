from m3urecorder.channels import ChannelList, parse_m3u

from .conftest import SAMPLE_M3U


def test_parse_m3u():
    channels = parse_m3u(SAMPLE_M3U)
    assert [c["name"] for c in channels] == ["Canal Noticias", "Deportes HD", "Sin Grupo"]
    assert channels[0]["group"] == "News"
    assert channels[0]["logo"] == "https://example.com/a.png"
    assert channels[2]["group"] == ""


def test_search(tmp_path):
    path = tmp_path / "l.m3u"
    path.write_text(SAMPLE_M3U, encoding="utf-8")
    cl = ChannelList(str(path))
    assert cl.load() == 3
    assert cl.groups == ["News", "Sports"]
    assert [c["name"] for c in cl.search("deportes")] == ["Deportes HD"]
    assert [c["name"] for c in cl.search(group="news")] == ["Canal Noticias"]


def test_missing_file(tmp_path):
    cl = ChannelList(str(tmp_path / "nada.m3u"))
    assert cl.load() == 0 and cl.channels == []
