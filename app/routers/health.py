"""Health and readiness."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from ..db.mongo import get_db
from ..db.redis_client import get_redis
from ..deps import SettingsDep

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness -- is the process up. No dependencies touched."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(response: Response, settings: SettingsDep) -> dict[str, object]:
    """Readiness -- can we actually serve. Checks Mongo and Redis.

    Split from liveness on purpose: a Redis blip should stop traffic being
    routed here, but must not get the container killed and restarted.
    """
    checks: dict[str, object] = {}
    ok = True

    try:
        await get_db().command("ping")
        checks["mongo"] = "ok"
    except Exception as exc:
        checks["mongo"] = f"error: {type(exc).__name__}"
        ok = False

    try:
        await get_redis().ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"
        ok = False

    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if ok else "degraded", "checks": checks,
            "environment": settings.environment}
