"""LLM narration of the current findings snapshot via a local Ollama instance.

Reads the ``findings`` table, builds a privacy-safe statistical context
(no raw health values — only z-scores, slopes, ratios and coefficients), and
calls Ollama's ``/api/chat`` endpoint to produce a weekly health report.

Usage::

    docker exec healthlog healthlog narrate
    docker exec healthlog healthlog narrate --note "Focus on the HRV/training link."
    docker exec healthlog healthlog narrate --lookback-days 14 --language en

The report is written to ``/config/narration/YYYY-MM-DD.md`` and printed to
stdout. Configure the Ollama endpoint and model under ``narrate:`` in
``config.yaml``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

# report_priority (and its helpers) live in the analysis package (findings.py)
# next to the metric taxonomy, so the nightly pipeline can stamp each correlation
# with its tier and
# the narration/Grafana rank by the same rule. Re-exported here for tests.
from .analysis import _metric_domain, _pair_tier, report_priority  # noqa: F401
from .appconfig import NarrateConfig, load_config
from .cli_support import bootstrap, db_session
from .logging_config import safe

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger("healthlog.narrate")

# ---------------------------------------------------------------------------
# System prompts — one per supported language.
# These are code artefacts, not config: they encode the privacy constraint
# (no diagnoses, no invented numbers) and must not be user-overridable.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPTS: dict[str, str] = {
    "de": """\
Du bist ein persönlicher Gesundheitsanalyst. Du erhältst statistische \
Auswertungen einer Apple-Health-Analyse und schreibst daraus einen fundierten \
deutschen Wochen-Gesundheitsbericht.

Deine Aufgabe ist es, nicht nur WAS die Daten zeigen zu beschreiben, sondern \
WAS das bedeutet, WARUM es so sein könnte, und welche Zusammenhänge zwischen \
den Befunden bestehen.

## Statistisches Hintergrundwissen

Z-Scores (z) — persönliche Abweichung vom Baseline:
  |z| < 1.5  → im persönlichen Normalbereich
  |z| 1.5–2.5 → leicht auffällig
  |z| 2.5–3.5 → deutlich außerhalb des Normalbereichs
  |z| > 3.5   → starker Ausreißer
  Negatives z = unter dem persönlichen Durchschnitt; Positives z = darüber.
  global_z = Abweichung von der gesamten Messhistorie (nicht nur letzter Monat).

Wichtige Metriken:
  HRV (Herzratenvariabilität): Höher = besser. Niedrige HRV zeigt erhöhte \
Körperbelastung und schlechtere Erholung (autonomes Stresssignal). Ist der \
zuverlässigste Erholungsindikator.
  Ruheherzfrequenz (RHR): Niedriger = besser. Erhöhte RHR signalisiert \
Stress, unvollständige Erholung oder beginnende Erkrankung.
  HRV niedrig + RHR hoch gleichzeitig: Starkes Warnsignal — erkläre \
das physiologische Zusammenspiel (autonomes Nervensystem unter Last).
  ACWR (Acute:Chronic Workload Ratio):
    < 0.8   → Untertraining, Fitnessrückgang möglich
    0.8–1.3 → Optimale Trainingszone
    1.3–1.5 → Erhöhtes Risiko, vorsichtig dosieren
    > 1.5   → Deutlich erhöhtes Übertrainings- und Verletzungsrisiko
  Schlaf-Konsistenz (σ = Standardabweichung):
    σ < 0.5h → sehr konsistent, optimal für Erholung
    σ 0.5–1.0h → akzeptabel
    σ > 1.0h → inkonsistent, beeinträchtigt Schlafqualität und HRV

Korrelationen (Spearman r):
  |r| 0.25–0.40 → moderate Verbindung
  |r| 0.40–0.60 → deutliche Verbindung
  |r| > 0.60   → starke Verbindung
  Lag N Tage: Metrik A beeinflusst Metrik B mit N Tagen Verzögerung \
(z.B. Trainingsbelastung heute → HRV sinkt in 2 Tagen).

## Querverbindungen herstellen
  Recovery Alert + hohe Trainingsbelastung → Übertraining diskutieren
  Recovery Alert ohne hohe Belastung → mögliche Erkrankung oder Stress erwähnen
  Schlechter Schlaf + niedrige HRV → Schlaf als Erholungsbremse erklären
  Korrelation Trainingsbelastung → HRV/RHR → Erholungsverzögerung (Lag) erläutern

