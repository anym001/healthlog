"""Unit guard: conversion + flagging of deviating units."""

from __future__ import annotations

import math

from app import units


def test_kj_converted_to_kcal_for_energy():
    # active_energy is canonical kcal; HAE ships kJ.
    out = units.normalise("active_energy", "kJ", 418.4)
    assert out.unit == "kcal"
    assert not out.flagged
    assert math.isclose(out.value, 418.4 * 0.2390057361, rel_tol=1e-9)


def test_matching_unit_passes_through():
    out = units.normalise("step_count", "count", 1000)
    assert out.unit == "count"
    assert out.value == 1000
    assert not out.flagged


def test_unknown_mismatch_is_flagged_and_kept():
    # heart_rate canonical is count/min; an unexpected unit can't be converted.
    out = units.normalise("heart_rate", "bpm", 60)
    assert out.flagged
    assert out.unit == "bpm"
    assert out.value == 60


def test_unknown_metric_passes_through_unflagged():
    out = units.normalise("future_unknown_metric", "widgets", 7)
    assert out.unit == "widgets"
    assert out.value == 7
    assert not out.flagged


def test_convert_helper_roundtrip():
    assert math.isclose(units.convert(100, "kcal", "kJ"), 100 / 0.2390057361, rel_tol=1e-9)
    assert units.convert(5, "km", "miles") is None
