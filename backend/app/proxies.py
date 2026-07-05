"""Trusted reverse-proxy resolution.

Decides one question: *is the immediate peer a reverse proxy we trust to set
``X-Forwarded-*`` headers?* The audit client-IP resolver (``deps.client_ip``)
uses it to decide whether to believe ``X-Real-IP`` / ``X-Forwarded-For`` for
the audit trail — without the check, any direct client could spoof its audit
IP (and poison a fail2ban feed) simply by sending the header itself.

``TRUSTED_PROXIES`` — comma-separated IPs/CIDRs, or ``*`` for all:
    empty (default)  trust the standard private and loopback ranges, so the
                     common "container on a private Docker/LAN network behind
                     a reverse proxy" deployment works without configuration.
    explicit list    trust exactly those networks (replaces the defaults) —
                     e.g. ``172.16.0.0/12,192.168.1.1``.
    ``*``            trust every peer; fine for single-proxy setups where the
                     container port is not reachable from untrusted networks.

The resolved trust governs the audit IP only — never authorization. A spoofed
header can at worst mislabel an audit log line, and only from a peer inside a
trusted network. (Adapted from pocketlog's ``app/proxies.py``.)
"""

from __future__ import annotations

import ipaddress
import logging
from functools import lru_cache

from .config import get_settings

log = logging.getLogger("healthlog.api")

_Network = ipaddress.IPv4Network | ipaddress.IPv6Network

# Standard private + loopback ranges trusted when TRUSTED_PROXIES is unset.
# Mirrors the defaults reverse proxies assume for a typical home-server /
# Docker deployment.
_PRIVATE_DEFAULTS = (
    "127.0.0.0/8",  # IPv4 loopback
    "10.0.0.0/8",  # RFC 1918
    "172.16.0.0/12",  # RFC 1918 (Docker bridge range)
    "192.168.0.0/16",  # RFC 1918
    "::1/128",  # IPv6 loopback
    "fe80::/10",  # IPv6 link-local
)


@lru_cache(maxsize=4)
def _parse(raw: str) -> tuple[_Network, ...] | None:
    """Parse a ``TRUSTED_PROXIES`` value.

    Returns ``None`` for the wildcard ``*`` (trust all), otherwise the tuple
    of networks: the explicit entries when given, else the private-range
    defaults. Cached per raw string so an invalid entry warns once, not on
    every request.
    """
    raw = raw.strip()
    if raw == "*":
        return None  # trust all
    parts = [p.strip() for p in raw.split(",") if p.strip()] if raw else list(_PRIVATE_DEFAULTS)
    networks: list[_Network] = []
    for part in parts:
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("TRUSTED_PROXIES: invalid entry %r ignored", part)
    return tuple(networks)


def is_trusted_peer(peer: str | None) -> bool:
    """True if ``peer`` (an IP string) is a trusted reverse proxy and may set
    ``X-Forwarded-*`` headers.

    The wildcard config trusts everyone; a missing or unparseable peer is
    never trusted.
    """
    networks = _parse(get_settings().trusted_proxies)
    if networks is None:
        return True  # wildcard
    if not peer:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in networks)
