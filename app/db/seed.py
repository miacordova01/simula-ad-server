"""Load the provided sample campaigns / ad sets / ad variants.

Idempotent by design: seeding upserts on the natural key, so a container
restart or a re-deploy does not duplicate rows or clobber edits made through
the API since the last boot (`$setOnInsert` for timestamps).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import UpdateOne

from ..models.adset import AdSet, AdVariant
from ..models.campaign import Campaign
from .mongo import AD_SETS, AD_VARIANTS, CAMPAIGNS

log = logging.getLogger(__name__)

_MODELS = {CAMPAIGNS: Campaign, AD_SETS: AdSet, AD_VARIANTS: AdVariant}

_FILES = {
    CAMPAIGNS: ("campaigns.json", "campaign_id"),
    AD_SETS: ("ad_sets.json", "ad_set_id"),
    AD_VARIANTS: ("ad_variants.json", "variant_id"),
}


def _parse_dates(doc: dict[str, Any]) -> dict[str, Any]:
    """Seed JSON carries ISO-8601 strings; store real datetimes.

    Without this, `created_at` sorts lexicographically and mixes types with
    documents created through the API.
    """
    for field in ("created_at", "updated_at"):
        v = doc.get(field)
        if isinstance(v, str):
            try:
                doc[field] = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                doc[field] = datetime.now(UTC)
    return doc


async def seed_from_dir(db: AsyncIOMotorDatabase, seed_dir: Path) -> dict[str, int]:
    """Upsert every seed file. Returns {collection: rows_written}."""
    written: dict[str, int] = {}

    for collection, (filename, key) in _FILES.items():
        path = seed_dir / filename
        if not path.exists():
            log.warning("seed file missing: %s", path)
            written[collection] = 0
            continue

        rows = json.loads(path.read_text())
        if not isinstance(rows, list):
            log.warning("seed file %s is not a list; skipping", path)
            written[collection] = 0
            continue

        model = _MODELS[collection]
        ops: list[UpdateOne] = []
        for row in rows:
            doc = _parse_dates(dict(row))
            ident = doc.get(key)
            if not ident:
                log.warning("row in %s missing %s; skipping", filename, key)
                continue

            # Validate through the domain model so seeded documents get the
            # same defaults as API-created ones. The provided campaigns.json
            # omits `surface`, and without this the seeded campaigns would be
            # invisible to the serve path's {active, surface: "native"} query.
            try:
                doc = model(**doc).model_dump(mode="python")
            except Exception:
                log.exception("seed row %s=%s failed validation; skipping", key, ident)
                continue

            created = doc.pop("created_at", datetime.now(UTC))
            ops.append(
                UpdateOne(
                    {key: ident},
                    {"$set": doc, "$setOnInsert": {"created_at": created}},
                    upsert=True,
                )
            )

        if ops:
            res = await db[collection].bulk_write(ops, ordered=False)
            written[collection] = res.upserted_count + res.modified_count
        else:
            written[collection] = 0

        log.info("seeded %s: %d rows from %s", collection, len(ops), filename)

    return written
