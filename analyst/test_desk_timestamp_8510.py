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


def test_malformed_double_seconds():
    # Fix for issue where 14:30:30:00 was generated from upstream concatenation
    raw = "2026-11-03T14:30:30:00+05:30"
    out = format_desk_timestamp_ist(raw)
    assert out == "03 Nov 2026, 14:30:30 IST"
    dt = parse_desk_timestamp(raw)
    assert dt is not None
    assert dt.day == 3 and dt.month == 11 and dt.year == 2026
    assert dt.hour == 14 and dt.minute == 30 and dt.second == 30

