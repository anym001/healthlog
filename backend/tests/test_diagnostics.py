"""Unit tests for the workout-HR diagnostic scan (app/diagnostics.py).

Pure: the scan takes raw payload dicts, so no DB is needed and it runs in the
default suite.
"""

from __future__ import annotations

from app.diagnostics import scan_workout_hr


def _payload(workouts):
    return {"data": {"workouts": workouts}}


def test_scan_detects_intra_workout_hr_series():
    payload = _payload(
        [
            {
                "id": "w1",
                "heartRate": {"min": 80, "avg": 140, "max": 175},  # summary only
                "heartRateData": [{"date": "t0", "qty": 130}, {"date": "t1", "qty": 150}],
                "stepCount": [{"date": "t0", "qty": 10}],
            }
        ]
    )
    report = scan_workout_hr([payload])
    assert report.payloads == 1
    assert report.workouts == 1
    assert report.with_hr_series == 1
    assert report.edwards_feasible is True
    assert report.hr_series_len_min == 2 and report.hr_series_len_max == 2
    # Every array-valued field is surfaced so we see what HAE actually sends.
    assert report.array_fields == {"heartRateData": 1, "stepCount": 1}


def test_scan_summary_only_workout_is_not_feasible():
    # Mirrors the real fixture: only a heartRate {min,avg,max} summary, no series.
    payload = _payload([{"id": "w1", "heartRate": {"min": 80, "avg": 140, "max": 175}}])
    report = scan_workout_hr([payload])
    assert report.workouts == 1
    assert report.with_hr_series == 0
    assert report.edwards_feasible is False
    assert report.array_fields == {}  # a dict is not an array field
    assert report.hr_series_len_median is None


def test_scan_aggregates_across_payloads():
    payloads = [
        _payload([{"id": "a", "heartRateData": [1, 2, 3]}]),
        _payload([{"id": "b"}, {"id": "c", "heartRateData": [1, 2, 3, 4, 5]}]),
        _payload([]),  # no workouts
    ]
    report = scan_workout_hr(payloads)
    assert (report.payloads, report.workouts, report.with_hr_series) == (3, 3, 2)
    assert report.hr_series_len_min == 3 and report.hr_series_len_max == 5
    assert report.array_fields == {"heartRateData": 2}


def test_scan_ignores_empty_series_and_bad_shapes():
    payloads = [
        _payload([{"id": "a", "heartRateData": []}]),  # present but empty -> not usable
        {"data": {"workouts": ["not-a-dict"]}},  # malformed workout entry
        {},  # no data
    ]
    report = scan_workout_hr(payloads)
    assert report.workouts == 1  # only the well-formed (empty-series) workout counts
    assert report.with_hr_series == 0
    assert report.edwards_feasible is False
