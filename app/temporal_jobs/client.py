"""Temporal connection helper.

Handles both local dev (plain TCP against the dev server) and Temporal Cloud
(TLS + API key), because the deployed service uses Cloud and the test suite
uses neither.
"""

from __future__ import annotations

import logging

from temporalio.client import Client

from ..config import Settings

log = logging.getLogger(__name__)


async def connect(settings: Settings) -> Client:
    kwargs: dict = {
        "target_host": settings.temporal_target,
        "namespace": settings.temporal_namespace,
    }
    if settings.temporal_api_key:
        kwargs["api_key"] = settings.temporal_api_key
        kwargs["tls"] = True
    elif settings.temporal_tls:
        kwargs["tls"] = True
    log.info(
        "connecting to temporal %s ns=%s tls=%s",
        settings.temporal_target, settings.temporal_namespace,
        bool(kwargs.get("tls")),
    )
    return await Client.connect(**kwargs)
