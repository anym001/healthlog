"""Parser: HAE payload -> normalised rows (pure, no DB)."""

from __future__ import annotations

import datetime as dt
import uuid

from app.ingest import _consolidate_sleep, parse_payload


def test_heart_rate_parsed_as_minmax(sample_payload):
    parsed = parse_payload(sample_payload)
    hr = [r for r in parsed.metric_rows if r["metric"] == "heart_rate"]
    assert len(hr) == 2
    first = hr[0]
    assert first["vmin"] == 50
    assert first["vavg"] == 58.5
    assert first["vmax"] == 92
    assert first["qty"] is None
    assert first["unit"] == "count/min"


def test_qty_metric_and_energy_conversion(sample_payload):
    parsed = parse_payload(sample_payload)
    energy = [r for r in parsed.metric_rows if r["metric"] == "active_energy"][0]
    # 418.4 kJ -> 100 kcal
    assert energy["unit"] == "kcal"
    assert abs(energy["qty"] - 100.0) < 1e-6


def test_compound_and_empty_source_preserved(sample_payload):
    parsed = parse_payload(sample_payload)
    steps = [r for r in parsed.metric_rows if r["metric"] == "step_count"]
    sources = {r["source"] for r in steps}
    assert "Apple Watch|iPhone" in sources
    assert "" in sources  # empty source tolerated, never None
    assert all(r["source"] is not None for r in steps)


def test_unknown_metric_flagged_for_registration(sample_payload):
    parsed = parse_payload(sample_payload)
    assert "future_unknown_metric" in parsed.unknown_metrics
    assert parsed.unknown_metrics["future_unknown_metric"] == "widgets"


def _metric_payload(name, points, units="count/min"):
    return {"data": {"metrics": [{"name": name, "units": units, "data": points}]}}


def test_implausible_qty_value_is_dropped_and_recorded():
    # step_count is non-negative; a negative reading is garbage and must not
    # enter the series the analysis runs on (the raw archive still keeps it).
    payload = _metric_payload(
        "step_count",
        [{"date": "2026-06-15 10:00:00 +0000", "qty": -42}],
        units="count",
    )
    parsed = parse_payload(payload)
    assert not parsed.metric_rows  # dropped
    assert parsed.implausible == [("step_count", -42.0)]


def test_implausible_minmax_value_is_dropped():
    # heart_rate arrives as {Min,Avg,Max}; an Avg of 0 (< the 20 bpm floor) is a
    # spurious bucket and the whole row is dropped on the representative value.
    payload = _metric_payload(
        "heart_rate",
        [{"date": "2026-06-15 10:00:00 +0000", "Min": 0, "Avg": 0, "Max": 0}],
    )
    parsed = parse_payload(payload)
    assert not parsed.metric_rows
    assert parsed.implausible == [("heart_rate", 0.0)]


def test_plausible_value_is_kept():
    payload = _metric_payload(
        "heart_rate",
        [{"date": "2026-06-15 10:00:00 +0000", "Min": 50, "Avg": 60, "Max": 92}],
    )
    parsed = parse_payload(payload)
    assert len(parsed.metric_rows) == 1
    assert parsed.implausible == []


def test_unknown_metric_value_is_not_bounds_checked():
    # An unknown metric has no envelope, so even an odd value is kept (and the
    # metric is queued for auto-registration).
    payload = _metric_payload(
        "future_unknown_metric",
        [{"date": "2026-06-15 10:00:00 +0000", "qty": -999}],
        units="widgets",
    )
    parsed = parse_payload(payload)
    assert len(parsed.metric_rows) == 1
    assert parsed.implausible == []
    assert "future_unknown_metric" in parsed.unknown_metrics


def test_flagged_unit_value_skips_the_bounds_check():
    # A value whose unit deviated with no known conversion is kept as-is and only
    # flagged: bounds are canonical, so an unconverted value is never dropped on
    # them (it would be a false positive against the wrong unit).
    payload = _metric_payload(
        "heart_rate",
        [{"date": "2026-06-15 10:00:00 +0000", "Min": 0, "Avg": 0, "Max": 0}],
        units="bogus_unit",
    )
    parsed = parse_payload(payload)
    assert len(parsed.metric_rows) == 1
    assert parsed.implausible == []
    assert parsed.flagged_units == [("heart_rate", "bogus_unit")]


