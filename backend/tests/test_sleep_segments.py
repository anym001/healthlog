"""Unaggregated sleep (HAE "Aggregate Data" off): stage segments folded into nights."""

from __future__ import annotations

import datetime as dt
import hashlib
import json

import pytest
from sqlalchemy import func, select

from app.ingest import archive_raw, fold_sleep_segments, parse_payload, store
from app.models import SleepSession
from app.rederive import run_rederive_sleep

WATCH = "Apple Watch"
PHONE = "iPhone"


def _seg(stage: str, start: str, end: str, source: str = WATCH) -> dict:
    return {
        "value": stage,
        "startDate": f"2026-10-{start} +0200",
        "endDate": f"2026-10-{end} +0200",
        "qty": 0.0,  # ignored: durations come from the timestamps
        "source": source,
    }


def _payload(*points: dict) -> dict:
    return {"data": {"metrics": [{"name": "sleep_analysis", "units": "hr", "data": list(points)}]}}


# A night 23:00 -> 06:30 local, with one awake segment and an iPhone in-bed window.
NIGHT = (
    _seg("In Bed", "01 22:45:00", "02 06:45:00", PHONE),
    _seg("Core", "01 23:00:00", "02 01:00:00"),
    _seg("Deep", "02 01:00:00", "02 02:00:00"),
    _seg("Awake", "02 02:00:00", "02 02:15:00"),
    _seg("REM", "02 02:15:00", "02 03:15:00"),
    _seg("Core", "02 03:15:00", "02 06:30:00"),
)


def test_night_folds_into_one_aggregated_row():
    parsed = parse_payload(_payload(*NIGHT))
    assert parsed.dropped_sleep == []
    assert len(parsed.sleep_rows) == 1
    row = parsed.sleep_rows[0]

    tz = dt.timezone(dt.timedelta(hours=2))
    assert row["sleep_start"] == dt.datetime(2026, 10, 1, 23, 0, tzinfo=tz)
    assert row["sleep_end"] == dt.datetime(2026, 10, 2, 6, 30, tzinfo=tz)
    assert row["in_bed_start"] == dt.datetime(2026, 10, 1, 22, 45, tzinfo=tz)
    assert row["in_bed_end"] == dt.datetime(2026, 10, 2, 6, 45, tzinfo=tz)
    assert row["sleep_date"] == dt.date(2026, 10, 2)  # local wake-up day
    assert row["source"] == WATCH  # the stage source, not the in-bed-only phone
    assert row["core_h"] == pytest.approx(5.25)
    assert row["deep_h"] == pytest.approx(1.0)
    assert row["rem_h"] == pytest.approx(1.0)
    assert row["awake_h"] == pytest.approx(0.25)
    assert row["asleep_h"] is None  # no uncategorised sleep -> unknown, not 0
    assert row["total_sleep_h"] == pytest.approx(7.25)
    assert row["in_bed_h"] == pytest.approx(8.0)


def test_wake_day_uses_the_segment_offset_not_utc():
    # Waking at 00:30 local (+0200) is still 22:30 on the previous day in UTC.
    local = dt.timezone(dt.timedelta(hours=2))
    segment = {
        "start": dt.datetime(2026, 10, 1, 21, 0, tzinfo=local),
        "end": dt.datetime(2026, 10, 2, 0, 30, tzinfo=local),
        "column": "core_h",
        "source": WATCH,
    }
    rows, dropped = fold_sleep_segments([segment])
    assert dropped == 0
    assert rows[0]["sleep_date"] == dt.date(2026, 10, 2)


def test_a_long_gap_starts_a_new_period():
    nap = _seg("Asleep", "02 14:00:00", "02 14:40:00")
    rows = parse_payload(_payload(*NIGHT, nap)).sleep_rows
    assert len(rows) == 2
    nap_row = max(rows, key=lambda r: r["sleep_start"])
    assert nap_row["asleep_h"] == pytest.approx(40 / 60)
    assert nap_row["total_sleep_h"] == pytest.approx(40 / 60)
    assert nap_row["in_bed_start"] is None


def test_a_short_gap_stays_in_the_same_night():
    late = _seg("Core", "02 08:00:00", "02 08:30:00")  # 1.5 h after 06:30
    rows = parse_payload(_payload(*NIGHT, late)).sleep_rows
    assert len(rows) == 1
    assert rows[0]["total_sleep_h"] == pytest.approx(7.75)


def test_two_stage_sources_do_not_double_count():
    other = (
        _seg("Asleep", "01 23:30:00", "02 05:30:00", "Sleep App"),  # 6 h, less than the watch
    )
    row = parse_payload(_payload(*NIGHT, *other)).sleep_rows[0]
    assert row["source"] == WATCH
    assert row["total_sleep_h"] == pytest.approx(7.25)
    assert row["asleep_h"] is None


def test_unusable_segments_and_stage_less_periods_are_counted():
    parsed = parse_payload(
        _payload(
            _seg("Dreaming", "02 01:00:00", "02 02:00:00"),  # unknown stage
            {"value": "Core", "startDate": "garbage", "endDate": "2026-10-02 02:00:00 +0200"},
            _seg("Core", "02 03:00:00", "02 02:00:00"),  # ends before it starts
            _seg("In Bed", "05 22:00:00", "06 06:00:00", PHONE),  # a period with no sleep stage
        )
    )
    assert parsed.sleep_rows == []
    reasons = sorted(r for r, _ in parsed.dropped_sleep)
    assert reasons == ["no asleep stage in period"] + ["unusable segment"] * 3


def test_aggregated_point_without_sleep_start_is_counted():
    parsed = parse_payload(_payload({"date": "2026-10-02 00:00:00 +0200", "totalSleep": 7.0}))
    assert parsed.sleep_rows == []
    assert parsed.dropped_sleep == [("no sleepStart", "")]


def test_aggregated_shape_is_unchanged(sample_payload):
    rows = parse_payload(sample_payload).sleep_rows
    assert len(rows) == 1 and rows[0]["sleep_start"] is not None


def test_folding_is_deterministic():
    shuffled = tuple(reversed(NIGHT))
    assert parse_payload(_payload(*NIGHT)).sleep_rows == parse_payload(_payload(*shuffled)).sleep_rows


# --- DB: store + re-derive ---


def _count(db) -> int:
    return db.execute(select(func.count()).select_from(SleepSession)).scalar_one()


def test_store_and_replay_are_idempotent(db):
    first = store(db, parse_payload(_payload(*NIGHT)))
    assert (first.sleep_rows, first.sleep_new, first.dropped_sleep) == (1, 1, 0)
    store(db, parse_payload(_payload(*NIGHT)))
    assert _count(db) == 1


def test_rederive_sleep_backfills_archived_segments(db):
    payload = _payload(*NIGHT)
    archive_raw(db, payload, hashlib.sha256(json.dumps(payload).encode()).digest(), None)
    db.flush()
    assert _count(db) == 0

    summary = run_rederive_sleep(db)
    assert (summary.payloads, summary.samples, summary.failures) == (1, 1, 0)
    assert _count(db) == 1
    assert run_rederive_sleep(db, dry_run=True).samples == 1
    run_rederive_sleep(db)
    assert _count(db) == 1  # re-run is a no-op
