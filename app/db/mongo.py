"""MongoDB access.

Mongo was chosen because the brief asks for *collections* and the seed data is
already documents with nested arrays (`geo_targets`, `native_ad_set_ids`) that
would need join tables in a relational store. Nothing here needs multi-table
transactions; the one cross-document write (ad set + variants + campaign link)
is made idempotent instead, which is more robust than a transaction anyway
because it survives a retry from the client.
"""

from __future__ import annotations

import logging
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, IndexModel

log = logging.getLogger(__name__)

CAMPAIGNS = "campaigns"
AD_SETS = "ad_sets"
AD_VARIANTS = "ad_variants"
SERVES = "serves"

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


async def connect(uri: str, db_name: str) -> AsyncIOMotorDatabase:
    global _client, _db
    _client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    _db = _client[db_name]
    await _client.admin.command("ping")
    log.info("connected to mongo db=%s", db_name)
    return _db


async def disconnect() -> None:
    global _client, _db
    if _client is not None:
        _client.close()
    _client, _db = None, None


def get_db() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("mongo not initialised; call connect() first")
    return _db


def set_db(db: AsyncIOMotorDatabase) -> None:
    """Test seam -- inject a mongomock database."""
    global _db
    _db = db


async def ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    """Create indexes. Idempotent, safe to run on every boot."""
    await db[CAMPAIGNS].create_indexes([
        IndexModel([("campaign_id", ASCENDING)], unique=True, name="campaign_id_uq"),
        # The serve path's only Mongo query (cache-miss fallback) filters on
        # exactly this pair.
        IndexModel([("active", ASCENDING), ("surface", ASCENDING)], name="active_surface"),
        IndexModel([("publisher_id", ASCENDING)], name="publisher"),
        IndexModel([("advertiser_company_id", ASCENDING)], name="advertiser"),
    ])
    await db[AD_SETS].create_indexes([
        IndexModel([("ad_set_id", ASCENDING)], unique=True, name="ad_set_id_uq"),
        IndexModel([("campaign_id", ASCENDING), ("active", ASCENDING)], name="campaign_active"),
    ])
    await db[AD_VARIANTS].create_indexes([
        IndexModel([("variant_id", ASCENDING)], unique=True, name="variant_id_uq"),
        IndexModel([("ad_set_id", ASCENDING), ("active", ASCENDING)], name="adset_active"),
        IndexModel([("campaign_id", ASCENDING), ("active", ASCENDING)], name="campaign_active"),
    ])
    await db[SERVES].create_indexes([
        IndexModel([("impression_id", ASCENDING)], unique=True, name="impression_id_uq"),
        IndexModel([("session_id", ASCENDING), ("created_at", DESCENDING)], name="session_time"),
        IndexModel([("campaign_id", ASCENDING), ("created_at", DESCENDING)], name="campaign_time"),
        # Frequency capping reads (user_key, created_at).
        IndexModel([("user_key", ASCENDING), ("created_at", DESCENDING)], name="user_time"),
    ])
    log.info("mongo indexes ensured")


def strip_mongo_id(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop the internal `_id` so documents serialise cleanly to JSON."""
    if doc is None:
        return None
    doc.pop("_id", None)
    return doc
