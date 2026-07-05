"""Trusted-proxy resolution (app.proxies) and the audit client-IP resolver it
gates (deps.client_ip).

Forwarded headers (X-Real-IP, X-Forwarded-For) must only be believed when the
immediate peer is a trusted proxy per TRUSTED_PROXIES — otherwise any direct
client could spoof its audit IP and poison a fail2ban feed. Adapted from
pocketlog's trusted-proxy suite.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from app import config
from app.deps import client_ip
from app.proxies import _parse, is_trusted_peer


@pytest.fixture
def trusted(monkeypatch):
    """Set TRUSTED_PROXIES for the test and clear the settings cache."""

    def _set(value: str) -> None:
        monkeypatch.setenv("TRUSTED_PROXIES", value)
        config.get_settings.cache_clear()

    yield _set
    config.get_settings.cache_clear()


def _make_request(*, headers: dict | None = None, client=("203.0.113.7", 5000)) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/ingest",
        "query_string": b"",
        "headers": raw,
        "client": client,
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# _parse / is_trusted_peer
# ---------------------------------------------------------------------------


def test_default_trusts_private_ranges(trusted):
    trusted("")
    assert is_trusted_peer("10.0.0.5")
    assert is_trusted_peer("172.17.0.1")  # Docker bridge
    assert is_trusted_peer("192.168.1.50")
    assert is_trusted_peer("127.0.0.1")
    assert is_trusted_peer("::1")


def test_default_does_not_trust_public_ip(trusted):
    trusted("")
    assert not is_trusted_peer("8.8.8.8")
    assert not is_trusted_peer("203.0.113.7")


def test_wildcard_trusts_everything(trusted):
    trusted("*")
    assert is_trusted_peer("8.8.8.8")
    assert is_trusted_peer("10.0.0.1")


def test_explicit_list_replaces_defaults(trusted):
    trusted("172.20.0.0/16")
    assert is_trusted_peer("172.20.5.5")
    # A private range NOT in the explicit list is no longer trusted.
    assert not is_trusted_peer("10.0.0.5")


def test_invalid_entry_is_ignored():
    networks = _parse("not-an-ip, 192.168.0.0/16")
    assert networks is not None
    assert [str(n) for n in networks] == ["192.168.0.0/16"]


def test_missing_or_garbage_peer_never_trusted(trusted):
    trusted("")
    assert not is_trusted_peer(None)
    assert not is_trusted_peer("")
    assert not is_trusted_peer("testclient")


# ---------------------------------------------------------------------------
# client_ip
# ---------------------------------------------------------------------------


def test_trusted_proxy_x_real_ip_wins(trusted):
    trusted("")
    req = _make_request(
        headers={"x-real-ip": "203.0.113.7", "x-forwarded-for": "198.51.100.9"},
        client=("172.17.0.1", 9000),
    )
    assert client_ip(req) == "203.0.113.7"


def test_trusted_proxy_first_forwarded_hop(trusted):
    trusted("")
    req = _make_request(
        headers={"x-forwarded-for": "203.0.113.7, 172.17.0.1"},
        client=("172.17.0.1", 9000),
    )
    assert client_ip(req) == "203.0.113.7"


def test_untrusted_peer_cannot_spoof_audit_ip(trusted):
    # A direct (public) client sending X-Forwarded-For must NOT relabel
    # itself — the audit line keeps the real peer address.
    trusted("")
    req = _make_request(
        headers={"x-forwarded-for": "10.0.0.1"},
        client=("203.0.113.7", 5000),
    )
    assert client_ip(req) == "203.0.113.7"


def test_garbage_forwarded_value_falls_back_to_peer(trusted):
    trusted("")
    req = _make_request(
        headers={"x-forwarded-for": "not-an-ip"},
        client=("172.17.0.1", 9000),
    )
    assert client_ip(req) == "172.17.0.1"  # INET column: valid IP or None


def test_no_proxy_returns_peer(trusted):
    trusted("")
    req = _make_request(client=("192.168.1.20", 5000))
    assert client_ip(req) == "192.168.1.20"


def test_testclient_placeholder_maps_to_none(trusted):
    trusted("")
    req = _make_request(client=("testclient", 5000))
    assert client_ip(req) is None
