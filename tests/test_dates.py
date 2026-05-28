from datetime import UTC, datetime

from optimizarr.dates import age_days, parse_iso


def test_parse_iso_handles_z_suffix():
    parsed = parse_iso("2026-04-07T12:00:00Z")
    assert parsed == datetime(2026, 4, 7, 12, 0, 0, tzinfo=UTC)


def test_parse_iso_handles_offset():
    parsed = parse_iso("2026-04-07T12:00:00+00:00")
    assert parsed == datetime(2026, 4, 7, 12, 0, 0, tzinfo=UTC)


def test_parse_iso_assumes_utc_for_naive_input():
    parsed = parse_iso("2026-04-07T12:00:00")
    assert parsed is not None
    assert parsed.tzinfo == UTC


def test_parse_iso_returns_none_for_empty():
    assert parse_iso(None) is None
    assert parse_iso("") is None


def test_age_days_basic():
    now = datetime(2026, 5, 22, tzinfo=UTC)
    assert age_days("2026-04-22T00:00:00Z", now) == 30.0


def test_age_days_fractional():
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    age = age_days("2026-05-22T00:00:00Z", now)
    assert age == 0.5


def test_age_days_returns_none_for_missing_value():
    now = datetime(2026, 5, 22, tzinfo=UTC)
    assert age_days(None, now) is None
    assert age_days("", now) is None
