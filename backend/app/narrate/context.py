"""Findings → plain-text context for the narration LLM.

Each finding kind is rendered by its own ``_section_<kind>`` helper;
:func:`build_context` builds the header, groups findings by kind, and stitches
the section blocks together with a single blank line between them. All finding
fields pass through :func:`scrub_details` (raw values are acceptable here — the
LLM is local); only metric display names are ever used as labels.
"""

from __future__ import annotations

import datetime as dt

# report_priority ranks correlations by cross-domain informativeness; it lives in
# the analysis package next to the metric taxonomy so the nightly pipeline and the
# narration order by the same rule.
from ..analysis import report_priority

# ---------------------------------------------------------------------------
# Privacy scrubbing
# ---------------------------------------------------------------------------


def scrub_details(kind: str, details: dict | None) -> dict:
    """Return the finding's ``details`` JSONB for the LLM prompt.

    All fields are passed through — this installation uses a local LLM so
    raw health values are acceptable context. The function is kept as the
    single pass-through point so a future privacy mode can be re-added here
    without touching callers.
    """
    if details is None:
        return {}
    return dict(details)


# ---------------------------------------------------------------------------
# Month names + small label helpers
# ---------------------------------------------------------------------------

_MONTH_NAMES_DE = [
    "",
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]
_MONTH_NAMES_EN = [
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def _month_name(n: int, language: str) -> str:
    names = _MONTH_NAMES_DE if language == "de" else _MONTH_NAMES_EN
    return names[n] if 1 <= n <= 12 else str(n)


def _label(f: dict, key: str) -> str:
    """Prefer the registry display name; fall back to the raw key, then the field name."""
    return f.get(f"{key}_label") or f.get(key) or key


def _z_label(z: float, language: str) -> str:
    """Plain-language band for a z-score magnitude (matches the system-prompt thresholds)."""
    az = abs(z)
    if az < 1.5:
        return "normal"  # the same word in both languages
    if az < 2.5:
        return "mildly notable" if language == "en" else "leicht auffällig"
    if az < 3.5:
        return "clearly outside normal range" if language == "en" else "deutlich außerhalb Normalbereich"
    return "strong outlier" if language == "en" else "starker Ausreißer"


# training_status zone slug (findings.py _tsb_zone) -> (en, de) label.
_TSB_ZONE_LABELS = {
    "detraining": ("detraining - load well below fitness", "Detraining – Belastung weit unter Fitnessniveau"),
    "fresh": ("fresh / tapered", "frisch / erholt (Taper)"),
    "neutral": ("neutral - load matches fitness", "neutral – Belastung entspricht Fitnessniveau"),
    "productive": ("productive training", "produktives Training"),
    "overreaching_risk": ("overreaching risk", "Überlastungsrisiko"),
}
_CTL_TREND_LABELS = {
    "rising": ("rising", "steigend"),
    "falling": ("falling", "fallend"),
    "flat": ("stable", "stabil"),
}


def _tsb_zone_label(zone: str | None, language: str) -> str | None:
    """Translated training-status zone label, or None for an unknown slug."""
    pair = _TSB_ZONE_LABELS.get(zone or "")
    return (pair[0] if language == "en" else pair[1]) if pair else None


def _acwr_zone(ratio: float, language: str) -> str:
    """Acute:chronic workload-ratio zone label (matches the system-prompt thresholds)."""
    if ratio < 0.8:
        return "undertraining zone" if language == "en" else "Untertrainingszone"
    if ratio <= 1.3:
        return "optimal zone" if language == "en" else "optimale Zone"
    if ratio <= 1.5:
        return "caution zone" if language == "en" else "Vorsichtszone"
    return "high overtraining risk" if language == "en" else "hohes Übertrainingsrisiko"


def _stress_zone(score: float, language: str) -> str:
    """Daily stress-score band (0-100), matching the system-prompt thresholds."""
    if score < 25:
        return "rest" if language == "en" else "Ruhe"
    if score < 50:
        return "low" if language == "en" else "niedrig"
    if score < 75:
        return "medium" if language == "en" else "mittel"
    return "high" if language == "en" else "hoch"


# ---------------------------------------------------------------------------
# Per-section builders. Each takes the findings of its kind and returns the
# section's lines (header + body, or a "–" placeholder); ``build_context``
# stitches them together with one blank line between sections.
# ---------------------------------------------------------------------------


def _section_anomalies(anomalies: list[dict], language: str, lookback_days: int) -> list[str]:
    if language == "de":
        out = [f"=== ANOMALIEN (letzte {lookback_days} Tage) ==="]
    else:
        out = [f"=== ANOMALIES (last {lookback_days} days) ==="]
    if not anomalies:
        return [*out, "–"]
    for f in anomalies:
        d = scrub_details("anomaly", f.get("details"))
        z_val = d.get("z")
        z_str = f"{z_val:.2f} ({_z_label(z_val, language)})" if z_val is not None else "n/a"
        parts = [f"z={z_str}"]
        if "global_z" in d:
            parts.append(f"global_z={d['global_z']:.2f}")
        if "value" in d:
            parts.append(f"value={d['value']}")
        out.append(f"[{f['ref_date']}] {_label(f, 'metric_a')}: {', '.join(parts)}")
    return out


def _section_recovery(alerts: list[dict], language: str) -> list[str]:
    out = ["=== ERHOLUNGSWARNUNG ==="] if language == "de" else ["=== RECOVERY ALERT ==="]
    if not alerts:
        return [*out, "–"]
    for f in alerts:
        d = scrub_details("recovery_alert", f.get("details"))
        parts = []
        hrv_z = d.get("heart_rate_variability_z")
        rhr_z = d.get("resting_heart_rate_z")
        if hrv_z is not None:
            direction = "below baseline" if language == "en" else "unter Baseline"
            parts.append(f"HRV-z={hrv_z:.2f} ({direction}, {_z_label(hrv_z, language)})")
        if rhr_z is not None:
            direction = "above baseline" if language == "en" else "über Baseline"
            parts.append(f"RHR-z={rhr_z:.2f} ({direction}, {_z_label(rhr_z, language)})")
        if d.get("short_sleep"):
            parts.append("short sleep also present" if language == "en" else "kurzer Schlaf ebenfalls vorhanden")
        alert_note = f.get("note") or ""
        out.append(f"[{f['ref_date']}] {', '.join(parts)}" + (f" — {alert_note}" if alert_note else ""))
    return out


def _section_stress(alerts: list[dict], language: str) -> list[str]:
    out = ["=== STRESS ==="]
    if not alerts:
        return [*out, "–"]
    for f in alerts:
        d = scrub_details("stress", f.get("details"))
        score = d.get("score", f.get("severity"))
        score_str = f"{score:.0f} ({_stress_zone(score, language)})" if score is not None else "n/a"
        parts = [("score=" if language == "en" else "Score=") + score_str]
        if d.get("high_min") is not None:
            parts.append(("high-stress min=" if language == "en" else "Hochstress-Min=") + f"{d['high_min']}")
        hrv_z = d.get("hrv_z")
        if hrv_z is not None:
            parts.append(f"HRV-z={hrv_z:.2f} ({_z_label(hrv_z, language)})")
        note_str = f" — {f['note']}" if f.get("note") else ""
        out.append(f"[{f['ref_date']}] {', '.join(parts)}{note_str}")
    return out


def _section_body_battery(alerts: list[dict], language: str) -> list[str]:
    out = ["=== BODY BATTERY ==="]
    if not alerts:
        return [*out, "–"]
    for f in alerts:
        d = scrub_details("body_battery", f.get("details"))
        # No severity fallback: severity is the depletion (100 − low_level),
        # not the level itself; the raw level is always present in details.
        low = d.get("low_level")
        low_str = f"{low:.0f}" if low is not None else "n/a"
        parts = [("low=" if language == "en" else "Tief=") + low_str]
        wake = d.get("wake_level")
        if wake is not None:
            parts.append(("wake=" if language == "en" else "Weckstand=") + f"{wake:.0f}")
        high = d.get("high_level")
        if high is not None:
            parts.append(("high=" if language == "en" else "Hoch=") + f"{high:.0f}")
        drained = d.get("drained")
        charged = d.get("charged")
        if drained is not None:
            parts.append(("drained=" if language == "en" else "entladen=") + f"{drained:.0f}")
        if charged is not None:
            parts.append(("charged=" if language == "en" else "geladen=") + f"{charged:.0f}")
        note_str = f" — {f['note']}" if f.get("note") else ""
        out.append(f"[{f['ref_date']}] {', '.join(parts)}{note_str}")
    return out


def _section_training_load(tload: list[dict], language: str) -> list[str]:
    out = ["=== TRAININGSBELASTUNG ==="] if language == "de" else ["=== TRAINING LOAD ==="]
    if not tload:
        return [*out, "–"]
    for f in tload:
        d = scrub_details("training_load", f.get("details"))
        ratio = d.get("ratio", f.get("severity"))
        ratio_str = f"{ratio:.2f}" if ratio is not None else "n/a"
        zone = _acwr_zone(ratio, language) if ratio is not None else ""
        acute = d.get("acute")
        chronic = d.get("chronic")
        load_parts = [f"ACWR={ratio_str} ({zone})"]
        if acute is not None and chronic is not None:
            days_a = d.get("acute_days", 7)
            days_c = d.get("chronic_days", 28)
            load_parts.append(f"acute_{days_a}d={acute:.2f}, chronic_{days_c}d={chronic:.2f}")
        note_str = f" — {f['note']}" if f.get("note") else ""
        out.append(f"[{f['ref_date']}] {_label(f, 'metric_a')}: {', '.join(load_parts)}{note_str}")
    return out


def _section_training_status(status: list[dict], language: str) -> list[str]:
    """The fitness/form snapshot (kind=training_status): CTL/ATL/TSB with the
    normalised-form zone and the 4-week fitness trend. A status, not an alert -
    it gives the report its baseline ("productive, base rising") even when the
    alert sections above are empty."""
    if language == "de":
        out = ["=== TRAININGSZUSTAND (Fitness/Form) ==="]
    else:
        out = ["=== TRAINING STATUS (fitness/form) ==="]
    if not status:
        return [*out, "–"]
    for f in status:
        d = scrub_details("training_status", f.get("details"))
        parts = []
        if d.get("ctl") is not None and d.get("atl") is not None:
            label_f = "fitness" if language == "en" else "Fitness"
            label_l = "fatigue" if language == "en" else "Ermüdung"
            ctl_str = f"{label_f} CTL={d['ctl']:.1f} ({d.get('ctl_days', 42)}d)"
            parts.append(f"{ctl_str}, {label_l} ATL={d['atl']:.1f} ({d.get('atl_days', 7)}d)")
        if d.get("tsb") is not None:
            tsb_pct = d.get("tsb_pct")
            pct_str = f" = {tsb_pct * 100:+.0f}% CTL" if tsb_pct is not None else ""
            parts.append(f"Form TSB={d['tsb']:+.1f}{pct_str}")
        zone = _tsb_zone_label(d.get("zone"), language)
        if zone:
            parts.append(zone)
        trend = _CTL_TREND_LABELS.get(d.get("ctl_trend") or "")
        if trend:
            label = "fitness trend" if language == "en" else "Fitness-Trend"
            days = d.get("ctl_trend_days", 28)
            parts.append(f"{label} ({days}d): {trend[0] if language == 'en' else trend[1]}")
        out.append(f"[{f['ref_date']}] {_label(f, 'metric_a')}: {', '.join(parts)}")
    return out


def _section_correlations(correlations: list[dict], language: str, max_correlations: int | None) -> list[str]:
    # Layer-2 curation: lead with the informative cross-domain links and, when a
    # cap is set, drop the long tail of expected/structural pairs (summarised as
    # a count) so the model isn't swamped by them.
    corrs = sorted(
        correlations,
        key=lambda f: report_priority(f.get("metric_a"), f.get("metric_b"), f.get("coefficient"), f.get("lag_days")),
        reverse=True,
    )
    omitted = 0
    if max_correlations and len(corrs) > max_correlations:
        omitted = len(corrs) - max_correlations
        corrs = corrs[:max_correlations]
    out = ["=== KORRELATIONEN ==="] if language == "de" else ["=== CORRELATIONS ==="]
    if corrs:
        for f in corrs:
            d = scrub_details("correlation", f.get("details"))
            n = d.get("n", "?")
            coef = f"{f['coefficient']:.3f}" if f.get("coefficient") is not None else "n/a"
            p = f"{f['p_value_adj']:.4f}" if f.get("p_value_adj") is not None else "n/a"
            lag = f.get("lag_days", 0)
            lag_label = (
                f"Lag {lag}{'d' if lag != 1 else ' Tag'}"
                if language == "de"
                else f"lag {lag}{'d' if lag != 1 else ' day'}"
            )
            out.append(f"{_label(f, 'metric_a')} → {_label(f, 'metric_b')} ({lag_label}): r={coef}, p_adj={p}, N={n}")
    else:
        out.append("–")
    if omitted:
        if language == "de":
            out.append(f"(+{omitted} weitere, niedrigere Priorität — erwartbar/strukturell, ausgelassen)")
        else:
            out.append(f"(+{omitted} more, lower priority — expected/structural, omitted)")
    return out


def _section_trends(trends: list[dict], language: str) -> list[str]:
    out = ["=== TRENDS ==="]
    if not trends:
        return [*out, "–"]
    for f in trends:
        d = scrub_details("trend", f.get("details"))
        slope = d.get("slope_per_day")
        strength = d.get("strength", f.get("severity"))
        slope_str = (
            f"{slope:+.4f}/Tag"
            if (slope is not None and language == "de")
            else f"{slope:+.4f}/day"
            if slope is not None
            else "n/a"
        )
        strength_str = f"{strength:.2f}" if strength is not None else "n/a"
        ws, we = f.get("window_start"), f.get("window_end")
        window = f" ({ws} – {we})" if ws and we else ""
        out.append(
            f"{_label(f, 'metric_a')}{window}: {slope_str}, "
            + ("Stärke=" if language == "de" else "strength=")
            + strength_str
        )
    return out


def _section_seasonality(seasons: list[dict], language: str) -> list[str]:
    out = ["=== SAISONALITÄT ==="] if language == "de" else ["=== SEASONALITY ==="]
    if not seasons:
        return [*out, "–"]
    for f in seasons:
        d = scrub_details("seasonality", f.get("details"))
        peak = _month_name(d.get("peak_month", 0), language)
        trough = _month_name(d.get("trough_month", 0), language)
        strength = d.get("strength", f.get("severity"))
        strength_str = f"{strength:.2f}" if strength is not None else "n/a"
        uncertain = ""
        if not d.get("phase_confident", True):
            uncertain = " (Phase unsicher)" if language == "de" else " (phase uncertain)"
        out.append(
            f"{_label(f, 'metric_a')}: "
            + ("Peak=" if language == "en" else "Hochpunkt=")
            + f"{peak}, "
            + ("Trough=" if language == "en" else "Tiefpunkt=")
            + f"{trough}, "
            + ("strength=" if language == "en" else "Stärke=")
            + f"{strength_str}{uncertain}"
        )
    return out


def _section_consistency(consistency: list[dict], language: str) -> list[str]:
    out = ["=== SCHLAF-KONSISTENZ ==="] if language == "de" else ["=== SLEEP CONSISTENCY ==="]
    if not consistency:
        return [*out, "–"]
    for f in consistency:
        d = scrub_details("consistency", f.get("details"))
        std = d.get("std_hours", f.get("severity"))
        std_str = f"{std:.2f}h" if std is not None else "n/a"
        note_str = f.get("note") or ""
        out.append(f"{_label(f, 'metric_a')}: σ={std_str} — {note_str}")
    return out


# ---------------------------------------------------------------------------
# Weekly-overview sections (narrate --weekly). Descriptive summaries computed
# by the nightly run (kind=weekly_* / fitness_markers); each renders one
# section block like the alert sections above.
# ---------------------------------------------------------------------------


def _fmt_clock(hour: float) -> str:
    """A float clock hour (23.17) as HH:MM (23:10)."""
    total_min = int(round(hour * 60)) % (24 * 60)
    return f"{total_min // 60:02d}:{total_min % 60:02d}"


def _window_prefix(f: dict) -> str:
    ws, we = f.get("window_start"), f.get("window_end")
    return f"[{ws} – {we}] " if ws and we else ""


def _section_weekly_training(items: list[dict], language: str) -> list[str]:
    out = ["=== WOCHE: TRAINING ==="] if language == "de" else ["=== WEEK: TRAINING ==="]
    if not items:
        return [*out, "–"]
    for f in items:
        d = scrub_details("weekly_training", f.get("details"))
        unit_word = "sessions" if language == "en" else "Einheiten"
        parts = [f"{d.get('sessions', 0)} {unit_word}", f"{d.get('duration_h', 0):.1f} h"]
        if d.get("distance_km"):
            parts.append(f"{d['distance_km']:.1f} km")
        if d.get("energy_kcal"):
            parts.append(f"{d['energy_kcal']:.0f} kcal")
        if d.get("load") is not None:
            parts.append(("load=" if language == "en" else "Last=") + f"{d['load']:.0f} ({_label(f, 'metric_a')})")
        out.append(_window_prefix(f) + ", ".join(parts))
        prev = d.get("prev")
        if prev:
            prev_parts = [f"{prev.get('sessions', 0)} {unit_word}", f"{prev.get('duration_h', 0):.1f} h"]
            if prev.get("load") is not None:
                prev_parts.append(("load=" if language == "en" else "Last=") + f"{prev['load']:.0f}")
            out.append(("  previous week: " if language == "en" else "  Vorwoche: ") + ", ".join(prev_parts))
        if d.get("baseline_load") is not None:
            label = "  4-week average load=" if language == "en" else "  4-Wochen-Schnitt Last="
            out.append(label + f"{d['baseline_load']:.0f}" + ("/week" if language == "en" else "/Woche"))
        for sport in d.get("per_sport") or []:
            sport_label = str(sport.get("sport", "?")).replace("_", " ").title()
            sport_parts = [f"{sport.get('sessions', 0)} {unit_word}", f"{sport.get('duration_h', 0):.1f} h"]
            if sport.get("distance_km"):
                sport_parts.append(f"{sport['distance_km']:.1f} km")
            if sport.get("load") is not None:
                sport_parts.append(("load=" if language == "en" else "Last=") + f"{sport['load']:.0f}")
            out.append(f"  – {sport_label}: " + ", ".join(sport_parts))
    return out


def _section_weekly_sleep(items: list[dict], language: str) -> list[str]:
    out = ["=== WOCHE: SCHLAF ==="] if language == "de" else ["=== WEEK: SLEEP ==="]
    if not items:
        return [*out, "–"]
    for f in items:
        d = scrub_details("weekly_sleep", f.get("details"))
        avg = d.get("avg_total_h")
        parts = [("avg sleep=" if language == "en" else "Ø Schlaf=") + (f"{avg:.2f}h" if avg is not None else "n/a")]
        stages = []
        if d.get("avg_deep_h") is not None:
            pct = f"/{d['deep_pct']:.0f}%" if d.get("deep_pct") is not None else ""
            stages.append(("deep " if language == "en" else "Tiefschlaf ") + f"{d['avg_deep_h']:.2f}h{pct}")
        if d.get("avg_rem_h") is not None:
            pct = f"/{d['rem_pct']:.0f}%" if d.get("rem_pct") is not None else ""
            stages.append(f"REM {d['avg_rem_h']:.2f}h{pct}")
        if stages:
            parts[-1] += " (" + ", ".join(stages) + ")"
        if d.get("avg_efficiency") is not None:
            parts.append(("efficiency=" if language == "en" else "Effizienz=") + f"{d['avg_efficiency'] * 100:.0f}%")
        if d.get("avg_bedtime") is not None:
            parts.append(("avg bedtime=" if language == "en" else "Ø Zubettgehen=") + _fmt_clock(d["avg_bedtime"]))
        parts.append(("nights=" if language == "en" else "Nächte=") + str(d.get("nights", "?")))
        out.append(_window_prefix(f) + ", ".join(parts))
        prev = d.get("prev")
        if prev and prev.get("avg_total_h") is not None:
            prev_parts = [("avg " if language == "en" else "Ø ") + f"{prev['avg_total_h']:.2f}h"]
            if prev.get("avg_efficiency") is not None:
                prev_parts.append(
                    ("efficiency " if language == "en" else "Effizienz ") + f"{prev['avg_efficiency'] * 100:.0f}%"
                )
            out.append(("  previous week: " if language == "en" else "  Vorwoche: ") + ", ".join(prev_parts))
    return out


def _section_weekly_stress(items: list[dict], language: str) -> list[str]:
    out = ["=== WOCHE: STRESS ==="] if language == "de" else ["=== WEEK: STRESS ==="]
    if not items:
        return [*out, "–"]
    for f in items:
        d = scrub_details("weekly_stress", f.get("details"))
        avg = d.get("avg_score")
        avg_str = f"{avg:.0f} ({_stress_zone(avg, language)})" if avg is not None else "n/a"
        parts = [("avg score=" if language == "en" else "Ø Score=") + avg_str]
        if d.get("high_min") is not None:
            parts.append(("high-stress=" if language == "en" else "Hochstress=") + f"{d['high_min']} min")
        if d.get("medium_min") is not None:
            parts.append(("medium=" if language == "en" else "Mittel=") + f"{d['medium_min']} min")
        if d.get("peak_day") is not None and d.get("peak_score") is not None:
            label = "peak day=" if language == "en" else "Spitzentag="
            parts.append(f"{label}{d['peak_day']} (Score {d['peak_score']:.0f})")
        if d.get("calm_day") is not None and d.get("calm_score") is not None:
            label = "calmest day=" if language == "en" else "ruhigster Tag="
            parts.append(f"{label}{d['calm_day']} (Score {d['calm_score']:.0f})")
        parts.append(("days=" if language == "en" else "Tage=") + str(d.get("days", "?")))
        out.append(_window_prefix(f) + ", ".join(parts))
        prev = d.get("prev")
        if prev and prev.get("avg_score") is not None:
            prev_parts = [("avg " if language == "en" else "Ø ") + f"{prev['avg_score']:.0f}"]
            if prev.get("high_min") is not None:
                prev_parts.append(("high-stress " if language == "en" else "Hochstress ") + f"{prev['high_min']} min")
            out.append(("  previous week: " if language == "en" else "  Vorwoche: ") + ", ".join(prev_parts))
    return out


def _section_weekly_body_battery(items: list[dict], language: str) -> list[str]:
    out = ["=== WOCHE: BODY BATTERY ==="] if language == "de" else ["=== WEEK: BODY BATTERY ==="]
    if not items:
        return [*out, "–"]
    for f in items:
        d = scrub_details("weekly_body_battery", f.get("details"))
        parts = []
        for key, en, de in (
            ("avg_wake", "avg wake=", "Ø Weckstand="),
            ("avg_high", "avg high=", "Ø Hoch="),
            ("avg_low", "avg low=", "Ø Tief="),
        ):
            if d.get(key) is not None:
                parts.append((en if language == "en" else de) + f"{d[key]:.0f}")
        if d.get("min_low") is not None:
            label = "deepest trough=" if language == "en" else "Tiefstwert="
            day = f" ({d['min_low_day']})" if d.get("min_low_day") else ""
            parts.append(f"{label}{d['min_low']:.0f}{day}")
        per_day = "/day" if language == "en" else "/Tag"
        if d.get("avg_charged") is not None:
            parts.append(("charged=" if language == "en" else "geladen=") + f"{d['avg_charged']:.0f}{per_day}")
        if d.get("avg_drained") is not None:
            parts.append(("drained=" if language == "en" else "entladen=") + f"{d['avg_drained']:.0f}{per_day}")
        out.append(_window_prefix(f) + ", ".join(parts))
        prev = d.get("prev")
        if prev and (prev.get("avg_wake") is not None or prev.get("avg_low") is not None):
            prev_parts = []
            if prev.get("avg_wake") is not None:
                prev_parts.append(("wake " if language == "en" else "Weckstand ") + f"{prev['avg_wake']:.0f}")
            if prev.get("avg_low") is not None:
                prev_parts.append(("low " if language == "en" else "Tief ") + f"{prev['avg_low']:.0f}")
            out.append(("  previous week: " if language == "en" else "  Vorwoche: ") + ", ".join(prev_parts))
    return out


def _section_weekly_vitals(items: list[dict], language: str) -> list[str]:
    if language == "de":
        out = ["=== WOCHE: VITALWERTE (vs. 28-Tage-Baseline) ==="]
    else:
        out = ["=== WEEK: VITALS (vs. 28-day baseline) ==="]
    if not items:
        return [*out, "–"]
    for f in items:
        d = scrub_details("weekly_vitals", f.get("details"))
        unit = f" {d['unit']}" if d.get("unit") else ""
        label_w = "week mean=" if language == "en" else "Wochenmittel="
        label_b = "baseline=" if language == "en" else "Baseline="
        parts = [f"{label_w}{d.get('week_mean', 'n/a')}{unit}", f"{label_b}{d.get('baseline_mean', 'n/a')}{unit}"]
        if d.get("delta") is not None:
            pct = f", {d['delta_pct']:+.1f}%" if d.get("delta_pct") is not None else ""
            parts.append(f"Δ={d['delta']:+.1f}{pct}")
        out.append(f"{_label(f, 'metric_a')}: " + ", ".join(parts))
    return out


def _section_weekly_activity(items: list[dict], language: str) -> list[str]:
    out = ["=== WOCHE: AKTIVITÄT ==="] if language == "de" else ["=== WEEK: ACTIVITY ==="]
    if not items:
        return [*out, "–"]
    for f in items:
        d = scrub_details("weekly_activity", f.get("details"))
        unit = f" {d['unit']}" if d.get("unit") else ""
        parts = [("total=" if language == "en" else "gesamt=") + f"{d.get('total', 'n/a')}{unit}"]
        if d.get("daily_avg") is not None:
            per_day = "/day" if language == "en" else "/Tag"
            parts.append(("avg " if language == "en" else "Ø ") + f"{d['daily_avg']}{per_day}")
        if d.get("prev_total") is not None:
            parts.append(("previous week " if language == "en" else "Vorwoche ") + f"{d['prev_total']}")
        if d.get("baseline_weekly") is not None:
            label = "4-week average " if language == "en" else "4-Wochen-Schnitt "
            per_week = "/week" if language == "en" else "/Woche"
            parts.append(label + f"{d['baseline_weekly']}{per_week}")
        out.append(f"{_label(f, 'metric_a')}: " + ", ".join(parts))
    return out


def _section_fitness_markers(items: list[dict], language: str) -> list[str]:
    out = ["=== FITNESS-MARKER ==="] if language == "de" else ["=== FITNESS MARKERS ==="]
    if not items:
        return [*out, "–"]
    for f in items:
        d = scrub_details("fitness_markers", f.get("details"))
        unit = f" {d['unit']}" if d.get("unit") else ""
        date_label = " on " if language == "en" else " am "
        line = f"{_label(f, 'metric_a')}: {d.get('latest', 'n/a')}{unit}{date_label}{d.get('latest_date', '?')}"
        if d.get("delta") is not None:
            line += f" (Δ={d['delta']:+.2f} vs. {d.get('prev_date', '?')})"
        elif language == "de":
            line += " (keine ältere Vergleichsmessung)"
        else:
            line += " (no older reading to compare)"
        out.append(line)
    return out


def build_context(
    findings: list[dict],
    lookback_days: int,
    report_date: dt.date,
    *,
    note: str | None = None,
    language: str = "de",
    max_correlations: int | None = None,
    weekly: bool = False,
) -> str:
    """Format findings as a structured plain-text context for the LLM.

    Passes all finding fields through :func:`scrub_details` (including raw
    values when using a local LLM). Only metric display names (never the raw
    snake_case keys) are used for labels. Each finding kind is rendered by its
    own ``_section_*`` helper; the sections are joined with one blank line.
    """
    computed_at: dt.datetime | None = None
    for f in findings:
        ca = f.get("computed_at")
        if ca and (computed_at is None or ca > computed_at):
            computed_at = ca

    window_start = report_date - dt.timedelta(days=lookback_days - 1)
    if language == "de":
        header = [
            f"Gesundheitsbericht – Berichtsdatum: {report_date}",
            f"Analysezeitraum Anomalien/Training: {window_start} bis {report_date}",
        ]
    else:
        header = [
            f"Health Report – Report date: {report_date}",
            f"Analysis window anomalies/training: {window_start} to {report_date}",
        ]
    if computed_at:
        ts = computed_at.strftime("%Y-%m-%d %H:%M")
        label = "Analyse berechnet am" if language == "de" else "Analysis computed at"
        header.append(f"{label}: {ts}")

    by_kind: dict[str, list[dict]] = {}
    for f in findings:
        by_kind.setdefault(f["kind"], []).append(f)

    # The weekly overview leads the report (the descriptive backbone); the
    # alert/statistics sections follow. Daily reports skip the weekly blocks
    # entirely instead of printing seven empty placeholders.
    weekly_blocks: list[list[str]] = (
        [
            _section_weekly_training(by_kind.get("weekly_training", []), language),
            _section_weekly_sleep(by_kind.get("weekly_sleep", []), language),
            _section_weekly_stress(by_kind.get("weekly_stress", []), language),
            _section_weekly_body_battery(by_kind.get("weekly_body_battery", []), language),
            _section_weekly_vitals(by_kind.get("weekly_vitals", []), language),
            _section_weekly_activity(by_kind.get("weekly_activity", []), language),
            _section_fitness_markers(by_kind.get("fitness_markers", []), language),
        ]
        if weekly
        else []
    )

    blocks: list[list[str]] = [
        header,
        *weekly_blocks,
        _section_anomalies(by_kind.get("anomaly", []), language, lookback_days),
        _section_recovery(by_kind.get("recovery_alert", []), language),
        _section_stress(by_kind.get("stress", []), language),
        _section_body_battery(by_kind.get("body_battery", []), language),
        _section_training_load(by_kind.get("training_load", []), language),
        _section_training_status(by_kind.get("training_status", []), language),
        _section_correlations(by_kind.get("correlation", []), language, max_correlations),
        _section_trends(by_kind.get("trend", []), language),
        _section_seasonality(by_kind.get("seasonality", []), language),
        _section_consistency(by_kind.get("consistency", []), language),
    ]
    if note:
        header_label = "=== NUTZERHINWEIS ===" if language == "de" else "=== USER NOTE ==="
        blocks.append([header_label, note])

    return "\n\n".join("\n".join(block) for block in blocks)
