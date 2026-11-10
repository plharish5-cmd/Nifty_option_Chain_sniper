from analyst.desk_timestamp_8510 import format_desk_timestamp_ist, parse_desk_timestamp


def test_iso_offset_to_readable():
    assert format_desk_timestamp_ist("2026-11-09T14:00:00+05:30") == "09 Nov 2026, 14:00:00 IST"


def test_db_style_space():
    out = format_desk_timestamp_ist("2026-11-09 14:00:00 IST")
    assert out == "09 Nov 2026, 14:00:00 IST"


def test_idempotent_readable():
    formatted = "09 Nov 2026, 14:00:00 IST"
    assert format_desk_timestamp_ist(formatted) == formatted


def test_parse_roundtrip():
    dt = parse_desk_timestamp("2026-11-09T14:00:00+05:30")
    assert dt is not None
    assert dt.hour == 14 and dt.minute == 0
