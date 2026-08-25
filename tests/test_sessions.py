"""Session resolution and the 30s inactivity rule."""

from __future__ import annotations

import pytest

from app.services.sessions import SessionService


@pytest.fixture
def svc(fake_redis):
    return SessionService(fake_redis, idle_ttl_s=30)


class TestUserResolution:
    def test_ppid_preferred_over_ip(self):
        assert SessionService.user_key("u1", "1.2.3.4") == ("ppid:u1", "ppid")

    def test_falls_back_to_ip(self):
        assert SessionService.user_key(None, "1.2.3.4") == ("ip:1.2.3.4", "ip")

    def test_blank_ppid_falls_back_to_ip(self):
        assert SessionService.user_key("   ", "1.2.3.4") == ("ip:1.2.3.4", "ip")

    def test_namespaces_prevent_ppid_spoofing_an_ip(self):
        """A caller passing ppid='1.2.3.4' must not land on that IP's session."""
        by_ppid, _ = SessionService.user_key("1.2.3.4", None)
        by_ip, _ = SessionService.user_key(None, "1.2.3.4")
        assert by_ppid != by_ip

    def test_anonymous_callers_get_distinct_identities(self):
        a, _ = SessionService.user_key(None, None)
        b, _ = SessionService.user_key(None, None)
        assert a != b


class TestSessionLifecycle:
    async def test_same_user_reuses_session_within_window(self, svc):
        s1 = await svc.resolve_or_create("u1", None)
        s2 = await svc.resolve_or_create("u1", None)
        assert s1.session_id == s2.session_id

    async def test_different_users_get_different_sessions(self, svc):
        s1 = await svc.resolve_or_create("u1", None)
        s2 = await svc.resolve_or_create("u2", None)
        assert s1.session_id != s2.session_id

    async def test_new_session_after_inactivity(self, svc, fake_redis):
        """Expiry of the Redis key IS the 30s rule -- simulate it by deleting."""
        s1 = await svc.resolve_or_create("u1", None)
        await fake_redis.delete("sess:user:ppid:u1")
        s2 = await svc.resolve_or_create("u1", None)
        assert s2.session_id != s1.session_id

    async def test_ttl_is_the_configured_idle_window(self, svc, fake_redis):
        await svc.resolve_or_create("u1", None)
        assert await fake_redis.ttl("sess:user:ppid:u1") == 30

    async def test_touch_refreshes_the_window(self, svc, fake_redis):
        """A serve counts as activity and must extend the window."""
        s = await svc.resolve_or_create("u1", None)
        await fake_redis.expire("sess:user:ppid:u1", 5)
        assert await fake_redis.ttl("sess:user:ppid:u1") == 5
        await svc.touch(s)
        assert await fake_redis.ttl("sess:user:ppid:u1") == 30

    async def test_touch_increments_serve_count(self, svc):
        s = await svc.resolve_or_create("u1", None)
        await svc.touch(s)
        await svc.touch(s)
        assert (await svc.get(s.session_id)).serve_count == 2

    async def test_get_unknown_session(self, svc):
        assert await svc.get("sess_nope") is None

    async def test_resolved_from_recorded(self, svc):
        assert (await svc.resolve_or_create("u1", None)).resolved_from == "ppid"
        assert (await svc.resolve_or_create(None, "9.9.9.9")).resolved_from == "ip"
