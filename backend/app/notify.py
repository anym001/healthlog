"""Run-outcome and health-alert push notifications.

Pushes to a Gotify-compatible endpoint (Gotify or PushBits). PushBits
(https://github.com/pushbits/server) relays into a Matrix room and implements
Gotify's message API: ``POST {url}/message?token=<token>`` with a JSON body
``{title, message, priority}``. The application token comes from the
``NOTIFY_TOKEN`` environment variable, never from anywhere persisted, and is
never logged (it travels as a query parameter, so request URLs must not be
logged either).

Three independent sources can notify, selected via ``NOTIFY_EVENTS``:

- ``analysis``  the nightly analysis run outcome (a crash, and — at
                ``NOTIFY_LEVEL=always`` — the clean OK summary).
- ``findings``  health alerts from a run (recent anomalies + recovery alerts).
- ``ingest``    an empty ingest (a problem) and — at ``always`` — each
                successful HAE sync.

Notifications are strictly best-effort: a failed send (or a misconfiguration)
is logged and swallowed — ingestion and analysis never depend on the notifier.
Message content is limited to counters and metric kinds; raw health values
never leave the machine this way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from .logging_config import safe

if TYPE_CHECKING:
    from .analysis import AnalysisResult
    from .appconfig import NotifyConfig

log = logging.getLogger("healthlog.notify")

_MESSAGE_PATH = "/message"

# Gotify priority scale (0-10); >= 8 is "high" and typically bypasses
# client-side muting, which is exactly right for failures and health alerts.
PRIORITY_INFO = 4
PRIORITY_PROBLEM = 8


@dataclass
class Notification:
    title: str
    message: str
    priority: int
    problem: bool


class GotifyNotifier:
    """Minimal client for Gotify-compatible push endpoints."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        verify_tls: bool = True,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not token:
            raise ValueError("NOTIFY_URL is set but NOTIFY_TOKEN is missing")
        self._url = url.rstrip("/")
        self._token = token
        self._client = client or httpx.Client(verify=verify_tls, timeout=timeout)
        log.debug("notifier ready: %s (verify_tls=%s)", self._url, verify_tls)

    def send(self, notification: Notification) -> bool:
        """Push one message; best-effort (False + warning instead of raising)."""
        try:
            response = self._client.post(
                self._url + _MESSAGE_PATH,
                params={"token": self._token},
                json={
                    "title": notification.title,
                    "message": notification.message,
                    "priority": notification.priority,
                },
            )
        except httpx.HTTPError as exc:
            log.warning("notification failed: %s", safe(exc))
            return False
        if response.status_code >= 400:
            log.warning("notification failed: HTTP %d", response.status_code)
            return False
        log.info("notification sent: %s", notification.title)
        return True

    def close(self) -> None:
        self._client.close()


def build_notifier(notify: NotifyConfig) -> GotifyNotifier | None:
    """Create the configured notifier, or None when notifications are off.

    Raises ``ValueError`` when a URL is configured without a token — callers in
    a request/run path use the best-effort dispatchers below, which swallow it.
    """
    if not notify.url:
        return None
    return GotifyNotifier(notify.url, notify.token or "", verify_tls=notify.verify_tls)


# ---------------------------------------------------------------------------
# Composers (pure: data in, Notification out).
# ---------------------------------------------------------------------------


def compose_analysis_run_message(result: AnalysisResult) -> Notification:
    """The clean nightly-analysis summary (sent only at NOTIFY_LEVEL=always)."""
    lines = [
        f"correlations: {result.correlations}",
        f"anomalies: {result.anomalies}",
        f"trends: {result.trends}",
        f"seasonality: {result.seasonality}",
        f"recovery alerts: {result.recovery_alerts}",
        f"consistency: {result.consistency}",
    ]
    return Notification("HealthLog: analysis OK", "\n".join(lines), PRIORITY_INFO, False)


def compose_analysis_crash_message(exc: Exception) -> Notification:
    """Alert for an analysis run that died with an exception."""
    detail = f"{type(exc).__name__}: {exc}"[:300]
    return Notification("HealthLog: analysis failed", detail, PRIORITY_PROBLEM, True)


def compose_findings_message(result: AnalysisResult) -> Notification | None:
    """Health alert for a run that surfaced recent anomalies / recovery alerts.

    Returns None when nothing alert-worthy came out of the run. Correlations,
    trends, seasonality and consistency are background analytics, not alerts.
    """
    alerts = result.anomalies + result.recovery_alerts
    if alerts == 0:
        return None
    lines = [
        f"anomalies: {result.anomalies}",
        f"recovery alerts: {result.recovery_alerts}",
    ]
    return Notification("HealthLog: health alerts", "\n".join(lines), PRIORITY_PROBLEM, True)


def compose_ingest_message(kind: str, metric_rows: int, sleep_rows: int, workout_rows: int) -> Notification:
    """Notification for one HAE ingest. ``kind`` is "stored" or "empty"."""
    if kind == "empty":
        return Notification(
            "HealthLog: empty ingest",
            "a payload was stored but produced no rows",
            PRIORITY_PROBLEM,
            True,
        )
    lines = [
        f"metrics: {metric_rows}",
        f"sleep: {sleep_rows}",
        f"workouts: {workout_rows}",
    ]
    return Notification("HealthLog: data ingested", "\n".join(lines), PRIORITY_INFO, False)


# ---------------------------------------------------------------------------
# Dispatch (best-effort: configuration/network errors never propagate).
# ---------------------------------------------------------------------------


def _send_all(notify: NotifyConfig, messages: list[Notification | None]) -> None:
    """Build the notifier once and push every (non-None) message; swallow all
    errors so a notification can never break the surrounding run."""
    pending = [m for m in messages if m is not None]
    if not pending:
        return
    try:
        notifier = build_notifier(notify)
    except ValueError as exc:
        log.warning("notifications disabled: %s", safe(exc))
        return
    if notifier is None:
        return
    try:
        for message in pending:
            notifier.send(message)
    finally:
        notifier.close()


def notify_analysis(notify: NotifyConfig, result: AnalysisResult) -> None:
    """Dispatch the run summary (``analysis``) and health alerts (``findings``)
    after a successful nightly analysis."""
    events = notify.event_set()
    messages: list[Notification | None] = []
    if "analysis" in events and notify.level == "always":
        messages.append(compose_analysis_run_message(result))
    if "findings" in events:
        messages.append(compose_findings_message(result))
    _send_all(notify, messages)


def notify_analysis_crash(notify: NotifyConfig, exc: Exception) -> None:
    """Alert that the nightly analysis crashed (``analysis`` source, any level)."""
    if "analysis" not in notify.event_set():
        return
    _send_all(notify, [compose_analysis_crash_message(exc)])


def notify_ingest(notify: NotifyConfig, *, metric_rows: int, sleep_rows: int, workout_rows: int) -> None:
    """Notify on an ingest outcome (``ingest`` source). An empty ingest is a
    problem (any level); a non-empty one is routine info (``always`` only)."""
    if "ingest" not in notify.event_set():
        return
    if metric_rows + sleep_rows + workout_rows == 0:
        message = compose_ingest_message("empty", metric_rows, sleep_rows, workout_rows)
    elif notify.level == "always":
        message = compose_ingest_message("stored", metric_rows, sleep_rows, workout_rows)
    else:
        return
    _send_all(notify, [message])
