from datetime import timedelta

from m3urecorder import timeutils as tu


def setup_module():
    tu.set_timezone("Europe/Madrid")


def test_offsets_winter_and_summer():
    assert tu.parse_local("2026-01-15T12:00").utcoffset() == timedelta(hours=1)
    assert tu.parse_local("2026-07-15T12:00").utcoffset() == timedelta(hours=2)


def test_repeated_hour_in_october_uses_fold():
    # 25/10/2026: de 02:00 a 03:00 ocurre dos veces
    first = tu.parse_local("2026-10-25T02:30", fold=0)
    second = tu.parse_local("2026-10-25T02:30", fold=1)
    assert first.utcoffset() == timedelta(hours=2)
    assert second.utcoffset() == timedelta(hours=1)
    assert tu.seconds_between(first, second) == 3600


def test_real_duration_across_dst_changes():
    # La noche del cambio de octubre dura una hora más; la de marzo, una menos
    assert tu.seconds_between("2026-10-25T00:00", "2026-10-25T04:00") == 5 * 3600
    assert tu.seconds_between("2026-03-29T00:00", "2026-03-29T04:00") == 3 * 3600


def test_offset_in_input_is_respected():
    dt = tu.parse_local("2026-07-15T10:00:00+00:00")
    assert dt.hour == 12 and dt.utcoffset() == timedelta(hours=2)


def test_other_timezones_work():
    tu.set_timezone("America/New_York")
    try:
        assert tu.parse_local("2026-07-15T12:00").utcoffset() == timedelta(hours=-4)
    finally:
        tu.set_timezone("Europe/Madrid")
