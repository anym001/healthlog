"""The ``narrate`` command: load findings, build context, call Ollama, write the report."""

from __future__ import annotations

import argparse
import datetime as dt
import logging
from pathlib import Path

import httpx

from ..appconfig import NarrateConfig, load_config
from ..cli_support import bootstrap, db_session
from ..logging_config import safe
from .client import OllamaClient
from .context import build_context
from .loader import load_findings
from .prompts import _system_prompt

log = logging.getLogger("healthlog.narrate")

# Per-report defaults, applied when neither the CLI flag nor config.yaml sets
# a value: the status check is short, the reviews get room; the monthly report
# widens the alert lookback to its 28-day window.
REPORT_MAX_WORDS = {"status": 300, "weekly": 700, "monthly": 1000}
REPORT_LOOKBACK_DAYS = {"status": 7, "weekly": 7, "monthly": 28}


def write_report(report: str, output_dir: str | Path, report_date: dt.date, report_type: str = "status") -> Path:
    """Write the report to ``<output_dir>/YYYY-MM-DD[-<type>].md``.

    The status report keeps the historical bare-date filename; weekly and
    monthly reports get a suffix, so different report types produced on the
    same day don't overwrite each other.
    """
    p = Path(output_dir)
    p.mkdir(parents=True, exist_ok=True)
    suffix = "" if report_type == "status" else f"-{report_type}"
    path = p / f"{report_date}{suffix}.md"
    path.write_text(report, encoding="utf-8")
    return path


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
        "--language",
        choices=("de", "en"),
        default=None,
        help="override narrate.language from config.yaml for this report",
    )
    parser.add_argument(
        "--audience",
        choices=("simple", "standard", "expert"),
        default=None,
        help="override narrate.audience from config.yaml for this report "
        "(how much gets explained — the findings are identical at every level)",
    )
    parser.add_argument(
        "--max-words",
        type=int,
        default=None,
        metavar="N",
        help="override narrate.max_words from config.yaml for this report",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--report",
        choices=("status", "weekly", "monthly"),
        default=None,
        help="report type (default: narrate.report from config.yaml, falling back to status): "
        "status = short exception check over the lookback window; "
        "weekly = week review leading with the descriptive WEEK sections; "
        "monthly = month review over rolling 28-day windows with a week-by-week course",
    )
    mode.add_argument(
        "--weekly",
        action="store_const",
        const="weekly",
        dest="report",
        help="shorthand for --report weekly",
    )
    mode.add_argument(
        "--monthly",
        action="store_const",
        const="monthly",
        dest="report",
        help="shorthand for --report monthly",
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

    # Apply CLI overrides; lookback and word budget fall back per report type
    # when neither the flag nor config.yaml pins them.
    report_type = args.report if args.report is not None else cfg.report
    if args.lookback_days is not None:
        lookback_days = args.lookback_days
    else:
        lookback_days = cfg.lookback_days if cfg.lookback_days is not None else REPORT_LOOKBACK_DAYS[report_type]
    output_dir = args.output_dir if args.output_dir is not None else (Path(settings.config_file).parent / "narration")
    language = args.language if args.language is not None else cfg.language
    audience = args.audience if args.audience is not None else cfg.audience
    if args.max_words is not None:
        max_words = args.max_words
    else:
        max_words = cfg.max_words if cfg.max_words is not None else REPORT_MAX_WORDS[report_type]

    # A real run needs Ollama; a dry run only renders the findings context (no
    # model call), so it must work even when no endpoint is configured.
    if not args.dry_run and not cfg.ollama_url:
        log.error("narrate.ollama_url is not set — add it to config.yaml (e.g. ollama_url: http://192.168.1.100:11434)")
        return 1

    with db_session() as db:
        findings = load_findings(db, lookback_days, report=report_type)

    log.info("narrate: loaded %d findings (lookback_days=%d, report=%s)", len(findings), lookback_days, report_type)

    today = dt.date.today()
    context = build_context(
        findings,
        lookback_days,
        today,
        note=args.note,
        language=language,
        max_correlations=cfg.max_correlations,
        report=report_type,
    )

    if args.dry_run:
        # Inspect the exact text the model would receive, deterministically and
        # without contacting Ollama — the "data -> report" bridge, minus the LLM.
        print(context)
        log.info("narrate --dry-run: rendered context for %d findings, no model call made", len(findings))
        return 0

    client = OllamaClient(cfg.ollama_url, cfg.model, timeout=float(cfg.timeout_s), thinking=cfg.thinking)
    try:
        report = client.generate(_system_prompt(language, audience, max_words, report=report_type), context)
    except httpx.HTTPError as exc:
        log.error("ollama call failed: %s", safe(str(exc)))
        return 1
    except ValueError as exc:
        log.error("ollama response error: %s", safe(str(exc)))
        return 1
    finally:
        client.close()

    path = write_report(report, output_dir, today, report_type)
    print(report)
    log.info("narration written to %s", path)
    return 0
