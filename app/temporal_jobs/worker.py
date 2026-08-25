"""Temporal worker process.

Runs as its own container so scheduled work cannot compete with the serve path
for CPU, and so the API can be scaled to zero without stopping the schedules.

Also installs the schedules on boot (idempotently), which keeps schedule
definitions in version control instead of clicked into a UI.
"""

from __future__ import annotations

import asyncio
import logging

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)
from temporalio.worker import Worker

from ..config import get_settings
from .activities import decay_ctr_counters, refresh_campaign_cache
from .client import connect
from .workflows import DecayCtrCountersWorkflow, RefreshCampaignCacheWorkflow

logging.basicConfig(
    level=get_settings().log_level,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("simula.temporal")

CACHE_SCHEDULE_ID = "simula-campaign-cache-hourly"
DECAY_SCHEDULE_ID = "simula-ctr-decay-6h"


async def ensure_schedules(client: Client, task_queue: str) -> None:
    """Create the schedules if absent. Safe to run on every worker boot."""
    from datetime import timedelta

    wanted = [
        (
            CACHE_SCHEDULE_ID,
            RefreshCampaignCacheWorkflow.__name__,
            timedelta(hours=1),
            "hourly campaign cache refresh (the brief's requirement)",
        ),
        (
            DECAY_SCHEDULE_ID,
            DecayCtrCountersWorkflow.__name__,
            timedelta(hours=6),
            "decay bandit evidence so stale performance fades",
        ),
    ]

    for schedule_id, workflow_name, interval, note in wanted:
        try:
            await client.create_schedule(
                schedule_id,
                Schedule(
                    action=ScheduleActionStartWorkflow(
                        workflow_name,
                        id=f"{schedule_id}-wf",
                        task_queue=task_queue,
                    ),
                    spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=interval)]),
                    # SKIP, not BUFFER_ALL: if a refresh is somehow still
                    # running when the next hour fires, the right move is to
                    # skip it. Queueing them would build a backlog of redundant
                    # full-cache rebuilds.
                    policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
                ),
            )
            log.info("created schedule %s (every %s) -- %s", schedule_id, interval, note)
        except ScheduleAlreadyRunningError:
            log.info("schedule %s already exists", schedule_id)
        except Exception:
            log.exception("could not create schedule %s", schedule_id)


async def main() -> None:
    settings = get_settings()
    client = await connect(settings)
    await ensure_schedules(client, settings.temporal_task_queue)

    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[RefreshCampaignCacheWorkflow, DecayCtrCountersWorkflow],
        activities=[refresh_campaign_cache, decay_ctr_counters],
    )
    log.info("worker polling task queue %s", settings.temporal_task_queue)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