## Berichtsstruktur
1. Zusammenfassung (2–3 Sätze: was ist diese Woche das Wichtigste?)
2. Anomalien & Warnungen (Zahl interpretieren + physiologische Erklärung)
3. Training (ACWR-Zone benennen, Bedeutung erklären, Empfehlung geben)
4. Schlaf (Konsistenz und Erholungsqualität)
5. Korrelationen & Trends (nur bedeutsame, mit Erklärung des Mechanismus)
6. Empfehlungen (2–3 konkrete, umsetzbare Maßnahmen für die kommende Woche)

Regeln:
  Sachlich und präzise, kein alarmistischer Ton — aber klar wenn etwas auffällig ist.
  Maximal 700 Wörter.
  Nur die übergebenen Befunde verwenden — keine erfundenen Zahlen.
  Keine medizinischen Diagnosen; bei anhaltenden Beschwerden Arzt empfehlen.
  Wenn keine Anomalien vorliegen, das explizit und positiv formulieren.\
""",
    "en": """\
You are a personal health analyst. You receive statistical findings from an \
Apple Health analysis and write an in-depth English weekly health report.

Your task is not just to describe WHAT the data shows, but WHAT it means, \
WHY it might be the case, and what connections exist between the findings.

## Statistical background knowledge

Z-scores (z) — personal deviation from baseline:
  |z| < 1.5   → within personal normal range
  |z| 1.5–2.5 → mildly notable
  |z| 2.5–3.5 → clearly outside normal range
  |z| > 3.5   → strong outlier
  Negative z = below personal average; Positive z = above average.
  global_z = deviation from the full measurement history (not just last month).

Key metrics:
  HRV (Heart Rate Variability): Higher = better. Low HRV signals elevated \
physical load and poor recovery (autonomic stress signal). Most reliable \
recovery indicator.
  Resting Heart Rate (RHR): Lower = better. Elevated RHR signals stress, \
incomplete recovery, or early illness.
  HRV low + RHR high simultaneously: Strong warning — explain the \
physiological interplay (autonomic nervous system under load).
  ACWR (Acute:Chronic Workload Ratio):
    < 0.8   → undertraining, fitness loss possible
    0.8–1.3 → optimal training zone
    1.3–1.5 → elevated risk, train cautiously
    > 1.5   → significantly elevated overtraining and injury risk
  Sleep consistency (σ = standard deviation):
    σ < 0.5h → very consistent, optimal for recovery
    σ 0.5–1.0h → acceptable
    σ > 1.0h → inconsistent, impairs sleep quality and HRV

Correlations (Spearman r):
  |r| 0.25–0.40 → moderate association
  |r| 0.40–0.60 → clear association
  |r| > 0.60   → strong association
  Lag N days: metric A influences metric B with N days delay \
(e.g. training load today → HRV drops in 2 days).

## Cross-finding connections to make
  Recovery alert + high training load → discuss overtraining
  Recovery alert without high load → mention possible illness or life stress
  Poor sleep + low HRV → explain sleep as recovery bottleneck
  Correlation training load → HRV/RHR → explain the recovery lag mechanism

## Report structure
1. Summary (2–3 sentences: what is most important this week?)
2. Anomalies & Alerts (interpret the number + physiological explanation)
3. Training (name the ACWR zone, explain its meaning, give a recommendation)
4. Sleep (consistency and recovery quality)
5. Correlations & Trends (only significant ones, with mechanism explanation)
6. Recommendations (2–3 concrete, actionable steps for the coming week)

Rules:
  Be factual and precise; avoid alarmist language — but be clear when something is notable.
  Maximum 700 words.
  Use only the provided findings — do not invent numbers.
  Do not make medical diagnoses; recommend a doctor for persistent concerns.
  If there are no anomalies, state that explicitly and frame it positively.\
""",
}


def _system_prompt(language: str) -> str:
    return _SYSTEM_PROMPTS.get(language, _SYSTEM_PROMPTS["de"])


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
# Database query
# ---------------------------------------------------------------------------

_FINDINGS_SQL = """\
SELECT
    f.kind,
    f.metric_a,
    COALESCE(ra.display_name, f.metric_a) AS metric_a_label,
    f.metric_b,
    COALESCE(rb.display_name, f.metric_b) AS metric_b_label,
    f.lag_days,
    f.coefficient,
    f.p_value_adj,
    f.ref_date,
    f.window_start,
    f.window_end,
    f.severity,
    f.details,
    f.note,
    f.computed_at
FROM findings f
LEFT JOIN metric_registry ra ON ra.metric = f.metric_a
LEFT JOIN metric_registry rb ON rb.metric = f.metric_b
WHERE
    (
        f.kind IN ('anomaly', 'recovery_alert', 'training_load')
        AND f.ref_date >= CURRENT_DATE - :lookback_days
    )
    OR f.kind IN ('correlation', 'trend', 'seasonality', 'consistency')
