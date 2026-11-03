"""Every SQL query of the shipped Grafana dashboards runs against the current schema.

The CI ``grafana`` job proves the dashboards provision; this proves their
queries still fit the tables and views after a migration. Each panel query and
each query variable is rendered the way Grafana's PostgreSQL datasource renders
it (template variables, then the ``$__time*`` macros) and executed on the
migrated test database, seeded with the sample HAE payload. A query that
references a dropped or renamed column, view or function fails here instead of
showing an empty, red panel after an update.

A new macro or variable in a dashboard fails with a clear message until it is
taught to ``_render`` / ``_FALLBACK``, so nothing is silently left unchecked.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.ingest import parse_payload, store

DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "grafana" / "dashboards"
DASHBOARDS = sorted(DASHBOARD_DIR.glob("*.json"))

# The dashboard time range: wide enough to include the sample payload's days.
TIME_FROM = dt.datetime(2000, 1, 1, tzinfo=dt.UTC)
TIME_TO = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)

# Value of a query variable whose query returns no row on the seeded data
# (``value``, ``text``). Every variable a dashboard uses needs one here.
_FALLBACK = {
    "tz": ("UTC", "UTC"),
    "category": ("other", "other"),
    "metric": ("heart_rate", "Heart rate"),
    "unit": ("", ""),
    "workout": ("00000000-0000-0000-0000-000000000000", "Workout"),
}

_VARIABLE = re.compile(r"\$\{(\w+)(?::(\w+))?\}|\$(?!__)(\w+)")
_MACRO = re.compile(r"\$__(\w+)\(")


@dataclass(frozen=True)
class Query:
    dashboard: str
    title: str
    sql: str
    format: str | None  # "time_series" / "table"; None for a variable query

    @property
    def id(self) -> str:
        return f"{self.dashboard}:{self.title}"


def _panel_queries(dashboard: str, panels: list[dict]) -> list[Query]:
    out = []
    for panel in panels:
        out += _panel_queries(dashboard, panel.get("panels", []))  # collapsed rows
        for target in panel.get("targets", []):
            if target.get("rawSql"):
                title = f"{panel.get('title') or panel.get('type')}:{target.get('refId', '?')}"
                out.append(Query(dashboard, title, target["rawSql"], target.get("format")))
    return out


def _variable_sql(variable: dict) -> str | None:
    query = variable.get("query")
    if isinstance(query, dict):
        query = query.get("rawSql") or query.get("query")
    return query or None


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _all_queries() -> list[Query]:
    out = []
    for path in DASHBOARDS:
        board = _load(path)
        for variable in board.get("templating", {}).get("list", []):
            if variable.get("type") == "query" and _variable_sql(variable):
                out.append(Query(path.name, f"variable {variable['name']}", _variable_sql(variable), None))
        out += _panel_queries(path.name, board.get("panels", []))
    return out


def _grafana_time(value: dt.datetime) -> str:
    return "'" + value.strftime("%Y-%m-%dT%H:%M:%SZ") + "'"


def _macro_argument(sql: str, start: int) -> tuple[str, int]:
    """The argument of a macro whose ``(`` is at ``start``, and the index after
    its closing ``)``; arguments nest parentheses, e.g.
    ``$__timeFilter((day::timestamp AT TIME ZONE 'UTC'))``."""
    depth = 0
    for i in range(start, len(sql)):
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                return sql[start + 1 : i].strip(), i + 1
    raise AssertionError(f"unbalanced parentheses after {sql[:start]!r}")


def _render_macros(sql: str) -> str:
    """The ``$__time*`` macros as the PostgreSQL datasource expands them."""
    out, pos = [], 0
    while match := _MACRO.search(sql, pos):
        name = match.group(1)
        argument, end = _macro_argument(sql, match.end() - 1)
        if name == "timeFilter":
            expansion = f"{argument} BETWEEN {_grafana_time(TIME_FROM)} AND {_grafana_time(TIME_TO)}"
        elif name == "timeFrom" and not argument:
            expansion = _grafana_time(TIME_FROM)
        elif name == "timeTo" and not argument:
            expansion = _grafana_time(TIME_TO)
        else:
            raise AssertionError(f"macro $__{name} is not rendered by this test yet; add it to _render_macros")
        out += [sql[pos : match.start()], expansion]
        pos = end
    if "$__" in sql[pos:]:
        raise AssertionError(f"unknown macro in {sql[pos:]!r}; add it to _render_macros")
    return "".join(out) + sql[pos:]


def _render(sql: str, values: dict[str, tuple[str, str]]) -> str:
    def variable(match: re.Match) -> str:
        name = match.group(1) or match.group(3)
        if name not in values:
            raise AssertionError(f"variable ${name} has no value; add it to _FALLBACK")
        value, text = values[name]
        fmt = match.group(2)
        if fmt in (None, "raw"):
            return value
        if fmt == "text":
            return text
        raise AssertionError(f"variable format ${{{name}:{fmt}}} is not rendered by this test yet")

    return _render_macros(_VARIABLE.sub(variable, sql))


def _execute(db_conn, sql: str) -> tuple[list[str], list[tuple]]:
    # The raw driver cursor, without bind parameters: dashboard SQL may contain
    # ``%`` and ``:name`` that SQLAlchemy's text() or a parameter style would
    # misread. A savepoint keeps one failing statement from aborting the rest.
    with db_conn.connection.driver_connection.cursor() as cur:
        cur.execute("SAVEPOINT dashboard_query")
        try:
            cur.execute(sql)
            columns = [c.name for c in cur.description or []]
            rows = cur.fetchall() if cur.description else []
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT dashboard_query")
            raise
        cur.execute("RELEASE SAVEPOINT dashboard_query")
    return columns, rows


def _variable_values(db_conn, board: dict) -> dict[str, tuple[str, str]]:
    """Each variable's value as Grafana would pick it: the first row its query
    returns on the seeded data (in dashboard order, so a variable can use the
    ones above it), else the fallback; a textbox keeps its default."""
    values: dict[str, tuple[str, str]] = {}
    for variable in board.get("templating", {}).get("list", []):
        name = variable["name"]
        if variable.get("type") == "textbox":
            default = str(variable.get("query") or "")
            values[name] = (default, default)
        elif variable.get("type") == "query" and _variable_sql(variable):
            columns, rows = _execute(db_conn, _render(_variable_sql(variable), _FALLBACK | values))
            if rows:
                row = dict(zip(columns, rows[0], strict=True))
                value = str(row.get("__value", rows[0][0]))
                values[name] = (value, str(row.get("__text", value)))
            elif name in _FALLBACK:
                values[name] = _FALLBACK[name]
    return values


@pytest.fixture
def seeded_conn(db_conn, sample_payload):
    from sqlalchemy.orm import Session

    session = Session(bind=db_conn, join_transaction_mode="create_savepoint")
    store(session, parse_payload(sample_payload))
    session.commit()
    session.close()
    return db_conn


def test_dashboards_are_found():
    # Guards against the glob silently matching nothing (moved directory).
    assert DASHBOARDS, f"no dashboards found in {DASHBOARD_DIR}"


@pytest.mark.parametrize("query", _all_queries(), ids=lambda q: q.id)
def test_dashboard_query_runs(seeded_conn, query):
    values = _variable_values(seeded_conn, _load(DASHBOARD_DIR / query.dashboard))
    columns, _ = _execute(seeded_conn, _render(query.sql, values))
    if query.format == "time_series":
        # Grafana's PostgreSQL datasource needs a column named "time" for this format.
        assert "time" in columns, f"time_series query without a 'time' column: {columns}"
