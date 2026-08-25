"""Campaign CRUD.

`campaign_id`, `created_at` and `updated_at` are API-owned and are not
accepted from the client on any route; `active` is API-owned on create (new
campaigns default to inactive) but settable via PATCH, which is what the
brief's own PATCH example does.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status

from ..deps import CampaignRepoDep
from ..models.campaign import Campaign, CampaignCreate, CampaignUpdate

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


@router.post("", response_model=Campaign, status_code=status.HTTP_201_CREATED)
async def create_campaign(body: CampaignCreate, repo: CampaignRepoDep) -> Campaign:
    campaign = Campaign(
        **body.model_dump(mode="json", exclude_none=True),
    )
    return await repo.create(campaign)


@router.get("", response_model=list[Campaign])
async def list_campaigns(
    repo: CampaignRepoDep,
    ids: Annotated[
        str | None, Query(description="comma-separated campaign ids")
    ] = None,
    surface: Annotated[str | None, Query()] = None,
    publisher_id: Annotated[str | None, Query()] = None,
    active: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Campaign]:
    id_list = [i.strip() for i in ids.split(",") if i.strip()] if ids else None
    return await repo.find(
        ids=id_list, surface=surface, publisher_id=publisher_id,
        active=active, limit=limit, offset=offset,
    )


@router.get("/{campaign_id}", response_model=Campaign)
async def get_campaign(campaign_id: str, repo: CampaignRepoDep) -> Campaign:
    campaign = await repo.get(campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"campaign {campaign_id} not found")
    return campaign


@router.patch("/{campaign_id}", response_model=Campaign)
async def update_campaign(
    campaign_id: str, body: CampaignUpdate, repo: CampaignRepoDep
) -> Campaign:
    # exclude_unset so PATCH means "change these fields", not "null the rest".
    changes = body.model_dump(mode="json", exclude_unset=True)
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no fields supplied to update")
    updated = await repo.update(campaign_id, changes)
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"campaign {campaign_id} not found")
    return updated


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_campaign(campaign_id: str, repo: CampaignRepoDep) -> Response:
    if not await repo.delete(campaign_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"campaign {campaign_id} not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
