"""Test fixtures.

Real Mongo/Redis are replaced with mongomock-motor and fakeredis so the suite
runs with no containers. The seams (`mongo.set_db`, `redis_client.set_redis`)
exist for exactly this.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import fakeredis.aioredis
import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient

from app.config import Settings, get_settings
from app.db import mongo, redis_client

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def settings() -> Settings:
    get_settings.cache_clear()
    s = Settings(
        environment="test",
        api_url="https://ads.test",
        api_key="test-key",
        assets_dir=ROOT / "assets",
        seed_on_startup=False,
        temporal_enabled=False,
        llm_enabled=False,
        anthropic_api_key=None,
        session_idle_ttl_s=30,
    )
    return s


@pytest_asyncio.fixture
async def fake_redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    redis_client.set_redis(client)
    yield client
    await client.flushall()
    await client.aclose()


@pytest_asyncio.fixture
async def fake_db():
    client = AsyncMongoMockClient()
    db = client["simula_test"]
    mongo.set_db(db)
    yield db


@pytest.fixture
def template_path() -> Path:
    return ROOT / "assets" / "template" / "character_ad.html"


@pytest.fixture
def prompt_path() -> Path:
    return ROOT / "assets" / "prompts" / "character_dialogue.md"


@pytest.fixture
def geoip_path() -> Path:
    return ROOT / "assets" / "data" / "GeoLite2-City-Test.mmdb"
