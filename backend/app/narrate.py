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

from .analysis import _is_activity_volume  # the activity-volume family predicate (layer-2 ranking)
from .appconfig import NarrateConfig, load_config
from .config import get_settings
from .logging_config import configure_logging, safe

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
Du bist ein Gesundheits-Assistent. Du erhältst statistische Auswertungen \
einer Apple-Health-Analyse und schreibst daraus einen kompakten deutschen \
Wochen-Gesundheitsbericht.

Regeln:
- Sachlich und präzise, kein alarmistischer Ton.
- Erkläre was die Statistiken bedeuten (z.B. „z = 3.9 bedeutet deutlich \
außerhalb des 28-Tage-Normalbereichs").
- Wenn keine Anomalien oder Warnungen vorliegen, sage das explizit.
- Struktur: Zusammenfassung → Anomalien & Warnungen → Training → Schlaf \
→ Korrelationen & Trends.
- Maximal 400 Wörter.
- Keine erfundenen Zahlen — verwende nur die übergebenen Befunde.
- Stelle keine medizinischen Diagnosen; empfehle bei Bedenken einen Arzt.\
""",
    "en": """\
You are a health analysis assistant. You receive statistical findings from an \
Apple Health analysis and write a concise English weekly health report.

Rules:
- Be factual and precise; avoid alarmist language.
- Explain what statistics mean (e.g. "z = 3.9 means well outside the 28-day \
normal range").
- If there are no anomalies or alerts, say so explicitly.
- Structure: Summary → Anomalies & Alerts → Training → Sleep \
→ Correlations & Trends.
- Maximum 400 words.
- Use only the provided findings — do not invent numbers.
- Do not make medical diagnoses; recommend seeing a doctor if concerned.\
""",
}


def _system_prompt(language: str) -> str:
    return _SYSTEM_PROMPTS.get(language, _SYSTEM_PROMPTS["de"])


# ---------------------------------------------------------------------------
# Privacy scrubbing
# ---------------------------------------------------------------------------


def scrub_details(kind: str, details: dict | None) -> dict:
    """Strip raw health values from a finding's ``details`` JSONB.

    Only statistical interpretations are passed to the LLM — z-scores, slopes,
    ratios, coefficients. The ``value`` field in anomaly findings is the raw
    sensor reading and must never appear in the prompt.
    """
    if details is None:
        return {}
    if kind == "anomaly":
        return {"z": details["z"]} if "z" in details else {}
    if kind == "recovery_alert":
        return {k: details[k] for k in ("rhr_z", "hrv_z", "short_sleep") if k in details}
    # correlation, trend, seasonality, consistency, training_load: no raw values.
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
# Correlation prioritisation (layer 2)
#
# The analysis already suppresses structural pairs (activity-volume vs itself),
# but the survivors still span a quality gradient: a lagged cross-domain link
# (training load -> next-day respiratory rate) is a real insight, while same-day
# pairs within one physiological subsystem (total vs deep sleep, average vs
# resting heart rate) are expected and only crowd the report. report_priority()
# ranks findings so the narration leads with the informative ones. This is
# presentation-only — it never changes which findings are computed or stored.
# ---------------------------------------------------------------------------

_AUTONOMIC = frozenset({"respiratory_rate", "heart_rate_variability", "cardio_recovery"})
_HR_RATE = frozenset({"heart_rate", "resting_heart_rate", "walking_heart_rate_average"})
_VITAL = frozenset(
    {
        "vo2_max",
        "apple_sleeping_wrist_temperature",
        "weight_body_mass",
        "blood_oxygen_saturation",
        "body_temperature",
        "breathing_disturbances",
    }
)
_PHYSIO = frozenset({"sleep", "autonomic", "hr_rate", "vital", "activity"})


def _metric_domain(name: str | None) -> str:
    """Coarse physiological domain of a metric, for correlation prioritisation."""
    if not name:
        return "other"
    if name.startswith("sleep_"):
        return "sleep"
    if name in _AUTONOMIC:
        return "autonomic"
    if name in _HR_RATE:
        return "hr_rate"
    if name in _VITAL:
        return "vital"
    if name == "physical_effort" or _is_activity_volume(name):
        return "activity"
    if name == "time_in_daylight":
        return "env"
    return "other"


def _pair_tier(domain_a: str, domain_b: str) -> int:
    """Priority tier for a correlation between two metric domains.

    2 = informative cross-domain (the layer-2 insights), 1 = neutral,
    0 = expected/structural (demoted): a subsystem correlated with itself, or a
    trivially-coupled pairing (exercise raises average HR; sunny days mean more
    movement).
    """
    pair = frozenset({domain_a, domain_b})
    if domain_a == domain_b and domain_a in {"sleep", "hr_rate", "activity"}:
        return 0  # self-correlation within one family
    if pair == {"hr_rate", "activity"}:
        return 0  # exercise raises average HR — trivial
    if pair == {"env", "activity"}:
        return 0  # sunny days = more movement — behavioural
    if domain_a in _PHYSIO and domain_b in _PHYSIO:
        return 2  # cross-subsystem physiological link
    return 1


def report_priority(
    metric_a: str | None, metric_b: str | None, coefficient: float | None, lag_days: int | None
) -> float:
    """Rank score for a correlation finding (higher = lead the report).

    The domain-crossing tier dominates, then the effect size, then a small bonus
    for lagged (directional, causally plausible) relationships.
    """
    tier = _pair_tier(_metric_domain(metric_a), _metric_domain(metric_b))
    strength = abs(coefficient) if coefficient is not None else 0.0
    lag_bonus = 0.05 * min(lag_days or 0, 3)
    return tier * 10.0 + strength + lag_bonus


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

    Raw health values are excluded via :func:`scrub_details`. Only metric
    display names (never the raw snake_case keys) are used.
    """
    computed_at: dt.datetime | None = None
    for f in findings:
        ca = f.get("computed_at")
        if ca and (computed_at is None or ca > computed_at):
            computed_at = ca

    window_start = report_date - dt.timedelta(days=lookback_days - 1)
    if language == "de":
        lines = [
            f"Gesundheitsbericht – Berichtsdatum: {report_date}",
            f"Analysezeitraum Anomalien/Training: {window_start} bis {report_date}",
        ]
    else:
        lines = [
            f"Health Report – Report date: {report_date}",
            f"Analysis window anomalies/training: {window_start} to {report_date}",
        ]
    if computed_at:
        ts = computed_at.strftime("%Y-%m-%d %H:%M")
        label = "Analyse berechnet am" if language == "de" else "Analysis computed at"
        lines.append(f"{label}: {ts}")
    lines.append("")

    by_kind: dict[str, list[dict]] = {}
    for f in findings:
        by_kind.setdefault(f["kind"], []).append(f)

    def _label(f: dict, key: str) -> str:
        return f.get(f"{key}_label") or f.get(key) or key

    # --- Anomalies ---
    anomalies = by_kind.get("anomaly", [])
    if language == "de":
        lines.append(f"=== ANOMALIEN (letzte {lookback_days} Tage) ===")
    else:
        lines.append(f"=== ANOMALIES (last {lookback_days} days) ===")
    if anomalies:
        for f in anomalies:
            d = scrub_details("anomaly", f.get("details"))
            z = f"{d['z']:.2f}" if "z" in d else "n/a"
            lines.append(f"[{f['ref_date']}] {_label(f, 'metric_a')}: z = {z}")
    else:
        lines.append("–" if language == "de" else "–")
    lines.append("")

    # --- Recovery alerts ---
    alerts = by_kind.get("recovery_alert", [])
    if language == "de":
        lines.append("=== ERHOLUNGSWARNUNG ===")
    else:
        lines.append("=== RECOVERY ALERT ===")
    if alerts:
        for f in alerts:
            d = scrub_details("recovery_alert", f.get("details"))
            parts = []
            if "hrv_z" in d:
                parts.append(f"HRV-z={d['hrv_z']:.2f}")
            if "rhr_z" in d:
                parts.append(f"RHR-z={d['rhr_z']:.2f}")
            if d.get("short_sleep"):
                parts.append("short sleep" if language == "en" else "kurzer Schlaf")
            lines.append(f"[{f['ref_date']}] {', '.join(parts)}")
    else:
        lines.append("–")
    lines.append("")

    # --- Training load ---
    tload = by_kind.get("training_load", [])
    if language == "de":
        lines.append("=== TRAININGSBELASTUNG ===")
    else:
        lines.append("=== TRAINING LOAD ===")
    if tload:
        for f in tload:
            d = scrub_details("training_load", f.get("details"))
            ratio = d.get("ratio", f.get("severity"))
            ratio_str = f"{ratio:.2f}" if ratio is not None else "n/a"
            note_str = f" — {f['note']}" if f.get("note") else ""
            lines.append(f"[{f['ref_date']}] {_label(f, 'metric_a')}: ACWR={ratio_str}{note_str}")
    else:
        lines.append("–")
    lines.append("")

    # --- Correlations ---
    # Layer-2 curation: lead with the informative cross-domain links and, when a
    # cap is set, drop the long tail of expected/structural pairs (summarised as
    # a count) so the model isn't swamped by them.
    corrs = sorted(
        by_kind.get("correlation", []),
        key=lambda f: report_priority(f.get("metric_a"), f.get("metric_b"), f.get("coefficient"), f.get("lag_days")),
        reverse=True,
    )
    omitted = 0
    if max_correlations and len(corrs) > max_correlations:
        omitted = len(corrs) - max_correlations
        corrs = corrs[:max_correlations]
    if language == "de":
        lines.append("=== KORRELATIONEN ===")
    else:
        lines.append("=== CORRELATIONS ===")
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
            lines.append(f"{_label(f, 'metric_a')} → {_label(f, 'metric_b')} ({lag_label}): r={coef}, p_adj={p}, N={n}")
    else:
        lines.append("–")
    if omitted:
        if language == "de":
            lines.append(f"(+{omitted} weitere, niedrigere Priorität — erwartbar/strukturell, ausgelassen)")
        else:
            lines.append(f"(+{omitted} more, lower priority — expected/structural, omitted)")
    lines.append("")

    # --- Trends ---
    trends = by_kind.get("trend", [])
    if language == "de":
        lines.append("=== TRENDS ===")
    else:
        lines.append("=== TRENDS ===")
    if trends:
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
            lines.append(
                f"{_label(f, 'metric_a')}{window}: {slope_str}, "
                + ("Stärke=" if language == "de" else "strength=")
                + strength_str
            )
    else:
        lines.append("–")
    lines.append("")

    # --- Seasonality ---
    seasons = by_kind.get("seasonality", [])
    if language == "de":
        lines.append("=== SAISONALITÄT ===")
    else:
        lines.append("=== SEASONALITY ===")
    if seasons:
        for f in seasons:
            d = scrub_details("seasonality", f.get("details"))
            peak = _month_name(d.get("peak_month", 0), language)
            trough = _month_name(d.get("trough_month", 0), language)
            strength = d.get("strength", f.get("severity"))
            strength_str = f"{strength:.2f}" if strength is not None else "n/a"
            uncertain = ""
            if not d.get("phase_confident", True):
                uncertain = " (Phase unsicher)" if language == "de" else " (phase uncertain)"
            lines.append(
                f"{_label(f, 'metric_a')}: "
                + ("Peak=" if language == "en" else "Hochpunkt=")
                + f"{peak}, "
                + ("Trough=" if language == "en" else "Tiefpunkt=")
                + f"{trough}, "
                + ("strength=" if language == "en" else "Stärke=")
                + f"{strength_str}{uncertain}"
            )
    else:
        lines.append("–")
    lines.append("")

    # --- Sleep consistency ---
    consistency = by_kind.get("consistency", [])
    if language == "de":
        lines.append("=== SCHLAF-KONSISTENZ ===")
    else:
        lines.append("=== SLEEP CONSISTENCY ===")
    if consistency:
        for f in consistency:
            d = scrub_details("consistency", f.get("details"))
            std = d.get("std_hours", f.get("severity"))
            std_str = f"{std:.2f}h" if std is not None else "n/a"
            note_str = f.get("note") or ""
            lines.append(f"{_label(f, 'metric_a')}: σ={std_str} — {note_str}")
    else:
        lines.append("–")

    if note:
        lines.append("")
        lines.append("=== NUTZERHINWEIS ===" if language == "de" else "=== USER NOTE ===")
        lines.append(note)

    return "\n".join(lines)


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
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout))

    def generate(self, system_prompt: str, user_message: str) -> str:
        """POST to ``/api/chat`` and return the generated text.

        Raises ``httpx.HTTPError`` on network / HTTP failures.
        Raises ``ValueError`` if the response shape is unexpected.
        """
        url = f"{self._base_url}/api/chat"
        payload = {
            "model": self._model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }
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


def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    app_config = load_config(settings.config_file)
    cfg: NarrateConfig = app_config.narrate

    # Apply CLI overrides.
    lookback_days = args.lookback_days if args.lookback_days is not None else cfg.lookback_days
    output_dir = args.output_dir if args.output_dir is not None else (Path(settings.config_file).parent / "narration")

    if not cfg.ollama_url:
        log.error("narrate.ollama_url is not set — add it to config.yaml (e.g. ollama_url: http://192.168.1.100:11434)")
        return 1

    from .database import SessionLocal

    db = SessionLocal()
    try:
        findings = load_findings(db, lookback_days)
    finally:
        db.close()

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

    client = OllamaClient(cfg.ollama_url, cfg.model, timeout=float(cfg.timeout_s))
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