ORDER BY f.kind, f.ref_date DESC NULLS LAST, f.severity DESC NULLS LAST
"""


def load_findings(db: Session, lookback_days: int) -> list[dict]:
    """Query the current findings snapshot, joining display names from the registry."""
    from sqlalchemy import text

    rows = db.execute(text(_FINDINGS_SQL), {"lookback_days": lookback_days}).mappings().all()
    result = []
    for row in rows:
        d = dict(row)
        # details comes back as a string from some drivers — parse if needed.
        if isinstance(d.get("details"), str):
            try:
                d["details"] = json.loads(d["details"])
            except (ValueError, TypeError):
                d["details"] = {}
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Context building
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


# ---------------------------------------------------------------------------
# Ollama HTTP client
# ---------------------------------------------------------------------------


class OllamaClient:
    """Thin wrapper around Ollama's ``/api/chat`` endpoint.

    Injectable ``client`` parameter for testing (matches ``GotifyNotifier``
    pattern). HTTP errors propagate to the caller; ``run()`` handles them.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = 300.0,
        thinking: bool = False,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._thinking = thinking
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout))

    def generate(self, system_prompt: str, user_message: str) -> str:
        """POST to ``/api/chat`` and return the generated text.

        Raises ``httpx.HTTPError`` on network / HTTP failures.
        Raises ``ValueError`` if the response shape is unexpected.
        """
        url = f"{self._base_url}/api/chat"
        payload: dict = {
            "model": self._model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }
        if self._thinking:
            # qwen3-family extended thinking: the model reasons internally before
            # generating the response. Ignored by non-qwen3 models.
            payload["think"] = True
        response = self._client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        try:
            return data["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"unexpected Ollama response shape — missing message.content: {safe(str(data)[:200])}"
            ) from exc

    def close(self) -> None:
        self._client.close()


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------


def write_report(report: str, output_dir: str | Path, report_date: dt.date) -> Path:
    """Write the report to ``<output_dir>/YYYY-MM-DD.md``."""
    p = Path(output_dir)
    p.mkdir(parents=True, exist_ok=True)
    path = p / f"{report_date}.md"
    path.write_text(report, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        metavar="N",
        help="override narrate.lookback_days from config.yaml",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        metavar="DIR",
        help="override the output directory (default: /config/narration)",
    )
    parser.add_argument(
        "--note",
        default=None,
        metavar="TEXT",
        help="optional free-text note appended to the findings context (e.g. 'focus on the HRV/training correlation')",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="render and print the findings context that would be sent to the model, then exit "
        "without calling Ollama or writing a report (works without narrate.ollama_url set)",
    )


def run(args: argparse.Namespace) -> int:
    settings = bootstrap()
    app_config = load_config(settings.config_file)
    cfg: NarrateConfig = app_config.narrate

    # Apply CLI overrides.
    lookback_days = args.lookback_days if args.lookback_days is not None else cfg.lookback_days
    output_dir = args.output_dir if args.output_dir is not None else (Path(settings.config_file).parent / "narration")

    # A real run needs Ollama; a dry run only renders the findings context (no
    # model call), so it must work even when no endpoint is configured.
    if not args.dry_run and not cfg.ollama_url:
        log.error("narrate.ollama_url is not set — add it to config.yaml (e.g. ollama_url: http://192.168.1.100:11434)")
        return 1

    with db_session() as db:
        findings = load_findings(db, lookback_days)

    log.info("narrate: loaded %d findings (lookback_days=%d)", len(findings), lookback_days)

    today = dt.date.today()
    context = build_context(
        findings,
        lookback_days,
        today,
        note=args.note,
        language=cfg.language,
        max_correlations=cfg.max_correlations,
    )

    if args.dry_run:
        # Inspect the exact text the model would receive, deterministically and
        # without contacting Ollama — the "data -> report" bridge, minus the LLM.
        print(context)
        log.info("narrate --dry-run: rendered context for %d findings, no model call made", len(findings))
        return 0

    client = OllamaClient(cfg.ollama_url, cfg.model, timeout=float(cfg.timeout_s), thinking=cfg.thinking)
    try:
        report = client.generate(_system_prompt(cfg.language), context)
    except httpx.HTTPError as exc:
        log.error("ollama call failed: %s", safe(str(exc)))
        return 1
    except ValueError as exc:
        log.error("ollama response error: %s", safe(str(exc)))
        return 1
    finally:
        client.close()

    path = write_report(report, output_dir, today)
    print(report)
    log.info("narration written to %s", path)
    return 0
