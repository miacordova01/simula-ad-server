"""Campaign models.

`campaign_id`, `created_at`, `updated_at` and `active` are API-owned: they are
never accepted on create and (except `active`) never on update. That split is
enforced by using separate Create/Update/Document models rather than one model
with optional fields, so it is impossible to forget a guard on a new route.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import Field, HttpUrl, field_validator

from .common import OS, Base, Surface, new_id, utcnow

CountryCode = Annotated[str, Field(min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$")]


class CampaignCreate(Base):
    campaign_name: str = Field(min_length=1, max_length=200)
    advertiser_company_id: str = Field(min_length=1, max_length=100)
    daily_budget: float | None = Field(default=None, ge=0)
    geo_targets: list[CountryCode] = Field(default_factory=list)
    os_targets: list[OS] = Field(default_factory=list)
    attribution_provider: str | None = None
    ios_store_url: HttpUrl | None = None
    android_store_url: HttpUrl | None = None
    native_ad_set_ids: list[str] = Field(default_factory=list)
    surface: Surface = Surface.native
    publisher_id: str | None = None
    # Optional impression pixel; the template no-ops when empty.
    impression_url: HttpUrl | None = None
    downloads_label: str | None = None

    @field_validator("geo_targets")
    @classmethod
    def _upper_geo(cls, v: list[str]) -> list[str]:
        # Normalise on write so serve-time matching is a plain set lookup
        # rather than a case-insensitive scan.
        return sorted({c.upper() for c in v})


class CampaignUpdate(Base):
    """PATCH body. Every field optional; unset fields are left untouched.

    `active` IS settable here (the brief's own PATCH example toggles it) even
    though it is API-owned on create.
    """

    campaign_name: str | None = Field(default=None, min_length=1, max_length=200)
    advertiser_company_id: str | None = Field(default=None, min_length=1)
    daily_budget: float | None = Field(default=None, ge=0)
    geo_targets: list[CountryCode] | None = None
    os_targets: list[OS] | None = None
    attribution_provider: str | None = None
    ios_store_url: HttpUrl | None = None
    android_store_url: HttpUrl | None = None
    native_ad_set_ids: list[str] | None = None
    surface: Surface | None = None
    publisher_id: str | None = None
    impression_url: HttpUrl | None = None
    downloads_label: str | None = None
    active: bool | None = None

    @field_validator("geo_targets")
    @classmethod
    def _upper_geo(cls, v: list[str] | None) -> list[str] | None:
        return sorted({c.upper() for c in v}) if v is not None else None


class Campaign(Base):
    campaign_id: str = Field(default_factory=lambda: new_id("camp"))
    campaign_name: str
    advertiser_company_id: str
    active: bool = False  # new campaigns default to inactive
    daily_budget: float | None = None
    geo_targets: list[str] = Field(default_factory=list)
    os_targets: list[str] = Field(default_factory=list)
    attribution_provider: str | None = None
    ios_store_url: str | None = None
    android_store_url: str | None = None
    native_ad_set_ids: list[str] = Field(default_factory=list)
    surface: str = Surface.native
    publisher_id: str | None = None
    impression_url: str | None = None
    downloads_label: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    # ---- serve-time helpers ------------------------------------------
    def matches_geo(self, country: str | None) -> bool:
        """Empty geo_targets means 'no restriction'.

        An unknown country (GeoIP miss) only matches an unrestricted campaign.
        Failing closed here is deliberate: serving a US-only campaign to an
        unresolvable IP is a billing incident, whereas dropping it is a missed
        impression.
        """
        if not self.geo_targets:
            return True
        if not country:
            return False
        return country.upper() in self.geo_targets

    def matches_os(self, os_name: str | None) -> bool:
        """Empty os_targets means 'no restriction'. Same fail-closed logic."""
        if not self.os_targets:
            return True
        if not os_name or os_name == OS.unknown:
            return False
        return os_name in self.os_targets

    def store_url_for(self, os_name: str | None) -> str | None:
        """Click destination appropriate to the device."""
        if os_name == OS.ios and self.ios_store_url:
            return self.ios_store_url
        if os_name == OS.android and self.android_store_url:
            return self.android_store_url
        return self.ios_store_url or self.android_store_url
