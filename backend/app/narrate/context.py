"""Findings → privacy-safe plain-text context for the narration LLM.

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
        return "normal" if language == "en" else "normal"
    if az < 2.5:
        return "mildly notable" if language == "en" else "leicht auffällig"
    if az < 3.5:
        return "clearly outside normal range" if language == "en" else "deutlich außerhalb Normalbereich"
    return "strong outlier" if language == "en" else "starker Ausreißer"


def _acwr_zone(ratio: float, language: str) -> str:
    """Acute:chronic workload-ratio zone label (matches the system-prompt thresholds)."""
    if ratio < 0.8:
        return "undertraining zone" if language == "en" else "Untertrainingszone"
    if ratio <= 1.3:
        return "optimal zone" if language == "en" else "optimale Zone"
    if ratio <= 1.5:
        return "caution zone" if language == "en" else "Vorsichtszone"
    return "high overtraining risk" if language == "en" else "hohes Übertrainingsrisiko"


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


def build_context(
    findings: list[dict],
    lookback_days: int,
    report_date: dt.date,
    *,
    note: str | None = None,
    language: str = "de",
    max_correlations: int | None = None,
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

    blocks: list[list[str]] = [
        header,
        _section_anomalies(by_kind.get("anomaly", []), language, lookback_days),
        _section_recovery(by_kind.get("recovery_alert", []), language),
        _section_training_load(by_kind.get("training_load", []), language),
        _section_correlations(by_kind.get("correlation", []), language, max_correlations),
        _section_trends(by_kind.get("trend", []), language),
        _section_seasonality(by_kind.get("seasonality", []), language),
        _section_consistency(by_kind.get("consistency", []), language),
    ]
    if note:
        header_label = "=== NUTZERHINWEIS ===" if language == "de" else "=== USER NOTE ==="
        blocks.append([header_label, note])

    return "\n\n".join("\n".join(block) for block in blocks)
