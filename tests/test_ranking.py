"""Ranking, exploration and business modifiers."""

from __future__ import annotations

import pytest

from app.models.campaign import Campaign
from app.services.features import CampaignStats, context_bucket, position_band
from app.services.ranking import (
    BetaBanditScorer,
    CampaignRanker,
    ModelScorer,
    ScoringContext,
    fatigue_multiplier,
    pacing_multiplier,
)


def camp(cid: str, budget: float | None = None) -> Campaign:
    return Campaign(campaign_id=cid, campaign_name=cid,
                    advertiser_company_id="adv", daily_budget=budget, active=True)


CTX = ScoringContext("US", "ios", "roleplay", 3, False,
                     context_bucket("US", "ios", "roleplay", 3))


class TestBetaBanditScorer:
    def test_no_data_scores_at_prior(self):
        s = BetaBanditScorer(prior_ctr=0.02)
        p, ucb = s.score(camp("c"), (CampaignStats(), CampaignStats()), CTX)
        assert p == pytest.approx(0.02, abs=1e-3)
        assert ucb > p  # uncertainty bonus present

    def test_more_evidence_narrows_the_interval(self):
        s = BetaBanditScorer()
        _, ucb_thin = s.score(camp("c"), (CampaignStats(10, 1), CampaignStats(10, 1)), CTX)
        _, ucb_thick = s.score(camp("c"), (CampaignStats(10000, 1000), CampaignStats(10000, 1000)), CTX)
        thin_p, _ = s.score(camp("c"), (CampaignStats(10, 1), CampaignStats(10, 1)), CTX)
        thick_p, _ = s.score(camp("c"), (CampaignStats(10000, 1000), CampaignStats(10000, 1000)), CTX)
        assert (ucb_thin - thin_p) > (ucb_thick - thick_p)

    def test_high_ctr_scores_above_low_ctr(self):
        s = BetaBanditScorer()
        hi, _ = s.score(camp("a"), (CampaignStats(1000, 100), CampaignStats(1000, 100)), CTX)
        lo, _ = s.score(camp("b"), (CampaignStats(1000, 5), CampaignStats(1000, 5)), CTX)
        assert hi > lo

    def test_thin_bucket_shrinks_toward_global(self):
        """One click in a new bucket must not imply a 100% CTR."""
        s = BetaBanditScorer()
        p, _ = s.score(camp("c"), (CampaignStats(1, 1), CampaignStats(10000, 200)), CTX)
        assert p < 0.10


class TestModifiers:
    def test_pacing_no_budget_is_neutral(self):
        assert pacing_multiplier(None, 0, 0.5) == 1.0

    def test_pacing_throttles_overspender(self):
        assert pacing_multiplier(1000, 900, 0.5) < 1.0

    def test_pacing_boosts_underspender(self):
        assert pacing_multiplier(1000, 100, 0.5) > 1.0

    def test_exhausted_budget_throttles_but_never_zero(self):
        """A soft floor avoids a CTR cliff at the same hour every day."""
        assert 0 < pacing_multiplier(1000, 1000, 0.5) <= 0.1

    def test_fatigue_decays_then_hard_caps(self):
        vals = [fatigue_multiplier(i, cap=4) for i in range(5)]
        assert vals[0] == 1.0
        assert vals[0] > vals[1] > vals[2] > vals[3] > 0
        assert vals[4] == 0.0


class TestCampaignRanker:
    def test_orders_by_score(self):
        r = CampaignRanker(BetaBanditScorer())
        out = r.rank(
            [camp("lo"), camp("hi")],
            {"lo": (CampaignStats(1000, 10), CampaignStats(1000, 10)),
             "hi": (CampaignStats(1000, 200), CampaignStats(1000, 200))},
            {}, CTX,
        )
        assert out[0].campaign.campaign_id == "hi"

    def test_frequency_capped_campaign_scores_zero(self):
        r = CampaignRanker(BetaBanditScorer(), fatigue_cap=3)
        out = r.rank([camp("a")], {}, {"a": 3}, CTX)
        assert out[0].score == 0.0
        assert "frequency cap" in out[0].reason

    def test_cold_campaign_is_flagged_and_boosted(self):
        r = CampaignRanker(BetaBanditScorer())
        out = r.rank([camp("cold")], {}, {}, CTX)
        assert "cold campaign" in out[0].reason
        assert out[0].p_ctr_ucb > out[0].p_ctr

    def test_budget_does_not_dominate_measured_performance(self):
        """A huge budget must not outrank a clearly better performer."""
        r = CampaignRanker(BetaBanditScorer())
        out = r.rank(
            [camp("rich", budget=100000.0), camp("good", budget=100.0)],
            {"rich": (CampaignStats(5000, 10), CampaignStats(5000, 10)),
             "good": (CampaignStats(5000, 900), CampaignStats(5000, 900))},
            {}, CTX, spend={"rich": 0.0, "good": 0.0},
        )
        assert out[0].campaign.campaign_id == "good"

    def test_empty_input(self):
        assert CampaignRanker(BetaBanditScorer()).rank([], {}, {}, CTX) == []


class TestModelScorerFallback:
    def test_broken_model_falls_back_to_bandit(self):
        class Boom:
            def predict_proba(self, rows):
                raise RuntimeError("model exploded")

        bandit = BetaBanditScorer()
        scorer = ModelScorer(Boom(), fallback=bandit)
        got = scorer.score(camp("c"), (CampaignStats(), CampaignStats()), CTX)
        assert got == bandit.score(camp("c"), (CampaignStats(), CampaignStats()), CTX)

    def test_working_model_is_used(self):
        class Fixed:
            def predict_proba(self, rows):
                return [0.42]

        p, ucb = ModelScorer(Fixed(), BetaBanditScorer()).score(
            camp("c"), (CampaignStats(), CampaignStats()), CTX
        )
        assert p == pytest.approx(0.42)
        assert ucb >= p


class TestBuckets:
    def test_bucket_is_stable_and_readable(self):
        assert context_bucket("us", "ios", "RolePlay", 3, False) == "US|ios|roleplay|p3_5|sfw"

    def test_missing_values_get_placeholders(self):
        assert context_bucket(None, None, None, 0, True) == "XX|unknown|none|p0|nsfw"

    @pytest.mark.parametrize("pos,band", [(0,"p0"),(1,"p1_2"),(2,"p1_2"),(5,"p3_5"),(10,"p6_10"),(99,"p11plus")])
    def test_position_bands(self, pos, band):
        assert position_band(pos) == band
