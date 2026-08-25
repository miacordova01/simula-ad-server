"""Domain model behaviour."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.adset import MAX_VARIANTS_PER_AD_SET, AdSet, AdSetCreate
from app.models.campaign import Campaign, CampaignCreate


class TestCampaignTargeting:
    def test_empty_targets_match_everything(self):
        c = Campaign(campaign_name="c", advertiser_company_id="a")
        assert c.matches_geo("US") and c.matches_geo(None)
        assert c.matches_os("ios") and c.matches_os(None)

    def test_geo_match_is_case_insensitive_and_normalised(self):
        c = Campaign(campaign_name="c", advertiser_company_id="a", geo_targets=["US"])
        assert c.matches_geo("us")
        assert c.matches_geo("US")
        assert not c.matches_geo("GB")

    def test_unknown_geo_fails_closed_against_targeted_campaign(self):
        """An unresolvable IP must not receive a geo-targeted campaign."""
        c = Campaign(campaign_name="c", advertiser_company_id="a", geo_targets=["US"])
        assert not c.matches_geo(None)

    def test_unknown_os_fails_closed(self):
        c = Campaign(campaign_name="c", advertiser_company_id="a", os_targets=["ios"])
        assert not c.matches_os("unknown")
        assert not c.matches_os(None)

    def test_create_normalises_geo_to_upper_and_sorted(self):
        c = CampaignCreate(campaign_name="c", advertiser_company_id="a",
                           geo_targets=["us", "ca", "US"])
        assert c.geo_targets == ["CA", "US"]

    def test_new_campaigns_default_inactive(self):
        assert Campaign(campaign_name="c", advertiser_company_id="a").active is False

    @pytest.mark.parametrize(
        "os_name,expected",
        [("ios", "https://ios.example"), ("android", "https://and.example")],
    )
    def test_store_url_selection(self, os_name, expected):
        c = Campaign(campaign_name="c", advertiser_company_id="a",
                     ios_store_url="https://ios.example",
                     android_store_url="https://and.example")
        assert c.store_url_for(os_name) == expected

    def test_store_url_falls_back_when_os_url_missing(self):
        c = Campaign(campaign_name="c", advertiser_company_id="a",
                     ios_store_url="https://ios.example")
        assert c.store_url_for("android") == "https://ios.example"

    def test_invalid_country_code_rejected(self):
        with pytest.raises(ValidationError):
            CampaignCreate(campaign_name="c", advertiser_company_id="a",
                           geo_targets=["USA"])


class TestAdSetExpansion:
    def test_cartesian_product(self):
        s = AdSet(campaign_id="c", ad_set_name="n",
                  character_names=["Luna", "Rex"], video_urls=["v1", "v2"],
                  ctas=["Play", "Install"], ai_prompts=["p"])
        variants = s.build_variants()
        assert len(variants) == 2 * 2 * 2 * 1 == s.variant_count()
        combos = {(v.character_name, v.video_url, v.cta) for v in variants}
        assert len(combos) == 8

    def test_every_variant_links_back_to_set_and_campaign(self):
        s = AdSet(campaign_id="camp_1", ad_set_name="n", character_names=["A"],
                  video_urls=["v"], ctas=["c"], ai_prompts=["p"])
        v = s.build_variants()[0]
        assert v.ad_set_id == s.ad_set_id and v.campaign_id == "camp_1"

    def test_duplicates_are_removed_before_expansion(self):
        """A repeated asset would otherwise double that asset's serve share."""
        c = AdSetCreate(campaign_id="c", ad_set_name="n",
                        character_names=["Luna", "Luna", " Luna "],
                        video_urls=["v"], ctas=["c"], ai_prompts=["p"])
        assert c.character_names == ["Luna"]

    def test_blank_only_list_rejected(self):
        with pytest.raises(ValidationError, match="at least one non-empty"):
            AdSetCreate(campaign_id="c", ad_set_name="n", character_names=["  "],
                        video_urls=["v"], ctas=["c"], ai_prompts=["p"])

    def test_explosion_is_capped(self):
        big = [f"x{i}" for i in range(30)]
        s = AdSet(campaign_id="c", ad_set_name="n", character_names=big,
                  video_urls=big, ctas=["a"], ai_prompts=["p"])
        assert s.variant_count() > MAX_VARIANTS_PER_AD_SET
        with pytest.raises(ValueError, match="over the"):
            s.build_variants()
