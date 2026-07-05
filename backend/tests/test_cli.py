"""Operator CLI: parser wiring, required arguments, error exits."""

from __future__ import annotations

import pytest

from app.cli import build_parser, main


def test_no_command_exits_with_usage_error():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2  # argparse usage error, not a crash


def test_unknown_command_exits_with_usage_error():
    with pytest.raises(SystemExit) as exc:
        main(["no-such-command"])
    assert exc.value.code == 2


def test_backfill_requires_a_path():
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["backfill"])
    assert exc.value.code == 2


def test_every_subcommand_is_wired_to_a_runner():
    parser = build_parser()
    # Minimal valid argv per subcommand; each must parse and carry a func.
    for argv in (
        ["backfill", "/config/import"],
        ["analyze"],
        ["audit"],
        ["check-workout-hr"],
        ["rederive-workout-hr"],
        ["narrate"],
    ):
        args = parser.parse_args(argv)
        assert callable(args.func), argv[0]


def test_narrate_flags_parse():
    args = build_parser().parse_args(["narrate", "--dry-run", "--lookback-days", "14", "--note", "focus"])
    assert args.dry_run is True
    assert args.lookback_days == 14
    assert args.note == "focus"


def test_backfill_flags_parse():
    args = build_parser().parse_args(["backfill", "--dry-run", "--glob", "*.json.gz", "a", "b"])
    assert args.dry_run is True
    assert args.glob == "*.json.gz"
    assert args.paths == ["a", "b"]
