"""Parser: HAE payload -> normalised rows (pure, no DB)."""

from __future__ import annotations

import datetime as dt
import uuid

from app.ingest import parse_payload


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


def test_sleep_routed_to_sleep_rows_with_wake_day(sample_payload):
    parsed = parse_payload(sample_payload)
    assert not any(r["metric"] == "sleep_analysis" for r in parsed.metric_rows)
    assert len(parsed.sleep_rows) == 1
    s = parsed.sleep_rows[0]
    assert s["sleep_date"] == dt.date(2026, 1, 2)  # wake-up day
    assert s["sleep_start"].hour == 22  # previous evening
    assert s["total_sleep_h"] == 7.5
    assert abs((s["deep_h"] + s["core_h"] + s["rem_h"]) - s["total_sleep_h"]) < 1e-6


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