def test_malformed_timestamp_skips_the_point_not_the_payload():
    # A single bad date (wrong format or even a non-string) among good points
    # must cost exactly that point — the rest of the payload still parses.
    payload = _metric_payload(
        "step_count",
        [
            {"date": "garbage", "qty": 100},
            {"date": {"nested": "junk"}, "qty": 200},
            {"date": "2026-06-15 10:00:00 +0000", "qty": 300},
        ],
        units="count",
    )
    parsed = parse_payload(payload)
    assert [r["qty"] for r in parsed.metric_rows] == [300.0]


def test_malformed_sleep_start_skips_the_row():
    payload = {
        "data": {
            "metrics": [
                {
                    "name": "sleep_analysis",
                    "units": "hr",
                    "data": [
                        {"sleepStart": "not a date", "totalSleep": 7.0},
                        {"sleepStart": "2026-06-15 23:00:00 +0200", "totalSleep": 6.5},
                    ],
                }
            ]
        }
    }
    parsed = parse_payload(payload)
    assert len(parsed.sleep_rows) == 1
    assert parsed.sleep_rows[0]["total_sleep_h"] == 6.5


def test_workout_malformed_times_tolerated_as_null():
    payload = {
        "data": {
            "workouts": [
                {
                    "id": "3213AD95-044D-4777-9D99-B473968262F1",
                    "start": "junk",
                    "end": "2026-06-15 11:00:00 +0200",
                    "name": "Outdoor Run",
                }
            ]
        }
    }
    parsed = parse_payload(payload)
    assert len(parsed.workout_rows) == 1
    w = parsed.workout_rows[0]
    assert w["start_time"] is None  # tolerated, column is nullable
    assert w["end_time"] is not None


def test_sleep_routed_to_sleep_rows_with_wake_day(sample_payload):
    parsed = parse_payload(sample_payload)
    assert not any(r["metric"] == "sleep_analysis" for r in parsed.metric_rows)
    assert len(parsed.sleep_rows) == 1
    s = parsed.sleep_rows[0]
    assert s["sleep_date"] == dt.date(2026, 1, 2)  # wake-up day
    assert s["sleep_start"].hour == 22  # previous evening
    assert s["total_sleep_h"] == 7.5
    assert abs((s["deep_h"] + s["core_h"] + s["rem_h"]) - s["total_sleep_h"]) < 1e-6


def test_consolidate_sleep_keeps_fullest_per_awakening():
    end = dt.datetime(2026, 6, 22, 6, 11, tzinfo=dt.UTC)
    nap_end = dt.datetime(2026, 6, 22, 14, 40, tzinfo=dt.UTC)
    rows = [
        {"sleep_start": 1, "sleep_end": end, "source": "w", "total_sleep_h": 5.0},
        {"sleep_start": 2, "sleep_end": end, "source": "w", "total_sleep_h": 8.5},  # fullest
        {"sleep_start": 3, "sleep_end": end, "source": "w", "total_sleep_h": 3.0},
        {"sleep_start": 4, "sleep_end": nap_end, "source": "w", "total_sleep_h": 0.6},  # distinct
    ]
    out = _consolidate_sleep(rows)
    by_end = {r["sleep_end"]: r for r in out}
    assert len(out) == 2  # one night + one nap, not four
    assert by_end[end]["total_sleep_h"] == 8.5
    assert by_end[nap_end]["total_sleep_h"] == 0.6


def test_consolidate_sleep_buckets_null_end_per_source():
    rows = [
        {"sleep_start": 1, "sleep_end": None, "source": "w", "total_sleep_h": 4.0},
        {"sleep_start": 2, "sleep_end": None, "source": "w", "total_sleep_h": 7.0},  # fullest
        {"sleep_start": 3, "sleep_end": None, "source": "phone", "total_sleep_h": 6.0},
    ]
    out = _consolidate_sleep(rows)
    assert len(out) == 2  # one per source (NULL ends share a bucket)
    assert {r["source"]: r["total_sleep_h"] for r in out} == {"w": 7.0, "phone": 6.0}


