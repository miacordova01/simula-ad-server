"""Session resolution.

"New session only after 30s of inactivity" is exactly a Redis key with a 30s TTL
that gets refreshed on each serve. So the store's own expiry IS the rule - no
sweeper job, no last_seen comparison, no clock skew.

User is ppid if given, else IP. The two live in separate keyspaces so someone
can't pass ppid="1.2.3.4" and hijack that IP's session.
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis

from ..models.common import new_id
from ..models.session import Session

log = logging.getLogger(__name__)


class SessionService:
    def __init__(self, redis: aioredis.Redis, idle_ttl_s: int = 30) -> None:
        self.redis = redis
        self.idle_ttl_s = idle_ttl_s

    @staticmethod
    def user_key(ppid: str | None, ip: str | None) -> tuple[str, str]:
        """Resolve caller -> (user_key, how). Namespaced to prevent spoofing."""
        if ppid and ppid.strip():
            return f"ppid:{ppid.strip()}", "ppid"
        if ip:
            return f"ip:{ip}", "ip"
        # No ppid and no usable IP: mint a throwaway identity rather than
        # bucketing every anonymous caller into one shared session.
        return f"anon:{new_id('a', 6)}", "anonymous"

    @staticmethod
    def _key(user_key: str) -> str:
        return f"sess:user:{user_key}"

    @staticmethod
    def _sess_key(session_id: str) -> str:
        return f"sess:id:{session_id}"

    async def resolve_or_create(self, ppid: str | None, ip: str | None) -> Session:
        """Return the live session for this user, or mint a new one."""
        user_key, how = self.user_key(ppid, ip)
        key = self._key(user_key)

        existing = await self.redis.get(key)
        if existing:
            # Touch: any resolve counts as activity and extends the window.
            await self.redis.expire(key, self.idle_ttl_s)
            sess = await self.get(existing)
            if sess is not None:
                await self.redis.expire(self._sess_key(existing), self.idle_ttl_s * 60)
                return sess

        session = Session(user_key=user_key, resolved_from=how)
        pipe = self.redis.pipeline()
        pipe.set(key, session.session_id, ex=self.idle_ttl_s)
        pipe.hset(
            self._sess_key(session.session_id),
            mapping={
                "session_id": session.session_id,
                "user_key": user_key,
                "resolved_from": how,
                "created_at": session.created_at.isoformat(),
                "serve_count": 0,
            },
        )
        # Session detail outlives the idle window so a serve arriving just
        # after expiry can still attribute itself rather than 404.
        pipe.expire(self._sess_key(session.session_id), self.idle_ttl_s * 60)
        await pipe.execute()
        log.info("minted session %s for %s (%s)", session.session_id, user_key, how)
        return session

    async def get(self, session_id: str) -> Session | None:
        raw = await self.redis.hgetall(self._sess_key(session_id))
        if not raw:
            return None
        from datetime import datetime

        try:
            created = datetime.fromisoformat(raw["created_at"])
        except Exception:
            from ..models.common import utcnow

            created = utcnow()
        return Session(
            session_id=raw["session_id"],
            user_key=raw["user_key"],
            resolved_from=raw.get("resolved_from", "unknown"),
            created_at=created,
            serve_count=int(raw.get("serve_count") or 0),
        )

    async def touch(self, session: Session) -> None:
        """Record activity: refresh the idle window and bump the serve count."""
        pipe = self.redis.pipeline()
        pipe.expire(self._key(session.user_key), self.idle_ttl_s)
        pipe.hincrby(self._sess_key(session.session_id), "serve_count", 1)
        pipe.expire(self._sess_key(session.session_id), self.idle_ttl_s * 60)
        await pipe.execute()
