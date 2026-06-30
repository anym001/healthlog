"""Shared scaffolding for the operator CLI commands.

Every ``healthlog`` subcommand — and its standalone ``python -m app.X`` twin —
opens with the same boilerplate: load settings + configure logging, then open a
``SessionLocal`` it is responsible for closing, and (for the module twin) wrap a
single ``add_arguments``/``run`` pair in a one-command argparse parser. These
helpers keep that scaffolding in one place so each command module carries only
its own logic.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from .config import Settings, get_settings
from .logging_config import configure_logging

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def bootstrap() -> Settings:
    """Load settings and configure logging — the opening of every command's ``run``.

    Returns the ``Settings`` so callers that need ``config_file``/``local_tz``
    don't have to call :func:`get_settings` a second time.
    """
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    return settings


@contextmanager
def db_session() -> Iterator[Session]:
    """Yield a ``SessionLocal`` and guarantee it is closed.

    ``SessionLocal`` is imported lazily so ``--help`` works without a configured
    ``DATABASE_URL`` and command modules don't pay the DB import cost up front.
    """
    from .database import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def module_main(
    add_arguments: Callable[[argparse.ArgumentParser], None],
    run: Callable[[argparse.Namespace], int],
    *,
    prog: str,
    description: str,
    argv: list[str] | None = None,
) -> int:
    """Run one command as ``python -m app.X``: build a one-command parser, dispatch to ``run``.

    The shared ``healthlog`` CLI (``cli.py``) registers the same ``add_arguments``
    as a subparser; this is the standalone-module twin of that path.
    """
    parser = argparse.ArgumentParser(prog=prog, description=description)
    add_arguments(parser)
    return run(parser.parse_args(argv))
