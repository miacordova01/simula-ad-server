"""Temporal activities.

Activities own all the I/O. They build their own connections rather than
sharing the API process's clients, because the worker runs as a separate
container (and, in production, would scale independently of the API).
"""

from __future__ import annotations

import logging

from temporalio import activity

log = logging.getLogger(__name__)


@activity.defn(name="refresh_campaign_cache")
async def refresh_campaign_cache() -> dict[str, int | str]:
    """Rebuild the Redis campaign snapshot from Mongo.

    This is the hourly repair job. The API is already write-through, so in the
    steady state this changes nothing -- it exists to heal drift after a Redis
    restart, a missed invalidation, or a direct database edit.
    """
    from ..config import get_settings
    from ..db import mongo, redis_client
    from ..services.campaign_cache import CampaignCache, CampaignRepository

    settings = get_settings()
    db = await mongo.connect(settings.mongo_uri, settings.mongo_db)
    redis = await redis_client.connect(settings.redis_url)
    try:
        cache = CampaignCache(
            redis, settings.campaign_cache_key, settings.campaign_cache_ttl_s
        )
        count = await CampaignRepository(db, cache).refresh_cache()
        activity.logger.info("campaign cache refreshed: %d campaigns", count)
        return {"status": "ok", "campaigns": count}
    finally:
        await redis_client.disconnect()
        await mongo.disconnect()


@activity.defn(name="decay_ctr_counters")
async def decay_ctr_counters(factor: float = 0.5) -> dict[str, int | str]:
    """Halve the bandit's evidence so stale performance fades.

    Same reasoning as the decayed-posterior layer in the offline CTR work:
    without forgetting, a campaign that performed well a month ago keeps
    winning auctions it no longer deserves, and campaigns that have gone quiet
    never regain enough uncertainty to be explored again.
    """
    from ..config import get_settings
    from ..db import redis_client
    from ..services.features import FeatureStore

    settings = get_settings()
    redis = await redis_client.connect(settings.redis_url)
    try:
        n = await FeatureStore(redis).decay(factor=factor)
        activity.logger.info("decayed %d ctr counters by %.2f", n, factor)
        return {"status": "ok", "keys_decayed": n}
    finally:
        await redis_client.disconnect()
