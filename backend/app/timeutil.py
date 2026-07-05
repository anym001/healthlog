"""Pure date/time helpers. No DB, no app state — unit-tested directly."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

# HAE timestamps look like: "2026-06-09 00:00:00 +0200"
_HAE_FORMAT = "%Y-%m-%d %H:%M:%S %z"


def parse_hae_datetime(value: object) -> dt.datetime | None:
    """Parse an HAE timestamp into a tz-aware datetime, or None.

    Tolerant by design: a missing, non-string, or malformed value yields None.
    Ingest calls this for every timestamp in a payload, and a single bad
    sample must degrade to a skipped point — never abort the whole payload.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.strptime(value.strip(), _HAE_FORMAT)
    except ValueError:
        return None


def local_day(ts: dt.datetime, tz: str) -> dt.date:
    """The calendar day ``ts`` falls on in the given local timezone.

    All daily aggregation rests on this: a sample at 23:30 UTC belongs to the
    *next* day in Europe/Vienna, so we convert before taking the date.
    """
    return ts.astimezone(ZoneInfo(tz)).date()
