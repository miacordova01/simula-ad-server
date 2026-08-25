"""Application settings.

All configuration is environment-driven so the same image runs locally under
docker-compose and in Cloud Run without code changes.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- service ---------------------------------------------------------
    app_name: str = "simula-ad-server"
    environment: str = "local"
    log_level: str = "INFO"
    # Public base URL baked into rendered creatives so the click tracker in the
    # template can call back. Must be the externally reachable URL, not 0.0.0.0.
    api_url: str = "http://localhost:8080"
    api_key: str = "dev-key"

    # --- mongo -----------------------------------------------------------
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "simula"

    # --- redis -----------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"

    # Campaign cache. The Temporal schedule rebuilds this hourly; the TTL is
    # deliberately longer than the refresh interval so a late/failed refresh
    # degrades to slightly stale data rather than an empty cache and a
    # thundering herd onto Mongo.
    campaign_cache_ttl_s: int = 3 * 3600
    campaign_cache_key: str = "cache:campaigns:active"

    # --- sessions --------------------------------------------------------
    # "Only mint a new session after 30s of inactivity" -- implemented as a
    # Redis TTL that is refreshed on every serve.
    session_idle_ttl_s: int = 30

    # --- frequency capping ----------------------------------------------
    fatigue_window_s: int = 3600
    fatigue_cap_per_campaign: int = 4

    # --- LLM -------------------------------------------------------------
    anthropic_api_key: str | None = None
    # Ad copy is a one-line generation sitting on the serve path, so effort is
    # pinned low. Thinking is left adaptive rather than disabled: disabling it
    # can leak reasoning into the visible answer, which would become ad copy.
    llm_model: str = "claude-opus-5"
    llm_timeout_s: float = 2.5
    llm_max_tokens: int = 256
    llm_enabled: bool = True
    # Generated copy is cached per variant so we do not pay latency on every
    # serve for a line that is deterministic given (character, ai_prompt).
    copy_cache_ttl_s: int = 6 * 3600

    # --- assets ----------------------------------------------------------
    assets_dir: Path = ROOT / "assets"

    @property
    def template_path(self) -> Path:
        return self.assets_dir / "template" / "character_ad.html"

    @property
    def geoip_path(self) -> Path:
        return self.assets_dir / "data" / "GeoLite2-City-Test.mmdb"

    @property
    def seed_dir(self) -> Path:
        return self.assets_dir / "data"

    # --- temporal --------------------------------------------------------
    temporal_target: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "simula-ads"
    temporal_api_key: str | None = None
    temporal_tls: bool = False
    # Set false in tests / when no Temporal server is reachable.
    temporal_enabled: bool = True

    seed_on_startup: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
