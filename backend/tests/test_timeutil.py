"""Date parsing + local-day bucketing (the basis of the daily grid)."""

from __future__ import annotations

import datetime as dt

from app.timeutil import local_day, offset_mismatches, parse_hae_datetime


def test_parse_hae_datetime_keeps_offset():
    ts = parse_hae_datetime("2026-06-09 00:00:00 +0200")
    assert ts == dt.datetime(2026, 6, 9, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))


def test_parse_none_and_empty():
    assert parse_hae_datetime(None) is None
    assert parse_hae_datetime("") is None


def test_parse_malformed_string_returns_none():
    # One bad sample must degrade to a skipped point, never abort a payload.
    assert parse_hae_datetime("garbage") is None
    assert parse_hae_datetime("2026-06-09") is None  # missing time + offset


def test_parse_iso_8601_with_offset():
    # stateOfMind ships ISO 8601 instead of HAE's usual format.
    assert parse_hae_datetime("2026-06-15T10:28:00Z") == dt.datetime(2026, 6, 15, 10, 28, tzinfo=dt.UTC)
    assert parse_hae_datetime("2026-06-15T12:28:00+02:00").utcoffset() == dt.timedelta(hours=2)
    assert parse_hae_datetime("2026-06-15T10:28:00") is None  # no offset: rejected, not guessed


def test_parse_non_string_returns_none():
    assert parse_hae_datetime({"qty": 1}) is None
    assert parse_hae_datetime(12345) is None


def test_local_day_crossing_midnight_utc():
    # 23:30 UTC on Jan 1 is 00:30 on Jan 2 in Europe/Vienna (UTC+1 in winter).
    ts = dt.datetime(2026, 1, 1, 23, 30, tzinfo=dt.UTC)
    assert local_day(ts, "Europe/Vienna") == dt.date(2026, 1, 2)


def test_local_day_summer_offset():
    # 22:30 UTC in summer is 00:30 next day in Vienna (UTC+2).
    ts = dt.datetime(2026, 6, 9, 22, 30, tzinfo=dt.UTC)
    assert local_day(ts, "Europe/Vienna") == dt.date(2026, 6, 10)


def test_offset_mismatches_flags_stamps_outside_the_configured_zone():
    ny_summer = parse_hae_datetime("2026-10-14 09:00:00 -0400")
    vienna_summer = parse_hae_datetime("2026-07-01 09:00:00 +0200")
    assert offset_mismatches([ny_summer, vienna_summer], "Europe/Vienna") == [ny_summer]
    assert offset_mismatches([ny_summer], "America/New_York") == []


def test_offset_mismatches_is_dst_aware():
    # New York is -0500 in winter and -0400 in summer: both match the zone.
    winter = parse_hae_datetime("2026-01-15 12:00:00 -0500")
    summer = parse_hae_datetime("2026-07-15 12:00:00 -0400")
    assert offset_mismatches([winter, summer], "America/New_York") == []
    # A winter stamp carrying the summer offset does not.
    wrong = parse_hae_datetime("2026-01-15 12:00:00 -0400")
    assert offset_mismatches([wrong], "America/New_York") == [wrong]


def test_offset_mismatches_ignores_naive_timestamps():
    assert offset_mismatches([dt.datetime(2026, 1, 1, 12, 0)], "Europe/Vienna") == []
