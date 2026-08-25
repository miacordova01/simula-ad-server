"""Temporal workflows.

Kept deliberately thin: a workflow must be deterministic, so anything touching
a clock, a socket or a random number lives in an activity. These just describe
retry policy and call out.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .activities import decay_ctr_counters, refresh_campaign_cache


@workflow.defn(name="RefreshCampaignCacheWorkflow")
class RefreshCampaignCacheWorkflow:
    @workflow.run
    async def run(self) -> dict:
        return await workflow.execute_activity(
            refresh_campaign_cache,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=5),
                backoff_coefficient=2.0,
                # Bounded: this runs hourly, so a run that cannot succeed in a
                # few minutes should fail and alert rather than pile up against
                # the next scheduled firing.
                maximum_attempts=4,
            ),
        )


@workflow.defn(name="DecayCtrCountersWorkflow")
class DecayCtrCountersWorkflow:
    @workflow.run
    async def run(self, factor: float = 0.5) -> dict:
        return await workflow.execute_activity(
            decay_ctr_counters,
            factor,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
