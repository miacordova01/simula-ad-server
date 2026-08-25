"""GeoIP resolution and user-agent classification."""

from __future__ import annotations

import pytest

from app.models.common import OS
from app.services.geo import GeoResolver, client_ip
from app.services.useragent import parse_device

IOS = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
ANDROID = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")
WINDOWS = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


class TestGeo:
    @pytest.fixture
    def geo(self, geoip_path):
        return GeoResolver(geoip_path)

    # The IPs documented in the provided README.
    @pytest.mark.parametrize("ip,country", [
        ("214.78.0.1", "US"), ("2.125.160.217", "GB"), ("89.160.20.113", "SE"),
        ("175.16.199.1", "CN"), ("202.196.224.1", "PH"), ("67.43.156.1", "BT"),
    ])
    def test_documented_test_ips(self, geo, ip, country):
        assert geo.country(ip) == country

    @pytest.mark.parametrize("bad", [None, "", "not-an-ip", "999.999.999.999", "1.2.3"])
    def test_invalid_input_returns_none(self, geo, bad):
        assert geo.country(bad) is None

    def test_unknown_public_ip_returns_none(self, geo):
        """The bundled db is MaxMind's test fixture; 8.8.8.8 is not in it."""
        assert geo.country("8.8.8.8") is None

    def test_missing_database_degrades_quietly(self, tmp_path):
        g = GeoResolver(tmp_path / "nope.mmdb")
        assert g.available is False
        assert g.country("214.78.0.1") is None


class TestClientIp:
    def test_prefers_first_public_xff_entry(self):
        assert client_ip("214.78.0.1, 10.0.0.1", None, "127.0.0.1") == "214.78.0.1"

    def test_skips_private_prefix_hops(self):
        assert client_ip("10.0.0.1, 192.168.1.1, 214.78.0.1", None, None) == "214.78.0.1"

    def test_falls_back_to_real_ip_header(self):
        assert client_ip(None, "214.78.0.1", "127.0.0.1") == "214.78.0.1"

    def test_falls_back_to_peer(self):
        assert client_ip(None, None, "214.78.0.1") == "214.78.0.1"

    def test_returns_private_peer_when_nothing_public(self):
        assert client_ip(None, None, "127.0.0.1") == "127.0.0.1"

    def test_all_empty(self):
        assert client_ip(None, None, None) is None


class TestUserAgent:
    @pytest.mark.parametrize("ua,expected", [
        (IOS, OS.ios), (ANDROID, OS.android), (WINDOWS, OS.web),
        ("", OS.unknown), (None, OS.unknown), ("   ", OS.unknown),
        ("garbage-not-a-ua", OS.unknown),
    ])
    def test_os_classification(self, ua, expected):
        assert parse_device(ua).os == expected

    def test_device_family_extracted(self):
        assert parse_device(ANDROID).device_family == "Pixel 8"
        assert parse_device(IOS).device_family == "iPhone"

    def test_ipad_counts_as_ios(self):
        ipad = ("Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
        assert parse_device(ipad).os == OS.ios
