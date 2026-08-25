"""Dependency wiring.

Singletons that are expensive to build (the GeoIP reader, the parsed template,
the Anthropic client) are constructed once during app startup and stashed on
`app.state`. Request-scoped objects are thin wrappers over those, so a request
never does I/O or file parsing just to assemble its dependencies.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request

from .config import Settings, get_settings
from .db.mongo import get_db
from .db.redis_client import get_redis
from .services.campaign_cache import CampaignCache, CampaignRepository
from .services.features import FeatureStore
from .services.geo import GeoResolver, client_ip
from .services.llm import CopyGenerator
from .services.ranking import BetaBanditScorer, CampaignRanker
from .services.renderer import TemplateRenderer
from .services.serving import AdServer
from .services.sessions import SessionService
from .services.useragent import DeviceInfo, parse_device

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_campaign_cache(settings: SettingsDep) -> CampaignCache:
    return CampaignCache(get_redis(), settings.campaign_cache_key, settings.campaign_cache_ttl_s)


def get_campaign_repo(
    cache: Annotated[CampaignCache, Depends(get_campaign_cache)],
) -> CampaignRepository:
    return CampaignRepository(get_db(), cache)


def get_feature_store() -> FeatureStore:
    return FeatureStore(get_redis())


def get_session_service(settings: SettingsDep) -> SessionService:
    return SessionService(get_redis(), settings.session_idle_ttl_s)


def get_geo(request: Request) -> GeoResolver:
    return request.app.state.geo


def get_renderer(request: Request) -> TemplateRenderer:
    return request.app.state.renderer


def get_copy_gen(request: Request) -> CopyGenerator:
    return request.app.state.copy_gen


def get_ad_server(
    request: Request,
    settings: SettingsDep,
    repo: Annotated[CampaignRepository, Depends(get_campaign_repo)],
    features: Annotated[FeatureStore, Depends(get_feature_store)],
) -> AdServer:
    ranker = CampaignRanker(
        scorer=BetaBanditScorer(), fatigue_cap=settings.fatigue_cap_per_campaign
    )
    return AdServer(
        db=get_db(),
        redis=get_redis(),
        campaigns=repo,
        features=features,
        ranker=ranker,
        renderer=request.app.state.renderer,
        copy_gen=request.app.state.copy_gen,
        api_url=settings.api_url,
        api_key=settings.api_key,
        fatigue_window_s=settings.fatigue_window_s,
        copy_cache_ttl_s=settings.copy_cache_ttl_s,
    )


class RequestIdentity:
    """Everything we can infer about the caller from transport headers."""

    def __init__(self, ip: str | None, device: DeviceInfo, user_agent: str | None) -> None:
        self.ip = ip
        self.device = device
        self.user_agent = user_agent

    @property
    def country_source_ip(self) -> str | None:
        return self.ip


def get_identity(
    request: Request,
    x_forwarded_for: Annotated[str | None, Header(alias="X-Forwarded-For")] = None,
    x_real_ip: Annotated[str | None, Header(alias="X-Real-IP")] = None,
    user_agent: Annotated[str | None, Header(alias="User-Agent")] = None,
) -> RequestIdentity:
    peer = request.client.host if request.client else None
    ip = client_ip(x_forwarded_for, x_real_ip, peer)
    return RequestIdentity(ip=ip, device=parse_device(user_agent), user_agent=user_agent)


CampaignRepoDep = Annotated[CampaignRepository, Depends(get_campaign_repo)]
SessionServiceDep = Annotated[SessionService, Depends(get_session_service)]
AdServerDep = Annotated[AdServer, Depends(get_ad_server)]
GeoDep = Annotated[GeoResolver, Depends(get_geo)]
IdentityDep = Annotated[RequestIdentity, Depends(get_identity)]
FeatureStoreDep = Annotated[FeatureStore, Depends(get_feature_store)]
