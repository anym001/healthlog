"""Workout-type normalisation (pure module, no DB)."""

from __future__ import annotations

from app.workout_types import BUILTIN_WORKOUT_TYPES, canonical_workout_type, slugify


def test_builtin_map_normalises_common_apple_types():
    assert canonical_workout_type("Outdoor Run") == "running"
    assert canonical_workout_type("Traditional Strength Training") == "strength"
    assert canonical_workout_type("Pool Swim") == "swimming"


def test_builtin_map_is_language_stable():
    # The point of §4.4: the same sport must not fragment across a phone
    # language switch. German and English names fold to one canonical type.
    assert canonical_workout_type("Laufen") == canonical_workout_type("Outdoor Run") == "running"
    assert canonical_workout_type("Radfahren") == canonical_workout_type("Outdoor Cycle") == "cycling"
    assert canonical_workout_type("Wandern") == canonical_workout_type("Hiking") == "hiking"


def test_german_hae_export_names_resolve():
    # HAE emits its own German spellings, which differ from the Apple Fitness
    # picker (e.g. "Outdoor Radfahren" vs. the app's "Rad outdoor"). These are
    # the forms observed in a real ``workouts`` table that previously fell
    # through to NULL and grouped as "Other" in Grafana.
    assert canonical_workout_type("Outdoor Spaziergang") == "walking"
    assert canonical_workout_type("Outdoor Radfahren") == "cycling"
    assert canonical_workout_type("Innenräume Radfahren") == "cycling"
    assert canonical_workout_type("Traditionelles Krafttraining") == "strength"
    assert canonical_workout_type("Freiwasser Schwimmen") == "swimming"
    assert canonical_workout_type("Schwimmbad Schwimmen") == "swimming"
    assert canonical_workout_type("Elliptisch") == "elliptical"


def test_apple_fitness_picker_names_resolve():
    # The other spelling of the same sports, as shown in the German app picker.
    assert canonical_workout_type("Rad outdoor") == "cycling"
    assert canonical_workout_type("Gehen outdoor") == "walking"
    assert canonical_workout_type("Laufen outdoor") == "running"
    assert canonical_workout_type("Rudern indoor") == "rowing"
    assert canonical_workout_type("Beckenschwimmen") == "swimming"
    assert canonical_workout_type("Crosstrainer") == "elliptical"


def test_matching_is_case_and_whitespace_insensitive():
    assert canonical_workout_type("  OUTDOOR   run ") == "running"
    # No-break space (U+00A0), as seen in HAE source strings, must still match.
    assert canonical_workout_type("Outdoor\u00a0Run") == "running"


def test_config_overrides_builtin():
    assert canonical_workout_type("Outdoor Run", {"Outdoor Run": "trail running"}) == "trail_running"


def test_config_extends_builtin():
    # A name the built-in does not know is resolved from the config map.
    assert canonical_workout_type("Quidditch Match", {"Quidditch Match": "quidditch"}) == "quidditch"


def test_unknown_and_empty_return_none():
    assert canonical_workout_type("Quidditch Match") is None
    assert canonical_workout_type("Quidditch Match", {}) is None
    assert canonical_workout_type(None) is None
    assert canonical_workout_type("") is None


def test_slugify_makes_safe_suffixes():
    assert slugify("Trail Running") == "trail_running"
    assert slugify("  High-Intensity  Interval  ") == "high_intensity_interval"


def test_builtin_values_are_already_slugs():
    # Every canonical value must survive slugify unchanged, so series-name
    # suffixes stay stable regardless of which side (built-in/config) resolved.
    for canonical in set(BUILTIN_WORKOUT_TYPES.values()):
        assert slugify(canonical) == canonical
