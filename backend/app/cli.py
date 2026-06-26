"""HealthLog operator CLI (``healthlog`` console script).

A single entry point with subcommands, so operators type a short command
instead of a module path::

    healthlog backfill /config/import
    healthlog backfill --dry-run /config/import
    healthlog analyze                       # run the nightly analysis once now
    healthlog audit                         # read-only data-quality audit
    healthlog narrate                       # generate a weekly health report via Ollama
    healthlog narrate --note "…"            # with an optional focus note
    healthlog narrate --dry-run             # print the model context only (no Ollama call)
    healthlog check-workout-hr              # is intra-workout HR in the archive?
    healthlog rederive-workout-hr           # backfill HR samples from the archive

New operator commands are added as further subparsers here. The installed
console script (see ``pyproject.toml``) maps ``healthlog`` to ``main``;
``python -m app`` resolves here too.
"""

from __future__ import annotations

import argparse
import sys

from . import audit, backfill, diagnostics, narrate, rederive


def _run_analyze(_args: argparse.Namespace) -> int:
    # Imported lazily so `healthlog backfill` doesn't pay the analysis import
    # cost (pandas/statsmodels). Same code path as the scheduled subprocess.
    from . import analysis

    return analysis.main()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="healthlog", description="HealthLog operator CLI.")
    sub = parser.add_subparsers(dest="command", required=True)

    bf = sub.add_parser("backfill", help="bulk-import Apple Health history from disk")
    backfill.add_arguments(bf)
    bf.set_defaults(func=backfill.run)

    ana = sub.add_parser("analyze", help="run the nightly analysis once now")
    ana.set_defaults(func=_run_analyze)

    aud = sub.add_parser("audit", help="read-only data-quality audit (coverage, findings, units)")
    audit.add_arguments(aud)
    aud.set_defaults(func=audit.run)

    whr = sub.add_parser("check-workout-hr", help="report intra-workout HR series in the raw archive")
    diagnostics.add_arguments(whr)
    whr.set_defaults(func=diagnostics.run)

    rwh = sub.add_parser("rederive-workout-hr", help="backfill intra-workout HR samples from the raw archive")
    rederive.add_arguments(rwh)
    rwh.set_defaults(func=rederive.run)

    nar = sub.add_parser("narrate", help="generate a weekly health report via Ollama")
    narrate.add_arguments(nar)
    nar.set_defaults(func=narrate.run)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
