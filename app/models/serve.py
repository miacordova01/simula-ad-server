"""The Serve record - one row per rendered ad.

Join point for billing, CTR training data, pacing and freq capping, so it stores
the whole decision: what we knew, what we picked, what we rejected, timings.

Storing the losing candidates and their scores is what makes the log usable for
off-policy eval later. Log only the winner and you can only learn from what you
already chose to serve.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from .common import Base, new_id, utcnow


class AdContext(Base):
    """Relevance signals supplied by the caller."""

    searchTerm: str | None = None
    tags: list[str] = Field(default_factory=list)
    category: str | None = None
    title: str | None = None
    nsfw: bool = False


class NativeAdRequest(Base):
    position: int = Field(ge=0, description="feed index of the slot")
    session_id: str = Field(min_length=1)
    context: AdContext | None = None


class NativeAdResponse(Base):
    impression_id: str
    rendered_html: str


class CandidateScore(Base):
    """One scored campaign, kept for auditability and off-policy learning."""

    campaign_id: str
    p_ctr: float
    p_ctr_ucb: float
    bid: float
    pacing_mult: float = 1.0
    fatigue_mult: float = 1.0
    score: float
    rank: int
    reason: str = ""


class ServeDecision(Base):
    """Why this serve looks the way it does."""

    eligible_before_filters: int = 0
    rejected_geo: list[str] = Field(default_factory=list)
    rejected_os: list[str] = Field(default_factory=list)
    rejected_inactive: list[str] = Field(default_factory=list)
    rejected_no_variants: list[str] = Field(default_factory=list)
    rejected_fatigue: list[str] = Field(default_factory=list)
    candidates: list[CandidateScore] = Field(default_factory=list)


class Serve(Base):
    impression_id: str = Field(default_factory=lambda: new_id("imp"))
    session_id: str
    user_key: str
    position: int

    # --- what we served ---------------------------------------------
    campaign_id: str
    ad_set_id: str
    variant_id: str
    character_name: str
    cta: str
    media_url: str
    tracking_url: str | None = None
    char_message: str
    # "llm" or "fallback" -- lets us monitor LLM availability from the serve
    # log alone, without correlating against application logs.
    copy_source: str = "fallback"

    # --- request context --------------------------------------------
    ip: str | None = None
    country: str | None = None
    os: str | None = None
    user_agent: str | None = None
    device_family: str | None = None
    context: AdContext | None = None

    # --- decision trace ---------------------------------------------
    decision: ServeDecision = Field(default_factory=ServeDecision)
    timings_ms: dict[str, float] = Field(default_factory=dict)

    # --- outcome ------------------------------------------------------
    clicked: bool = False
    clicked_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)

    def to_feature_row(self) -> dict[str, Any]:
        """Flatten to the row shape the CTR trainer consumes.

        Keeping this next to the model means the serving log and the training
        pipeline cannot drift apart silently -- which is the usual way online
        and offline features stop matching.
        """
        ctx = self.context or AdContext()
        return {
            "impression_id": self.impression_id,
            "click": int(self.clicked),
            "hour": self.created_at.strftime("%y%m%d%H"),
            "campaign_id": self.campaign_id,
            "ad_set_id": self.ad_set_id,
            "variant_id": self.variant_id,
            "character_name": self.character_name,
            "cta": self.cta,
            "position": self.position,
            "country": self.country,
            "os": self.os,
            "device_family": self.device_family,
            "category": ctx.category,
            "nsfw": int(ctx.nsfw),
            "n_tags": len(ctx.tags),
            "has_search_term": int(bool(ctx.searchTerm)),
            "copy_source": self.copy_source,
            "session_id": self.session_id,
            "user_key": self.user_key,
        }
