"""Campaign cache + repository.

Serve path must not query Mongo. Active campaigns are small and slow-moving, so
the whole set is one Redis key read with a single GET.

One key rather than key-per-campaign on purpose: the serve path needs all
candidates, so per-campaign keys would need an index key too, and that index can
disagree with what it points at. A single snapshot swaps atomically.

Writes are write-through - Mongo is truth, cache refreshes right after every
mutation, so toggling a campaign takes effect on the next request. The Temporal
schedule is the repair job for drift (Redis restart, missed invalidation), not
the primary path.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as aioredis
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..db.mongo import CAMPAIGNS, strip_mongo_id
from ..models.campaign import Campaign

log = logging.getLogger(__name__)


def _json_default(o: Any) -> str:
    from datetime import datetime

    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


class CampaignCache:
    def __init__(self, redis: aioredis.Redis, key: str, ttl_s: int) -> None:
        self.redis = redis
        self.key = key
        self.ttl_s = ttl_s

    async def write(self, campaigns: list[Campaign]) -> int:
        payload = json.dumps(
            [c.model_dump(mode="json") for c in campaigns], default=_json_default
        )
        await self.redis.set(self.key, payload, ex=self.ttl_s)
        log.info("campaign cache written: %d campaigns", len(campaigns))
        return len(campaigns)

    async def read(self) -> list[Campaign] | None:
        """Cached active campaigns, or None on a miss."""
        raw = await self.redis.get(self.key)
        if not raw:
            return None
        try:
            rows = json.loads(raw)
            return [Campaign(**r) for r in rows]
        except Exception:
            # A corrupt or schema-drifted snapshot must not break serving --
            # drop it and let the caller fall through to Mongo.
            log.exception("corrupt campaign cache; dropping key")
            await self.redis.delete(self.key)
            return None

    async def invalidate(self) -> None:
        await self.redis.delete(self.key)


class CampaignRepository:
    """Mongo-backed CRUD with a write-through cache."""

    def __init__(self, db: AsyncIOMotorDatabase, cache: CampaignCache) -> None:
        self.db = db
        self.cache = cache

    @property
    def col(self):
        return self.db[CAMPAIGNS]

    # -- reads -----------------------------------------------------
    async def get(self, campaign_id: str) -> Campaign | None:
        doc = strip_mongo_id(await self.col.find_one({"campaign_id": campaign_id}))
        return Campaign(**doc) if doc else None

    async def find(
        self,
        ids: list[str] | None = None,
        surface: str | None = None,
        publisher_id: str | None = None,
        active: bool | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Campaign]:
        q: dict[str, Any] = {}
        if ids:
            q["campaign_id"] = {"$in": ids}
        if surface:
            q["surface"] = surface
        if publisher_id:
            q["publisher_id"] = publisher_id
        if active is not None:
            q["active"] = active
        cursor = self.col.find(q).sort("created_at", -1).skip(offset).limit(limit)
        return [Campaign(**strip_mongo_id(d)) for d in await cursor.to_list(length=limit)]

    async def list_active_from_db(self, surface: str = "native") -> list[Campaign]:
        cursor = self.col.find({"active": True, "surface": surface})
        return [Campaign(**strip_mongo_id(d)) for d in await cursor.to_list(length=5000)]

    async def active_for_serving(self, surface: str = "native") -> tuple[list[Campaign], bool]:
        """Serve-path read. Returns (campaigns, cache_hit).

        On a miss we read Mongo AND repopulate, so a cold start or a Redis
        flush costs one slow request rather than every request until the next
        scheduled refresh.
        """
        cached = await self.cache.read()
        if cached is not None:
            return [c for c in cached if c.surface == surface], True
        campaigns = await self.list_active_from_db(surface)
        try:
            await self.cache.write(await self.list_active_from_db())
        except Exception:
            log.exception("failed to repopulate campaign cache")
        return campaigns, False

    # -- writes ----------------------------------------------------
    async def create(self, campaign: Campaign) -> Campaign:
        await self.col.insert_one(campaign.model_dump(mode="python"))
        await self.refresh_cache()
        return campaign

    async def update(self, campaign_id: str, changes: dict[str, Any]) -> Campaign | None:
        from ..models.common import utcnow

        if not changes:
            return await self.get(campaign_id)
        changes = {**changes, "updated_at": utcnow()}
        doc = await self.col.find_one_and_update(
            {"campaign_id": campaign_id}, {"$set": changes}, return_document=True
        )
        await self.refresh_cache()
        return Campaign(**strip_mongo_id(doc)) if doc else None

    async def delete(self, campaign_id: str) -> bool:
        res = await self.col.delete_one({"campaign_id": campaign_id})
        await self.refresh_cache()
        return res.deleted_count > 0

    async def refresh_cache(self) -> int:
        """Rebuild the snapshot from Mongo. Used by writes and by Temporal."""
        campaigns = await self.list_active_from_db()
        return await self.cache.write(campaigns)
