"""Ad-serving orchestration.

    Request -> Session -> Campaigns -> Geo -> OS -> Rank -> Variant
            -> Render & WriteServe

Two latency decisions worth calling out:

1. The serve write is off the response path. impression_id is minted before the
   write is scheduled, so the response is consistent even though the row lands
   a few ms later.
2. Copy is cached per variant, so only the first serve of a variant pays LLM
   latency. With the timeout + fallback, the LLM can never make a serve slow or
   failed.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..db.mongo import AD_SETS, AD_VARIANTS, SERVES, strip_mongo_id
from ..models.adset import AdSet, AdVariant
from ..models.campaign import Campaign
from ..models.serve import AdContext, CandidateScore, Serve, ServeDecision
from ..models.session import Session
from .campaign_cache import CampaignRepository
from .features import FeatureStore, context_bucket
from .llm import CopyGenerator
from .ranking import CampaignRanker, ScoringContext
from .renderer import RenderContext, TemplateRenderer, looks_like_video

log = logging.getLogger(__name__)


class NoEligibleAdError(Exception):
    """Raised when nothing survives filtering. Maps to a 204 at the route."""

    def __init__(self, decision: ServeDecision, message: str = "no eligible ad") -> None:
        super().__init__(message)
        self.decision = decision


@dataclass
class ServeResult:
    serve: Serve
    rendered_html: str
    timings_ms: dict[str, float] = field(default_factory=dict)


class AdServer:
    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        redis: aioredis.Redis,
        campaigns: CampaignRepository,
        features: FeatureStore,
        ranker: CampaignRanker,
        renderer: TemplateRenderer,
        copy_gen: CopyGenerator,
        api_url: str,
        api_key: str,
        fatigue_window_s: int = 3600,
        copy_cache_ttl_s: int = 6 * 3600,
        rng: random.Random | None = None,
    ) -> None:
        self.db = db
        self.redis = redis
        self.campaigns = campaigns
        self.features = features
        self.ranker = ranker
        self.renderer = renderer
        self.copy_gen = copy_gen
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.fatigue_window_s = fatigue_window_s
        self.copy_cache_ttl_s = copy_cache_ttl_s
        # Injectable so variant selection is deterministic under test.
        self.rng = rng or random.Random()

    # -- variant selection -------------------------------------------
    async def _pick_variant(
        self, campaign: Campaign
    ) -> tuple[AdSet, AdVariant] | None:
        """Random active ad set, then random active variant within it.

        Two-stage random (rather than one random pick over all the campaign's
        variants) is what the brief describes, and it also gives every ad set
        equal share regardless of how many variants it expanded into --
        otherwise an ad set with 40 combinations would drown one with 2.
        """
        set_ids = campaign.native_ad_set_ids or []
        query: dict[str, Any] = {"campaign_id": campaign.campaign_id, "active": True}
        if set_ids:
            query = {"ad_set_id": {"$in": set_ids}, "active": True}

        ad_sets = [
            AdSet(**strip_mongo_id(d))
            for d in await self.db[AD_SETS].find(query).to_list(length=100)
        ]
        if not ad_sets:
            return None

        self.rng.shuffle(ad_sets)
        for ad_set in ad_sets:
            variants = [
                AdVariant(**strip_mongo_id(d))
                for d in await self.db[AD_VARIANTS]
                .find({"ad_set_id": ad_set.ad_set_id, "active": True})
                .to_list(length=1000)
            ]
            if variants:
                return ad_set, self.rng.choice(variants)
            # An ad set with no active variants is not fatal -- try the next.
            log.warning("ad set %s has no active variants", ad_set.ad_set_id)
        return None

    # -- copy ----------------------------------------------------------
    async def _get_copy(self, variant: AdVariant, ad_set: AdSet) -> tuple[str, str]:
        """(text, source) for the character's line."""
        fallback = (
            self.rng.choice(ad_set.fallback_copy)
            if ad_set.fallback_copy
            else f"{variant.character_name} has something for you."
        )
        cache_key = f"copy:variant:{variant.variant_id}"

        try:
            cached = await self.redis.get(cache_key)
            if cached:
                return cached, "cache"
        except Exception:
            log.exception("copy cache read failed")

        result = await self.copy_gen.generate(
            variant.character_name, variant.ai_prompt, fallback
        )
        if result.source == "llm":
            try:
                await self.redis.set(cache_key, result.text, ex=self.copy_cache_ttl_s)
            except Exception:
                log.exception("copy cache write failed")
        return result.text, result.source

    # -- main ----------------------------------------------------------
    async def serve(
        self,
        session: Session,
        position: int,
        context: AdContext | None,
        ip: str | None,
        country: str | None,
        os_name: str | None,
        user_agent: str | None,
        device_family: str | None,
        theme: str = "dark",
    ) -> ServeResult:
        t0 = time.perf_counter()
        timings: dict[str, float] = {}
        decision = ServeDecision()
        ctx = context or AdContext()

        def mark(label: str, since: float) -> float:
            now = time.perf_counter()
            timings[label] = round((now - since) * 1000, 2)
            return now

        # --- fetch candidates (cache-first) ---------------------------
        t = t0
        campaigns, cache_hit = await self.campaigns.active_for_serving("native")
        timings["cache_hit"] = 1.0 if cache_hit else 0.0
        t = mark("fetch_campaigns", t)
        decision.eligible_before_filters = len(campaigns)

        # --- geo + OS filters ----------------------------------------
        eligible: list[Campaign] = []
        for c in campaigns:
            if not c.active:
                decision.rejected_inactive.append(c.campaign_id)
                continue
            if not c.matches_geo(country):
                decision.rejected_geo.append(c.campaign_id)
                continue
            if not c.matches_os(os_name):
                decision.rejected_os.append(c.campaign_id)
                continue
            eligible.append(c)
        t = mark("filters", t)

        if not eligible:
            raise NoEligibleAdError(decision, "no campaign matched geo/os targeting")

        # --- features + ranking --------------------------------------
        bucket = context_bucket(country, os_name, ctx.category, position, ctx.nsfw)
        cids = [c.campaign_id for c in eligible]
        stats, fatigue = await asyncio.gather(
            self.features.campaign_stats(cids, bucket),
            self.features.fatigue_counts(session.user_key, cids),
        )
        t = mark("features", t)

        now = datetime.now(UTC)
        fraction_of_day = (now.hour * 3600 + now.minute * 60 + now.second) / 86400.0
        scoring_ctx = ScoringContext(
            country=country, os=os_name, category=ctx.category,
            position=position, nsfw=ctx.nsfw, bucket=bucket,
        )
        ranked = self.ranker.rank(eligible, stats, fatigue, scoring_ctx,
                                  fraction_of_day=fraction_of_day)
        decision.candidates = [
            CandidateScore(
                campaign_id=s.campaign.campaign_id, p_ctr=s.p_ctr,
                p_ctr_ucb=s.p_ctr_ucb, bid=s.bid, pacing_mult=s.pacing_mult,
                fatigue_mult=s.fatigue_mult, score=s.score, rank=i, reason=s.reason,
            )
            for i, s in enumerate(ranked, start=1)
        ]
        t = mark("ranking", t)

        # --- variant selection, walking down the ranking --------------
        # A campaign that ranks first but has no servable creative must not
        # blank the slot; fall through to the next candidate instead.
        chosen: tuple[Campaign, AdSet, AdVariant] | None = None
        for scored in ranked:
            if scored.fatigue_mult == 0.0:
                decision.rejected_fatigue.append(scored.campaign.campaign_id)
                continue
            picked = await self._pick_variant(scored.campaign)
            if picked is None:
                decision.rejected_no_variants.append(scored.campaign.campaign_id)
                continue
            chosen = (scored.campaign, picked[0], picked[1])
            break

        if chosen is None:
            raise NoEligibleAdError(decision, "no campaign had a servable variant")

        campaign, ad_set, variant = chosen
        t = mark("variant_selection", t)

        # --- copy -----------------------------------------------------
        char_message, copy_source = await self._get_copy(variant, ad_set)
        t = mark("copy", t)

        # --- build the serve record -----------------------------------
        serve = Serve(
            session_id=session.session_id,
            user_key=session.user_key,
            position=position,
            campaign_id=campaign.campaign_id,
            ad_set_id=ad_set.ad_set_id,
            variant_id=variant.variant_id,
            character_name=variant.character_name,
            cta=variant.cta,
            media_url=variant.video_url,
            tracking_url=campaign.store_url_for(os_name),
            char_message=char_message,
            copy_source=copy_source,
            ip=ip, country=country, os=os_name,
            user_agent=user_agent, device_family=device_family,
            context=ctx, decision=decision,
        )

        # --- render ---------------------------------------------------
        html = self.renderer.render(
            RenderContext(
                CHAR_NAME=variant.character_name,
                CAMPAIGN=campaign.advertiser_company_id or campaign.campaign_name,
                CHAR_MESSAGE=char_message,
                CTA=variant.cta,
                MEDIA_URL=variant.video_url,
                TRACKING_URL=serve.tracking_url or "",
                IMPRESSION_URL=campaign.impression_url or "",
                AD_ID=serve.impression_id,
                API_URL=self.api_url,
                API_KEY=self.api_key,
                THEME=theme,
                DOWNLOADS=campaign.downloads_label or "",
                MEDIA_IS_VIDEO=looks_like_video(variant.video_url),
            )
        )
        t = mark("render", t)
        timings["total"] = round((time.perf_counter() - t0) * 1000, 2)
        serve.timings_ms = timings

        return ServeResult(serve=serve, rendered_html=html, timings_ms=timings)

    # -- async persistence --------------------------------------------
    async def persist(self, serve: Serve, bucket: str) -> None:
        """Write the serve and bump counters. Runs off the response path."""
        try:
            await asyncio.gather(
                self.db[SERVES].insert_one(serve.model_dump(mode="python")),
                self.features.record_serve(
                    serve.campaign_id, bucket, serve.user_key, serve.country,
                    self.fatigue_window_s,
                ),
            )
        except Exception:
            # Losing a serve row costs us a training example and some billing
            # accuracy; it must never surface to the caller, who already has
            # their ad.
            log.exception("failed to persist serve %s", serve.impression_id)

    async def record_click(self, impression_id: str) -> bool:
        """Mark a serve clicked and update the bandit's evidence."""
        doc = await self.db[SERVES].find_one_and_update(
            {"impression_id": impression_id, "clicked": False},
            {"$set": {"clicked": True, "clicked_at": datetime.now(UTC)}},
            return_document=True,
        )
        if not doc:
            return False
        doc = strip_mongo_id(doc)
        ctx = doc.get("context") or {}
        bucket = context_bucket(
            doc.get("country"), doc.get("os"), ctx.get("category"),
            int(doc.get("position") or 0), bool(ctx.get("nsfw")),
        )
        try:
            await self.features.record_click(
                doc["campaign_id"], bucket, doc["user_key"]
            )
        except Exception:
            log.exception("failed to record click features")
        return True
