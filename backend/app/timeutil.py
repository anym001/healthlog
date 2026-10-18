"""Pure date/time helpers. No DB, no app state — unit-tested directly."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from zoneinfo import ZoneInfo

# HAE timestamps look like: "2026-06-09 00:00:00 +0200"
_HAE_FORMAT = "%Y-%m-%d %H:%M:%S %z"


def parse_hae_datetime(value: object) -> dt.datetime | None:
    """Parse an HAE timestamp into a tz-aware datetime, or None.

    Tolerant by design: a missing, non-string, or malformed value yields None.
    Ingest calls this for every timestamp in a payload, and a single bad
    sample must degrade to a skipped point — never abort the whole payload.

    Most HAE sections use ``2026-06-15 12:28:00 +0200``; ``stateOfMind`` ships
    ISO 8601 (``2026-06-15T10:28:00Z``), accepted as a fallback. A timestamp
    without an offset is rejected rather than guessed.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.strptime(value.strip(), _HAE_FORMAT)
    except ValueError:
        pass
    try:
        parsed = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def local_day(ts: dt.datetime, tz: str) -> dt.date:
    """The calendar day ``ts`` falls on in the given local timezone.

    All daily aggregation rests on this: a sample at 23:30 UTC belongs to the
    *next* day in Europe/Vienna, so we convert before taking the date.
    """
    return ts.astimezone(ZoneInfo(tz)).date()


def offset_mismatches(timestamps: Iterable[dt.datetime], tz: str) -> list[dt.datetime]:
    """Timestamps whose own UTC offset differs from ``tz``'s offset at that instant.

    HAE stamps every sample with the phone's local offset (``... -0400``). If
    those offsets disagree with the configured ``TZ``, the daily grid is shifted
    against the user's real days. Compared per instant, so DST transitions on
    either side are handled. Naive timestamps are ignored.
    """
    zone = ZoneInfo(tz)
    return [ts for ts in timestamps if ts.utcoffset() is not None and ts.utcoffset() != ts.astimezone(zone).utcoffset()]
