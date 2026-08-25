"""Redis access -- campaign cache, sessions, frequency caps, generated copy.

One Redis instance serves four jobs with different lifetimes, so every key is
namespaced by purpose. All of them are derived state: Redis going away costs
latency and resets frequency caps, but never loses a campaign or a serve.
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

_redis: aioredis.Redis | None = None


async def connect(url: str) -> aioredis.Redis:
    global _redis
    _redis = aioredis.from_url(url, encoding="utf-8", decode_responses=True)
    await _redis.ping()
    log.info("connected to redis")
    return _redis


async def disconnect() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
    _redis = None


def get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("redis not initialised; call connect() first")
    return _redis


def set_redis(client: aioredis.Redis) -> None:
    """Test seam -- inject fakeredis."""
    global _redis
    _redis = client


# --- key builders ------------------------------------------------------
def session_key(user_key: str) -> str:
    return f"sess:user:{user_key}"


def fatigue_key(user_key: str, campaign_id: str) -> str:
    return f"fatigue:{user_key}:{campaign_id}"


def copy_key(variant_id: str) -> str:
    return f"copy:variant:{variant_id}"


def spend_key(campaign_id: str, day: str) -> str:
    return f"spend:{campaign_id}:{day}"