def test_workout_uses_uuid_and_converts_energy(sample_payload):
    parsed = parse_payload(sample_payload)
    assert len(parsed.workout_rows) == 1
    w = parsed.workout_rows[0]
    assert w["hae_id"] == uuid.UUID("3213AD95-044D-4777-9D99-B473968262F1")
    assert w["avg_hr"] == 112
    assert w["max_hr"] == 130
    assert abs(w["total_energy_kcal"] - 836.8 * 0.2390057361) < 1e-6
    assert w["distance_km"] == 2.4
    assert w["is_indoor"] is False
    # The summary-only fixture carries no intra-workout HR series.
    assert parsed.workout_hr_rows == []


def test_workout_heart_rate_series_parsed_to_samples():
    wid = "3213AD95-044D-4777-9D99-B473968262F1"
    payload = {
        "data": {
            "workouts": [
                {
                    "id": wid,
                    "name": "Outdoor Run",
                    "start": "2026-06-15 12:28:00 +0200",
                    "end": "2026-06-15 13:00:00 +0200",
                    # HAE ships per-minute buckets {Min, Avg, Max} with a timestamp.
                    "heartRateData": [
                        {
                            "date": "2026-06-15 12:28:21 +0200",
                            "Min": 100,
                            "Avg": 104.5,
                            "Max": 109,
                            "units": "count/min",
                        },
                        {
                            "date": "2026-06-15 12:29:21 +0200",
                            "Min": 109,
                            "Avg": 111.75,
                            "Max": 114,
                            "units": "count/min",
                        },
                        {"date": "bad-timestamp", "Avg": 120},  # unparseable -> dropped
                        {"Avg": 130},  # no timestamp -> dropped
                    ],
                }
            ]
        }
    }
    parsed = parse_payload(payload)
    assert len(parsed.workout_hr_rows) == 2  # only the two timed, valued samples
    first = parsed.workout_hr_rows[0]
    assert first["workout_hae_id"] == uuid.UUID(wid)
    assert first["bpm"] == 104.5  # the Avg of the bucket
    assert first["ts"] == dt.datetime(2026, 6, 15, 12, 28, 21, tzinfo=dt.timezone(dt.timedelta(hours=2)))


def test_workout_route_parsed_to_points_v2_and_v1():
    wid = "3213AD95-044D-4777-9D99-B473968262F1"
    payload = {
        "data": {
            "workouts": [
                {
                    "id": wid,
                    "name": "Outdoor Run",
                    "start": "2026-06-15 12:28:00 +0200",
                    "end": "2026-06-15 13:00:00 +0200",
                    "route": [
                        # v2 shape: latitude/longitude + altitude/speed
                        {
                            "latitude": 48.2082,
                            "longitude": 16.3738,
                            "altitude": 171.0,
                            "speed": 3.1,
                            "timestamp": "2026-06-15 12:28:21 +0200",
                        },
                        # v1 shape: abbreviated lat/lon, no speed
                        {
                            "lat": 48.2090,
                            "lon": 16.3750,
                            "altitude": 172.5,
                            "timestamp": "2026-06-15 12:29:21 +0200",
                        },
                        {"latitude": 48.21, "timestamp": "2026-06-15 12:30:00 +0200"},  # no lon -> dropped
                        {"lat": 48.21, "lon": 16.38, "timestamp": "bad-timestamp"},  # unparseable -> dropped
                        {"lat": 48.21, "lon": 16.38},  # no timestamp -> dropped
                    ],
                }
            ]
        }
    }
    parsed = parse_payload(payload)
    assert len(parsed.workout_route_rows) == 2  # only the two timed, coordinate-bearing points
    first = parsed.workout_route_rows[0]
    assert first["workout_hae_id"] == uuid.UUID(wid)
    assert (first["lat"], first["lon"]) == (48.2082, 16.3738)
    assert first["altitude_m"] == 171.0
    assert first["speed_mps"] == 3.1
    assert first["ts"] == dt.datetime(2026, 6, 15, 12, 28, 21, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    # v1 point carries no speed.
    assert parsed.workout_route_rows[1]["speed_mps"] is None
    assert (parsed.workout_route_rows[1]["lat"], parsed.workout_route_rows[1]["lon"]) == (48.2090, 16.3750)


def test_summary_only_workout_has_no_route():
    payload = {
        "data": {
            "workouts": [
                {
                    "id": "3213AD95-044D-4777-9D99-B473968262F1",
                    "name": "Indoor Cycle",
                    "start": "2026-06-15 12:28:00 +0200",
                    "end": "2026-06-15 13:00:00 +0200",
                }
            ]
        }
    }
    assert parse_payload(payload).workout_route_rows == []
