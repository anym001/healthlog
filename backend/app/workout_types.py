"""Workout-type normalisation: localised HAE ``name`` -> canonical type slug.

HAE ships a workout ``name`` that is **localised** (``'Outdoor Run'`` in English,
``'Outdoor-Lauf'`` in German). Left raw, the same sport fragments across a phone
language switch and a per-sport analysis can no longer tell ``running`` apart
from ``Laufen`` (PLAN.md §4.4). This module is the workout-side counterpart to
``units.py``: a built-in map handles the common Apple workout types out of the
box, and the operator's ``workouts.type_map`` (config.yaml) layers on top for
custom names — config wins, the built-in is the fallback.

Pure functions, no DB. The raw ``name`` is always preserved in ``workouts``;
canonicalisation happens at analysis time so the mapping can evolve without a
re-ingest.
"""

from __future__ import annotations

import unicodedata

# Built-in localised name -> canonical type slug. Keys are matched after
# whitespace/case normalisation (see ``_normalize_key``), so only the spelling
# needs to be listed, not every casing. Covers the common Apple Watch workout
# types in English and German; extend via ``workouts.type_map`` for the rest.
BUILTIN_WORKOUT_TYPES: dict[str, str] = {
    # Running
    "running": "running",
    "outdoor run": "running",
    "indoor run": "running",
    "trail running": "running",
    "laufen": "running",
    "outdoor-lauf": "running",
    "indoor-lauf": "running",
    "lauf": "running",
    # Walking
    "walking": "walking",
    "outdoor walk": "walking",
    "indoor walk": "walking",
    "gehen": "walking",
    "spazieren": "walking",
    "spaziergang": "walking",
    "outdoor-spaziergang": "walking",
    "indoor-spaziergang": "walking",
    # Hiking
    "hiking": "hiking",
    "wandern": "hiking",
    # Cycling
    "cycling": "cycling",
    "outdoor cycle": "cycling",
    "indoor cycle": "cycling",
    "radfahren": "cycling",
    "outdoor-radfahren": "cycling",
    "indoor-radfahren": "cycling",
    # Strength
    "traditional strength training": "strength",
    "functional strength training": "strength",
    "strength training": "strength",
    "krafttraining": "strength",
    "klassisches krafttraining": "strength",
    "funktionelles krafttraining": "strength",
    "core training": "core",
    "core-training": "core",
    # Swimming
    "swimming": "swimming",
    "pool swim": "swimming",
    "open water swim": "swimming",
    "schwimmen": "swimming",
    "beckenschwimmen": "swimming",
    "freiwasserschwimmen": "swimming",
    # Cardio machines / classes
    "elliptical": "elliptical",
    "crosstrainer": "elliptical",
    "rowing": "rowing",
    "rudern": "rowing",
    "stair stepper": "stair_stepper",
    "treppensteigen": "stair_stepper",
    "high intensity interval training": "hiit",
    "hiit": "hiit",
    "hochintensives intervalltraining": "hiit",
    "mixed cardio": "mixed_cardio",
    "gemischtes cardio": "mixed_cardio",
    # Mind & body
    "yoga": "yoga",
    "pilates": "pilates",
    "dance": "dance",
    "cardio dance": "dance",
    "tanzen": "dance",
    "cooldown": "cooldown",
    "cool down": "cooldown",
    "abkühlen": "cooldown",
}


def _normalize_key(name: str) -> str:
    """Lowercase and collapse whitespace (incl. NBSP) for robust matching.

    HAE ``source`` strings carry no-break spaces (PLAN.md §4.2); workout names
    can too, so the lookup must not depend on the exact whitespace byte."""
    # NFKC folds the no-break space (U+00A0) onto a regular space.
    folded = unicodedata.normalize("NFKC", name)
    return " ".join(folded.lower().split())


def slugify(value: str) -> str:
    """Lowercase a canonical type to a safe series-name suffix (``a-z0-9_``)."""
    out = "".join(c if c.isalnum() else "_" for c in value.strip().lower())
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


def canonical_workout_type(name: str | None, type_map: dict[str, str] | None = None) -> str | None:
    """Map a localised HAE workout ``name`` to its canonical type slug.

    Resolution order: the operator's ``type_map`` first (an override/extension),
    then the built-in map. Matching is case- and whitespace-insensitive. Returns
    None for a missing or unrecognised name — those workouts still feed the
    type-agnostic aggregate, they just get no per-type series.
    """
    if not name:
        return None
    key = _normalize_key(name)
    if type_map:
        override = {_normalize_key(k): v for k, v in type_map.items()}
        mapped = override.get(key)
        if mapped:
            return slugify(mapped)
    builtin = BUILTIN_WORKOUT_TYPES.get(key)
    return slugify(builtin) if builtin else None
