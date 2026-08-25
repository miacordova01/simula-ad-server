"""Online feature store.

Serve path can't afford a Mongo aggregation, let alone a join. So features are
pre-aggregated Redis counters, written on serve/click and read with one
pipelined MGET:

    ctr:{campaign}:{bucket}       imps + clicks for a campaign in a context bucket
    user:{user_key}               small rolling profile
    fatigue:{user_key}:{campaign} exposure count, TTL'd

Counters decay by halving on a schedule instead of storing timestamped events.
Keeps each key O(1), lets stale evidence fade, and re-inflates uncertainty on
arms that went quiet so they become explorable again.

Buckets are coarse on purpose - (country, os, category, position band) keeps
cardinality low enough that counts per cell mean something.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

CTR_TTL_S = 14 * 24 * 3600
USER_TTL_S = 30 * 24 * 3600


def position_band(position: int) -> str:
    """Feed position matters, but only coarsely."""
    if position <= 0:
        return "p0"
    if position <= 2:
        return "p1_2"
    if position <= 5:
        return "p3_5"
    if position <= 10:
        return "p6_10"
    return "p11plus"


def context_bucket(
    country: str | None,
    os_name: str | None,
    category: str | None,
    position: int,
    nsfw: bool = False,
) -> str:
    """Coarse, stable key describing the request context."""
    return "|".join(
        [
            (country or "XX").upper(),
            (os_name or "unknown"),
            (category or "none").lower()[:24],
            position_band(position),
            "nsfw" if nsfw else "sfw",
        ]
    )


@dataclass
class CampaignStats:
    impressions: float = 0.0
    clicks: float = 0.0

    @property
    def ctr(self) -> float:
        return self.clicks / self.impressions if self.impressions > 0 else 0.0


@dataclass
class UserProfile:
    serves: int = 0
    clicks: int = 0
    countries: list[str] = field(default_factory=list)

    @property
    def ctr(self) -> float:
        return self.clicks / self.serves if self.serves > 0 else 0.0


class FeatureStore:
    def __init__(self, redis: aioredis.Redis) -> None:
        self.redis = redis

    # -- keys ------------------------------------------------------
    @staticmethod
    def ctr_key(campaign_id: str, bucket: str) -> str:
        return f"ctr:{campaign_id}:{bucket}"

    @staticmethod
    def ctr_global_key(campaign_id: str) -> str:
        return f"ctr:{campaign_id}:__all__"

    @staticmethod
    def user_key_name(user_key: str) -> str:
        return f"user:{user_key}"

    @staticmethod
    def fatigue_key(user_key: str, campaign_id: str) -> str:
        return f"fatigue:{user_key}:{campaign_id}"

    # -- reads -----------------------------------------------------
    async def campaign_stats(
        self, campaign_ids: list[str], bucket: str
    ) -> dict[str, tuple[CampaignStats, CampaignStats]]:
        """Bucketed and global stats for each campaign, in one round trip.

        Returns {campaign_id: (bucket_stats, global_stats)}. The caller blends
        them: the bucket is more relevant, the global is denser, so a shrinkage
        toward global is the sane estimator when the bucket is thin.
        """
        if not campaign_ids:
            return {}
        pipe = self.redis.pipeline()
        for cid in campaign_ids:
            pipe.hmget(self.ctr_key(cid, bucket), ["imp", "clk"])
            pipe.hmget(self.ctr_global_key(cid), ["imp", "clk"])
        rows = await pipe.execute()

        out: dict[str, tuple[CampaignStats, CampaignStats]] = {}
        for i, cid in enumerate(campaign_ids):
            b_imp, b_clk = rows[2 * i]
            g_imp, g_clk = rows[2 * i + 1]
            out[cid] = (
                CampaignStats(float(b_imp or 0), float(b_clk or 0)),
                CampaignStats(float(g_imp or 0), float(g_clk or 0)),
            )
        return out

    async def fatigue_counts(
        self, user_key: str, campaign_ids: list[str]
    ) -> dict[str, int]:
        if not campaign_ids:
            return {}
        pipe = self.redis.pipeline()
        for cid in campaign_ids:
            pipe.get(self.fatigue_key(user_key, cid))
        rows = await pipe.execute()
        return {cid: int(v or 0) for cid, v in zip(campaign_ids, rows, strict=False)}

    async def user_profile(self, user_key: str) -> UserProfile:
        raw = await self.redis.hgetall(self.user_key_name(user_key))
        if not raw:
            return UserProfile()
        countries = [c for c in (raw.get("countries") or "").split(",") if c]
        return UserProfile(
            serves=int(raw.get("serves") or 0),
            clicks=int(raw.get("clicks") or 0),
            countries=countries,
        )

    # -- writes ----------------------------------------------------
    async def record_serve(
        self,
        campaign_id: str,
        bucket: str,
        user_key: str,
        country: str | None,
        fatigue_window_s: int,
    ) -> None:
        """Increment impression counters. Fire-and-forget from the serve path."""
        pipe = self.redis.pipeline()
        for key in (self.ctr_key(campaign_id, bucket), self.ctr_global_key(campaign_id)):
            pipe.hincrbyfloat(key, "imp", 1.0)
            pipe.expire(key, CTR_TTL_S)

        fkey = self.fatigue_key(user_key, campaign_id)
        pipe.incr(fkey)
        pipe.expire(fkey, fatigue_window_s)

        ukey = self.user_key_name(user_key)
        pipe.hincrby(ukey, "serves", 1)
        if country:
            pipe.hset(ukey, "last_country", country)
        pipe.expire(ukey, USER_TTL_S)
        await pipe.execute()

    async def record_click(self, campaign_id: str, bucket: str, user_key: str) -> None:
        pipe = self.redis.pipeline()
        for key in (self.ctr_key(campaign_id, bucket), self.ctr_global_key(campaign_id)):
            pipe.hincrbyfloat(key, "clk", 1.0)
            pipe.expire(key, CTR_TTL_S)
        pipe.hincrby(self.user_key_name(user_key), "clicks", 1)
        await pipe.execute()

    async def decay(self, factor: float = 0.5, match: str = "ctr:*") -> int:
        """Halve all CTR counters -- run on a schedule.

        Exponential forgetting keeps the bandit tracking drift instead of being
        anchored by evidence from weeks ago, and it re-inflates uncertainty on
        campaigns that have gone quiet so they become explorable again.
        """
        n = 0
        async for key in self.redis.scan_iter(match=match, count=500):
            vals = await self.redis.hmget(key, ["imp", "clk"])
            imp, clk = float(vals[0] or 0), float(vals[1] or 0)
            if imp <= 0 and clk <= 0:
                continue
            await self.redis.hset(key, mapping={"imp": imp * factor, "clk": clk * factor})
            n += 1
        return n
