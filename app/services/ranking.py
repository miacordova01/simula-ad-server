"""Campaign ranking.

The part-1 LightGBM model can't be called here, and it's worth being precise
why: it was trained on site_id, device_ip, C14-C21 and a character_id from a 5k
catalogue. None of those columns exist in this system. Feeding it zeros gives
confident nonsense.

What transfers is the architecture:

    score = pCTR_ucb * bid * pacing * fatigue

Hard filters before scoring, an uncertainty bonus so cold campaigns get
explored, multiplicative modifiers instead of cliffs. CTRScorer is a Protocol
so the estimator swaps:

  BetaBanditScorer - decayed Beta posterior, shrunk toward the campaign's
    global rate then the prior. Right default when you start with no history.
  ModelScorer - seam for a model trained on THIS system's features.
    Serve.to_feature_row() already emits that training set.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Protocol

from ..models.campaign import Campaign
from .features import CampaignStats

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScoringContext:
    country: str | None
    os: str | None
    category: str | None
    position: int
    nsfw: bool
    bucket: str
    user_ctr: float | None = None


@dataclass
class ScoredCampaign:
    campaign: Campaign
    p_ctr: float
    p_ctr_ucb: float
    bid: float
    pacing_mult: float = 1.0
    fatigue_mult: float = 1.0
    score: float = 0.0
    reason: str = ""


class CTRScorer(Protocol):
    def score(
        self, campaign: Campaign, stats: tuple[CampaignStats, CampaignStats],
        ctx: ScoringContext,
    ) -> tuple[float, float]:
        """Return (p_ctr, p_ctr_ucb)."""
        ...


class BetaBanditScorer:
    """Beta-posterior CTR estimate with an upper-confidence bonus.

    Two-stage shrinkage. The bucketed rate is what we actually want but is
    thin early on, so it is shrunk toward the campaign's global rate, which is
    in turn shrunk toward the platform prior. A campaign with no data anywhere
    scores exactly at the prior and carries the widest confidence interval,
    which is what earns it exploration traffic.
    """

    def __init__(
        self,
        prior_ctr: float = 0.02,
        prior_strength: float = 20.0,
        global_strength: float = 40.0,
        explore_z: float = 1.0,
    ) -> None:
        self.prior_ctr = prior_ctr
        self.prior_strength = prior_strength
        self.global_strength = global_strength
        self.explore_z = explore_z

    def score(
        self, campaign: Campaign, stats: tuple[CampaignStats, CampaignStats],
        ctx: ScoringContext,
    ) -> tuple[float, float]:
        bucket_stats, global_stats = stats

        g_a = global_stats.clicks + self.prior_ctr * self.prior_strength
        g_b = (global_stats.impressions - global_stats.clicks) + (
            1 - self.prior_ctr
        ) * self.prior_strength
        g_mean = g_a / max(g_a + g_b, 1e-9)

        a = bucket_stats.clicks + g_mean * self.global_strength
        b = (bucket_stats.impressions - bucket_stats.clicks) + (
            1 - g_mean
        ) * self.global_strength
        a, b = max(a, 1e-6), max(b, 1e-6)

        n = a + b
        mean = a / n
        sd = math.sqrt(a * b / (n * n * (n + 1)))
        return mean, min(mean + self.explore_z * sd, 1.0)


class ModelScorer:
    """Adapter for a learned model trained on this system's own features.

    Deliberately not wired to the offline booster (see module docstring). It
    takes any object exposing `predict_proba(list[dict]) -> list[float]` and
    falls back to the bandit on any failure, so a bad model deploy degrades to
    the previous behaviour instead of taking the serve path down.
    """

    def __init__(self, model, fallback: CTRScorer) -> None:
        self.model = model
        self.fallback = fallback

    def score(
        self, campaign: Campaign, stats: tuple[CampaignStats, CampaignStats],
        ctx: ScoringContext,
    ) -> tuple[float, float]:
        try:
            row = {
                "campaign_id": campaign.campaign_id,
                "advertiser_company_id": campaign.advertiser_company_id,
                "country": ctx.country,
                "os": ctx.os,
                "category": ctx.category,
                "position": ctx.position,
                "nsfw": int(ctx.nsfw),
                "bucket_imp": stats[0].impressions,
                "bucket_clk": stats[0].clicks,
                "global_imp": stats[1].impressions,
                "global_clk": stats[1].clicks,
                "user_ctr": ctx.user_ctr or 0.0,
            }
            p = float(self.model.predict_proba([row])[0])
            p = min(max(p, 1e-6), 1.0)
            # Uncertainty still comes from observed counts -- a point estimate
            # alone gives the ranker no way to explore.
            n = stats[1].impressions + 1.0
            sd = math.sqrt(max(p * (1 - p), 1e-9) / n)
            return p, min(p + sd, 1.0)
        except Exception:
            log.exception("model scorer failed; falling back to bandit")
            return self.fallback.score(campaign, stats, ctx)


def pacing_multiplier(
    daily_budget: float | None, spend_today: float, fraction_of_day: float,
    floor: float = 0.1, ceil: float = 2.0,
) -> float:
    """Keep an advertiser's spend on an even glide path through the day.

    A soft multiplier rather than a hard stop: cutting a campaign dead at
    budget exhaustion produces a visible CTR cliff at the same hour every day
    and starves the late-day auction.
    """
    if not daily_budget or daily_budget <= 0:
        return 1.0
    if spend_today >= daily_budget:
        return floor
    target = daily_budget * max(fraction_of_day, 1e-3)
    ratio = target / max(spend_today, 1e-6)
    return float(min(max(ratio, floor), ceil))


def fatigue_multiplier(seen_count: int, cap: int, decay: float = 0.6) -> float:
    """Geometric damping of repeat exposure, with a hard cap as a backstop."""
    if seen_count >= cap:
        return 0.0
    return float(decay**seen_count)


class CampaignRanker:
    """Orchestrates scoring -> business modifiers -> ordering."""

    def __init__(
        self, scorer: CTRScorer, fatigue_cap: int = 4, default_bid: float = 1.0,
    ) -> None:
        self.scorer = scorer
        self.fatigue_cap = fatigue_cap
        self.default_bid = default_bid

    def bid_for(self, campaign: Campaign) -> float:
        """Stand-in for an auction bid.

        The seed data has no bid field, only `daily_budget`. Budget is a
        spend ceiling, not a price, so using it directly as a bid would be
        wrong -- it would rank a big-budget campaign above a better-performing
        small one purely on wallet size. A mild log scaling keeps larger
        budgets modestly preferred (they need the volume) without letting
        budget dominate measured performance. A real system reads a CPC bid
        off the campaign; noted in the README.
        """
        if not campaign.daily_budget or campaign.daily_budget <= 0:
            return self.default_bid
        return self.default_bid * (1.0 + math.log10(1.0 + campaign.daily_budget / 100.0))

    def rank(
        self,
        campaigns: list[Campaign],
        stats: dict[str, tuple[CampaignStats, CampaignStats]],
        fatigue: dict[str, int],
        ctx: ScoringContext,
        spend: dict[str, float] | None = None,
        fraction_of_day: float = 0.5,
    ) -> list[ScoredCampaign]:
        spend = spend or {}
        out: list[ScoredCampaign] = []

        for c in campaigns:
            cid = c.campaign_id
            cstats = stats.get(cid, (CampaignStats(), CampaignStats()))
            p, p_ucb = self.scorer.score(c, cstats, ctx)

            pace = pacing_multiplier(
                c.daily_budget, spend.get(cid, 0.0), fraction_of_day
            )
            fat = fatigue_multiplier(fatigue.get(cid, 0), self.fatigue_cap)
            bid = self.bid_for(c)
            score = p_ucb * bid * pace * fat

            bits: list[str] = []
            if cstats[1].impressions < 50:
                bits.append(f"cold campaign (n={int(cstats[1].impressions)}), "
                            f"+{p_ucb - p:.4f} explore bonus")
            if fat == 0.0:
                bits.append(f"frequency cap hit ({fatigue.get(cid,0)}/{self.fatigue_cap})")
            elif fat < 1.0:
                bits.append(f"fatigue x{fat:.2f}")
            if pace < 0.95:
                bits.append(f"pacing throttle x{pace:.2f}")
            elif pace > 1.05:
                bits.append(f"pacing boost x{pace:.2f}")
            if not bits:
                bits.append("scored on measured rate")

            out.append(
                ScoredCampaign(
                    campaign=c, p_ctr=p, p_ctr_ucb=p_ucb, bid=bid,
                    pacing_mult=pace, fatigue_mult=fat, score=score,
                    reason="; ".join(bits),
                )
            )

        out.sort(key=lambda s: s.score, reverse=True)
        return out
