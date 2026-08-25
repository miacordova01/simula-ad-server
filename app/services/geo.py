"""IP -> country resolution via the bundled MaxMind database.

The reader is opened once and shared: `maxminddb` memory-maps the file and its
`get()` is thread-safe and effectively free, so there is no reason to reopen it
per request or push it onto a thread pool.
"""

from __future__ import annotations

import ipaddress
import logging
from pathlib import Path
from typing import Any

import maxminddb

log = logging.getLogger(__name__)


class GeoResolver:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._reader: Any | None = None
        if db_path.exists():
            try:
                self._reader = maxminddb.open_database(str(db_path))
                log.info("geoip database loaded from %s", db_path)
            except Exception:
                log.exception("failed to open geoip database at %s", db_path)
        else:
            log.warning("geoip database not found at %s; geo will resolve to None", db_path)

    @property
    def available(self) -> bool:
        return self._reader is not None

    def country(self, ip: str | None) -> str | None:
        """ISO-3166 alpha-2 for an IP, or None when it cannot be resolved.

        Prefers `country` (where MaxMind believes the IP physically is) over
        `registered_country` (where the block's owner is registered). They
        disagree for several of the provided test IPs -- 2.125.160.217 is
        GB/FR and 89.160.20.113 is SE/DE -- and geo targeting cares about the
        user's location, not the ISP's paperwork.
        """
        if not ip or self._reader is None:
            return None
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            log.debug("not a valid ip: %r", ip)
            return None
        try:
            rec = self._reader.get(ip)
        except Exception:
            log.exception("geoip lookup failed for %s", ip)
            return None
        if not isinstance(rec, dict):
            return None
        for key in ("country", "registered_country", "represented_country"):
            iso = (rec.get(key) or {}).get("iso_code")
            if iso:
                return str(iso).upper()
        return None

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None


# Private ranges never resolve to a country, so skip them when walking
# X-Forwarded-For rather than returning None for the whole chain.
def _is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_link_local)


def client_ip(
    x_forwarded_for: str | None,
    x_real_ip: str | None,
    peer: str | None,
) -> str | None:
    """Best-effort client IP.

    `X-Forwarded-For` is client-controlled and trivially spoofed. Behind a
    trusted proxy (Cloud Run, an ALB) the platform appends the real peer, so
    the correct read is the right-most address you do not trust -- but that
    requires knowing your proxy topology. Here we take the left-most PUBLIC
    address, which is the conventional behaviour and is what makes the
    documented test IPs work. In production this must be replaced with a
    trusted-proxy count; noted in the README as a known limitation.
    """
    if x_forwarded_for:
        for part in x_forwarded_for.split(","):
            candidate = part.strip()
            if candidate and _is_public(candidate):
                return candidate
    if x_real_ip and _is_public(x_real_ip.strip()):
        return x_real_ip.strip()
    if peer and _is_public(peer):
        return peer
    # Fall back to the raw first value so the serve log records what we saw,
    # even when it is a private address that will not geo-resolve.
    if x_forwarded_for:
        first = x_forwarded_for.split(",")[0].strip()
        if first:
            return first
    return peer
