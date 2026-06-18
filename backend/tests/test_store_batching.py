"""Bulk insert must chunk under Postgres' 65535-parameter limit and de-dup keys."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from app.ingest import ParsedPayload, store
from app.models import MetricSample

UTC = dt.UTC


def _row(metric: str, time: dt.datetime, qty: float, source: str = "") -> dict:
    return {
        "time": time,
        "metric": metric,
        "source": source,
        "unit": "count",
        "qty": qty,
        "vmin": None,
        "vavg": None,
        "vmax": None,
        "n": None,
    }


def _count(db) -> int:
    return db.execute(select(func.count()).select_from(MetricSample)).scalar_one()


def test_store_chunks_a_batch_over_the_parameter_limit(db):
    # 8000 rows * 9 columns = 72000 bound params > 65535: a single INSERT would
    # fail, so store() must chunk.
    base = dt.datetime(2020, 1, 1, tzinfo=UTC)
    rows = [_row("step_count", base + dt.timedelta(hours=i), float(i)) for i in range(8000)]

    store(db, ParsedPayload(metric_rows=rows))
    db.flush()

    assert _count(db) == 8000


def test_store_dedupes_duplicate_keys_last_wins(db):
    t = dt.datetime(2020, 1, 1, 12, tzinfo=UTC)
    store(db, ParsedPayload(metric_rows=[_row("x", t, 1.0), _row("x", t, 2.0)]))
    db.flush()

    got = db.execute(select(MetricSample).where(MetricSample.metric == "x")).scalars().all()
    assert len(got) == 1
    assert got[0].qty == 2.0
