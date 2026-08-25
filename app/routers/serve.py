"""Native ad serving and click tracking."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Response, status

from ..deps import AdServerDep, GeoDep, IdentityDep, SessionServiceDep
from ..models.serve import NativeAdRequest, NativeAdResponse
from ..services.features import context_bucket
from ..services.serving import NoEligibleAdError

log = logging.getLogger(__name__)
router = APIRouter(tags=["serving"])


@router.post("/load/native", response_model=NativeAdResponse)
async def load_native(
    body: NativeAdRequest,
    background: BackgroundTasks,
    identity: IdentityDep,
    geo: GeoDep,
    sessions: SessionServiceDep,
    ad_server: AdServerDep,
    theme: str = Query("dark", pattern="^(dark|light)$"),
) -> NativeAdResponse | Response:
    """Serve a native sponsored-character ad for a feed slot."""
    session = await sessions.get(body.session_id)
    if session is None:
        # An unknown/expired session is a client error worth surfacing rather
        # than silently minting a new one -- otherwise frequency capping and
        # attribution quietly detach from the user the caller thinks they have.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"session {body.session_id} not found or expired; "
            "call POST /session/create first",
        )

    country = geo.country(identity.ip)

    try:
        result = await ad_server.serve(
            session=session,
            position=body.position,
            context=body.context,
            ip=identity.ip,
            country=country,
            os_name=identity.device.os,
            user_agent=identity.user_agent,
            device_family=identity.device.device_family,
            theme=theme,
        )
    except NoEligibleAdError as exc:
        # 204 rather than 404: "there is legitimately no ad for this slot" is a
        # normal outcome in ad serving, and the caller should render the feed
        # without an ad rather than treat it as an error.
        log.info(
            "no eligible ad (session=%s country=%s os=%s): %s",
            body.session_id, country, identity.device.os, exc,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    ctx = body.context
    bucket = context_bucket(
        country, identity.device.os,
        ctx.category if ctx else None, body.position,
        ctx.nsfw if ctx else False,
    )

    # Serve row + counters are written after the response is sent.
    background.add_task(ad_server.persist, result.serve, bucket)
    background.add_task(sessions.touch, session)

    return NativeAdResponse(
        impression_id=result.serve.impression_id,
        rendered_html=result.rendered_html,
    )


@router.post("/impressions/{impression_id}/click", status_code=status.HTTP_202_ACCEPTED)
async def record_click(impression_id: str, ad_server: AdServerDep) -> dict[str, object]:
    """Click callback fired by the rendered creative.

    The template calls this endpoint directly, so it exists even though the
    brief does not list it. Returns 202 on an unknown or already-counted id
    rather than 404: click pixels are fire-and-forget and retried by the
    browser, and a duplicate must never look like a failure the client should
    retry harder.
    """
    counted = await ad_server.record_click(impression_id)
    return {"impression_id": impression_id, "counted": counted}
