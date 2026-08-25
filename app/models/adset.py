"""Ad set and ad variant models.

An ad set holds *lists* of assets; a variant is one concrete combination. The
brief says creating an ad set generates the cartesian product of the asset
lists, so `AdSet.build_variants` is the single place that expansion happens.
"""

from __future__ import annotations

import itertools
from datetime import datetime

from pydantic import Field, field_validator

from .common import Base, new_id, utcnow

# Guard rail: the cartesian product grows multiplicatively and an ad set with
# four 20-item lists would silently create 160k variants. Cap it and fail loudly.
MAX_VARIANTS_PER_AD_SET = 500


class AdSetCreate(Base):
    campaign_id: str = Field(min_length=1)
    ad_set_name: str = Field(min_length=1, max_length=200)
    character_names: list[str] = Field(min_length=1)
    video_urls: list[str] = Field(min_length=1)
    ctas: list[str] = Field(min_length=1)
    ai_prompts: list[str] = Field(min_length=1)
    # Not part of the product: it is the per-ad-set safety net used when the
    # LLM call fails, so it is a list we pick from rather than a variant axis.
    fallback_copy: list[str] = Field(default_factory=list)
    active: bool = True

    @field_validator("character_names", "video_urls", "ctas", "ai_prompts")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        """Drop duplicates while preserving order.

        Without this, a repeated CTA silently doubles the variant count and
        biases random variant selection toward the duplicated asset.
        """
        seen: set[str] = set()
        out: list[str] = []
        for item in v:
            s = item.strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        if not out:
            raise ValueError("list must contain at least one non-empty value")
        return out


class AdSet(Base):
    ad_set_id: str = Field(default_factory=lambda: new_id("adset"))
    campaign_id: str
    ad_set_name: str
    active: bool = True
    character_names: list[str] = Field(default_factory=list)
    video_urls: list[str] = Field(default_factory=list)
    ctas: list[str] = Field(default_factory=list)
    ai_prompts: list[str] = Field(default_factory=list)
    fallback_copy: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def variant_count(self) -> int:
        return (
            len(self.character_names)
            * len(self.video_urls)
            * len(self.ctas)
            * len(self.ai_prompts)
        )

    def build_variants(self) -> list[AdVariant]:
        """Cartesian product of the asset lists -> concrete variants."""
        n = self.variant_count()
        if n > MAX_VARIANTS_PER_AD_SET:
            raise ValueError(
                f"ad set would generate {n} variants, over the "
                f"{MAX_VARIANTS_PER_AD_SET} cap; split it into smaller ad sets"
            )
        combos = itertools.product(
            self.character_names, self.video_urls, self.ctas, self.ai_prompts
        )
        return [
            AdVariant(
                ad_set_id=self.ad_set_id,
                campaign_id=self.campaign_id,
                character_name=character,
                video_url=video,
                cta=cta,
                ai_prompt=prompt,
            )
            for character, video, cta, prompt in combos
        ]


class AdVariant(Base):
    variant_id: str = Field(default_factory=lambda: new_id("var"))
    ad_set_id: str
    campaign_id: str
    character_name: str
    video_url: str
    cta: str
    ai_prompt: str
    active: bool = True
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AdSetCreateResponse(Base):
    ad_set: AdSet
    variants: list[AdVariant]
    variant_count: int
    campaign_linked: bool
