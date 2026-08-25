"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import mongo, redis_client
from .db.seed import seed_from_dir
from .routers import adsets, campaigns, health, serve, sessions
from .services.campaign_cache import CampaignCache, CampaignRepository
from .services.geo import GeoResolver
from .services.llm import CopyGenerator
from .services.renderer import TemplateRenderer

logging.basicConfig(
    level=get_settings().log_level,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("simula")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build expensive singletons once, tear them down cleanly."""
    settings = get_settings()
    log.info("starting %s (env=%s)", settings.app_name, settings.environment)

    db = await mongo.connect(settings.mongo_uri, settings.mongo_db)
    await mongo.ensure_indexes(db)
    await redis_client.connect(settings.redis_url)

    if settings.seed_on_startup:
        written = await seed_from_dir(db, settings.seed_dir)
        log.info("seed complete: %s", written)

    # Parsed once: the template is ~18KB of regex work and the GeoIP file is
    # memory-mapped. Neither belongs on the request path.
    app.state.renderer = TemplateRenderer(settings.template_path)
    app.state.geo = GeoResolver(settings.geoip_path)
    app.state.copy_gen = CopyGenerator(
        prompt_path=settings.assets_dir / "prompts" / "character_dialogue.md",
        api_key=settings.anthropic_api_key,
        model=settings.llm_model,
        timeout_s=settings.llm_timeout_s,
        max_tokens=settings.llm_max_tokens,
        enabled=settings.llm_enabled,
    )

    # Warm the campaign cache so the first real request is not the one that
    # pays for the cold read.
    try:
        cache = CampaignCache(
            redis_client.get_redis(), settings.campaign_cache_key,
            settings.campaign_cache_ttl_s,
        )
        n = await CampaignRepository(db, cache).refresh_cache()
        log.info("campaign cache warmed with %d campaigns", n)
    except Exception:
        log.exception("failed to warm campaign cache; serve path will self-heal")

    yield

    app.state.geo.close()
    await redis_client.disconnect()
    await mongo.disconnect()
    log.info("shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Simula Native Ad Server",
        version="1.0.0",
        description=(
            "Campaign/ad-set management, session resolution and native ad "
            "serving with geo + OS targeting, bandit ranking and LLM ad copy."
        ),
        lifespan=lifespan,
    )

    app.include_router(health.router)
    app.include_router(campaigns.router)
    app.include_router(adsets.router)
    app.include_router(sessions.router)
    app.include_router(serve.router)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Log the stack, return a generic body. Leaking exception text to an
        # ad-serving client tells an attacker about internals and helps nobody.
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "internal server error"},
        )

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": settings.app_name,
            "docs": "/docs",
            "health": "/healthz",
        }

    return app


app = create_app()
