"""Ad set creation.

Creating an ad set does three writes that must agree: the ad set, its expanded
variants, and the campaign's `native_ad_set_ids` link. Rather than a Mongo
transaction, each write is idempotent and ordered so a partial failure leaves
the system in a state a retry repairs:

  1. variants written first (orphan variants are invisible -- nothing points
     at them yet)
  2. then the ad set
  3. then the campaign link (the step that makes it servable)

If step 3 fails the ad set exists but is not linked, so it cannot be served and
re-POSTing fixes it. The reverse order could briefly expose a campaign linked
to an ad set with no variants, which the serve path would have to skip.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ..db.mongo import AD_SETS, AD_VARIANTS, CAMPAIGNS, get_db, strip_mongo_id
from ..deps import CampaignRepoDep
from ..models.adset import (
    MAX_VARIANTS_PER_AD_SET,
    AdSet,
    AdSetCreate,
    AdSetCreateResponse,
    AdVariant,
)

router = APIRouter(prefix="/adsets", tags=["adsets"])


@router.post("", response_model=AdSetCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_ad_set(body: AdSetCreate, repo: CampaignRepoDep) -> AdSetCreateResponse:
    campaign = await repo.get(body.campaign_id)
    if campaign is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"campaign {body.campaign_id} not found"
        )

    ad_set = AdSet(**body.model_dump(mode="json"))
    try:
        variants = ad_set.build_variants()
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    db = get_db()
    if variants:
        await db[AD_VARIANTS].insert_many([v.model_dump(mode="python") for v in variants])
    await db[AD_SETS].insert_one(ad_set.model_dump(mode="python"))

    # $addToSet keeps the link idempotent under retries.
    res = await db[CAMPAIGNS].update_one(
        {"campaign_id": body.campaign_id},
        {"$addToSet": {"native_ad_set_ids": ad_set.ad_set_id}},
    )
    await repo.refresh_cache()

    return AdSetCreateResponse(
        ad_set=ad_set,
        variants=variants,
        variant_count=len(variants),
        campaign_linked=res.modified_count > 0 or res.matched_count > 0,
    )


@router.get("", response_model=list[AdSet])
async def list_ad_sets(campaign_id: str | None = None, limit: int = 100) -> list[AdSet]:
    q = {"campaign_id": campaign_id} if campaign_id else {}
    docs = await get_db()[AD_SETS].find(q).limit(limit).to_list(length=limit)
    return [AdSet(**strip_mongo_id(d)) for d in docs]


@router.get("/{ad_set_id}", response_model=AdSet)
async def get_ad_set(ad_set_id: str) -> AdSet:
    doc = strip_mongo_id(await get_db()[AD_SETS].find_one({"ad_set_id": ad_set_id}))
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"ad set {ad_set_id} not found")
    return AdSet(**doc)


@router.get("/{ad_set_id}/variants", response_model=list[AdVariant])
async def list_variants(ad_set_id: str, active: bool | None = None) -> list[AdVariant]:
    q: dict = {"ad_set_id": ad_set_id}
    if active is not None:
        q["active"] = active
    docs = await get_db()[AD_VARIANTS].find(q).to_list(length=MAX_VARIANTS_PER_AD_SET)
    return [AdVariant(**strip_mongo_id(d)) for d in docs]
