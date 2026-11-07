import pytest

from m3urecorder.utils import hhmmss_to_seconds, human_size, is_allowed_stream_url, sanitize_basename, seconds_to_hhmmss


@pytest.mark.parametrize("text,expected", [
    ("01:30:00", 5400), ("00:00:05", 5), ("10:00", 600), ("45", 45),
    ("", 0), ("abc", 0), ("1:2:3:4", 0),
])
def test_hhmmss_to_seconds(text, expected):
    assert hhmmss_to_seconds(text) == expected


def test_seconds_to_hhmmss():
    assert seconds_to_hhmmss(5400) == "01:30:00"
    assert seconds_to_hhmmss(-5) == "00:00:00"


def test_human_size():
    assert human_size(0) == "0.00 B"
    assert human_size(1536) == "1.50 KB"
    assert human_size("x") == "0 B"


@pytest.mark.parametrize("name,expected", [
    ("Grey's Anatomy", "Grey's Anatomy"),       # el apóstrofo es válido
    ("../../etc/passwd", "passwd"),             # sin carpetas
    ('a<b>c:d"e|f?g*h', "a_b_c_d_e_f_g_h"),     # caracteres prohibidos
    ("video.mp4", "video"),                     # sin la extensión
])
def test_sanitize_basename(name, expected):
    assert sanitize_basename(name, "mp4") == expected


def test_sanitize_basename_empty_gets_default():
    assert sanitize_basename("  ", "mp4").startswith("grabacion_")


@pytest.mark.parametrize("url,ok", [
    ("http://x/a.m3u8", True), ("HTTPS://x/a", True), ("rtmp://x/live", True),
    ("udp://239.0.0.1:1234", True), ("srt://x:9000", True),
    ("file:///etc/passwd", False), ("concat:/etc/passwd", False),
    ("/etc/passwd", False), ("", False),
])
def test_is_allowed_stream_url(url, ok):
    assert is_allowed_stream_url(url) is ok
