"""End-to-end API tests.

Exercises the real routers, dependency graph and serving pipeline against
mongomock + fakeredis. The app's lifespan is bypassed (it would dial real
Mongo/Redis); the singletons it normally builds are constructed here instead,
which also keeps the tests honest about what `app.state` must contain.
"""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.db.mongo import AD_VARIANTS, SERVES
from app.db.seed import seed_from_dir
from app.main import create_app
from app.services.geo import GeoResolver
from app.services.llm import CopyGenerator
from app.services.renderer import TemplateRenderer

IOS = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
ANDROID = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")
US_IP, GB_IP, CN_IP = "214.78.0.1", "2.125.160.217", "175.16.199.1"


@pytest_asyncio.fixture
async def client(settings, fake_db, fake_redis, monkeypatch):
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    get_settings.cache_clear()
    for key, val in {
        "ENVIRONMENT": "test", "API_URL": "https://ads.test", "API_KEY": "test-key",
        "SEED_ON_STARTUP": "false", "LLM_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, val)
    get_settings.cache_clear()

    app = create_app()
    # Build the singletons the (bypassed) lifespan would have built.
    app.state.renderer = TemplateRenderer(settings.template_path)
    app.state.geo = GeoResolver(settings.geoip_path)
    app.state.copy_gen = CopyGenerator(
        settings.assets_dir / "prompts" / "character_dialogue.md",
        api_key=None, model="m", timeout_s=1.0, max_tokens=64, enabled=False,
    )
    await seed_from_dir(fake_db, settings.seed_dir)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    get_settings.cache_clear()


async def make_session(client, ppid="u1", ip=US_IP, ua=IOS) -> str:
    r = await client.post("/session/create", json={"ppid": ppid},
                          headers={"X-Forwarded-For": ip, "User-Agent": ua})
    assert r.status_code == 200
    return r.json()["session_id"]


class TestHealth:
    async def test_healthz(self, client):
        assert (await client.get("/healthz")).status_code == 200


class TestCampaignCrud:
    async def test_seed_loaded(self, client):
        r = await client.get("/campaigns")
        assert r.status_code == 200
        assert {c["campaign_id"] for c in r.json()} == {
            "camp_baba", "camp_galaxy", "camp_kitchen"
        }

    async def test_seeded_campaigns_get_model_defaults(self, client):
        """The seed JSON has no `surface`; without defaults these would be
        invisible to the serve path's {active, surface:'native'} query."""
        r = await client.get("/campaigns/camp_baba")
        assert r.json()["surface"] == "native"

    async def test_create_defaults_to_inactive(self, client):
        r = await client.post("/campaigns", json={
            "campaign_name": "New", "advertiser_company_id": "acme"})
        assert r.status_code == 201
        body = r.json()
        assert body["active"] is False
        assert body["campaign_id"].startswith("camp_")
        assert body["created_at"] and body["updated_at"]

    async def test_create_requires_mandatory_fields(self, client):
        assert (await client.post("/campaigns", json={"campaign_name": "x"})).status_code == 422

    async def test_client_cannot_set_api_owned_fields(self, client):
        """campaign_id/created_at are API-owned -- extra='forbid' rejects them."""
        r = await client.post("/campaigns", json={
            "campaign_name": "X", "advertiser_company_id": "a",
            "campaign_id": "camp_hacked"})
        assert r.status_code == 422

    async def test_filters(self, client):
        assert len((await client.get("/campaigns?active=true")).json()) == 2
        assert len((await client.get("/campaigns?active=false")).json()) == 1
        assert len((await client.get("/campaigns?surface=native")).json()) == 3
        assert len((await client.get("/campaigns?ids=camp_baba,camp_galaxy")).json()) == 2

    async def test_get_one_and_404(self, client):
        assert (await client.get("/campaigns/camp_baba")).status_code == 200
        assert (await client.get("/campaigns/nope")).status_code == 404

    async def test_patch_partial_update(self, client):
        r = await client.patch("/campaigns/camp_baba",
                               json={"active": False, "daily_budget": 750.0})
        assert r.status_code == 200
        body = r.json()
        assert body["active"] is False and body["daily_budget"] == 750.0
        # Untouched fields survive.
        assert body["campaign_name"].startswith("Baba")

    async def test_patch_empty_body_rejected(self, client):
        assert (await client.patch("/campaigns/camp_baba", json={})).status_code == 400

    async def test_patch_404(self, client):
        assert (await client.patch("/campaigns/nope", json={"active": True})).status_code == 404

    async def test_delete(self, client):
        assert (await client.delete("/campaigns/camp_kitchen")).status_code == 204
        assert (await client.get("/campaigns/camp_kitchen")).status_code == 404
        assert (await client.delete("/campaigns/camp_kitchen")).status_code == 404


class TestAdSets:
    async def test_create_generates_cartesian_product(self, client, fake_db):
        r = await client.post("/adsets", json={
            "campaign_id": "camp_baba", "ad_set_name": "Test Set",
            "character_names": ["Luna", "Rex"], "video_urls": ["https://c/a.mp4"],
            "ctas": ["Play", "Install"], "ai_prompts": ["p1"],
            "fallback_copy": ["fb"]})
        assert r.status_code == 201
        body = r.json()
        assert body["variant_count"] == 4
        assert len(body["variants"]) == 4
        assert body["campaign_linked"] is True
        stored = await fake_db[AD_VARIANTS].count_documents(
            {"ad_set_id": body["ad_set"]["ad_set_id"]})
        assert stored == 4

    async def test_links_to_campaign(self, client):
        r = await client.post("/adsets", json={
            "campaign_id": "camp_baba", "ad_set_name": "Linked",
            "character_names": ["A"], "video_urls": ["https://c/a.mp4"],
            "ctas": ["c"], "ai_prompts": ["p"]})
        new_id = r.json()["ad_set"]["ad_set_id"]
        camp = (await client.get("/campaigns/camp_baba")).json()
        assert new_id in camp["native_ad_set_ids"]

    async def test_unknown_campaign_404(self, client):
        r = await client.post("/adsets", json={
            "campaign_id": "nope", "ad_set_name": "x", "character_names": ["A"],
            "video_urls": ["v"], "ctas": ["c"], "ai_prompts": ["p"]})
        assert r.status_code == 404

    async def test_variant_explosion_rejected(self, client):
        big = [f"c{i}" for i in range(30)]
        r = await client.post("/adsets", json={
            "campaign_id": "camp_baba", "ad_set_name": "huge",
            "character_names": big, "video_urls": big, "ctas": ["c"],
            "ai_prompts": ["p"]})
        assert r.status_code == 422

    async def test_empty_asset_list_rejected(self, client):
        r = await client.post("/adsets", json={
            "campaign_id": "camp_baba", "ad_set_name": "x", "character_names": [],
            "video_urls": ["v"], "ctas": ["c"], "ai_prompts": ["p"]})
        assert r.status_code == 422

    async def test_list_variants(self, client):
        r = await client.get("/adsets/adset_native_a/variants")
        assert r.status_code == 200 and len(r.json()) == 4
        active = await client.get("/adsets/adset_native_a/variants?active=true")
        assert len(active.json()) == 3  # var_a_04 is inactive in the seed data


class TestSessions:
    async def test_create_returns_session_id(self, client):
        r = await client.post("/session/create", json={"ppid": "user_42"})
        assert r.status_code == 200
        assert r.json()["session_id"].startswith("sess_")

    async def test_same_ppid_is_stable(self, client):
        a = await make_session(client, "stable")
        b = await make_session(client, "stable")
        assert a == b

    async def test_resolves_by_ip_without_ppid(self, client):
        r1 = await client.post("/session/create", json={},
                               headers={"X-Forwarded-For": US_IP})
        r2 = await client.post("/session/create", json={},
                               headers={"X-Forwarded-For": US_IP})
        assert r1.json()["session_id"] == r2.json()["session_id"]

    async def test_different_ips_get_different_sessions(self, client):
        r1 = await client.post("/session/create", json={},
                               headers={"X-Forwarded-For": US_IP})
        r2 = await client.post("/session/create", json={},
                               headers={"X-Forwarded-For": GB_IP})
        assert r1.json()["session_id"] != r2.json()["session_id"]

    async def test_new_session_after_inactivity(self, client, fake_redis):
        sid = await make_session(client, "expiring")
        await fake_redis.delete("sess:user:ppid:expiring")
        assert await make_session(client, "expiring") != sid


class TestNativeServing:
    async def test_serves_an_ad(self, client):
        sid = await make_session(client)
        r = await client.post("/load/native",
            json={"position": 3, "session_id": sid,
                  "context": {"searchTerm": "space", "tags": ["sci-fi"],
                              "category": "roleplay", "title": "T", "nsfw": False}},
            headers={"X-Forwarded-For": US_IP, "User-Agent": IOS})
        assert r.status_code == 200
        body = r.json()
        assert body["impression_id"].startswith("imp_")
        html = body["rendered_html"]
        assert html.lstrip().startswith("<!DOCTYPE html>")
        assert "{{" not in html

    async def test_geo_filter_excludes_non_matching_country(self, client):
        """Only camp_kitchen targets GB and it is inactive -> nothing eligible."""
        sid = await make_session(client, "gb", ip=GB_IP, ua=ANDROID)
        r = await client.post("/load/native", json={"position": 1, "session_id": sid},
                              headers={"X-Forwarded-For": GB_IP, "User-Agent": ANDROID})
        assert r.status_code == 204

    async def test_activating_campaign_makes_it_servable(self, client):
        await client.patch("/campaigns/camp_kitchen", json={"active": True})
        sid = await make_session(client, "gb2", ip=GB_IP, ua=ANDROID)
        r = await client.post("/load/native", json={"position": 1, "session_id": sid},
                              headers={"X-Forwarded-For": GB_IP, "User-Agent": ANDROID})
        assert r.status_code == 200
        assert "Chef" in r.json()["rendered_html"] or "kitchen" in r.json()["rendered_html"]

    async def test_os_filter(self, client):
        """camp_galaxy is iOS-only; an Android caller in the US gets baba."""
        sid = await make_session(client, "andr", ua=ANDROID)
        r = await client.post("/load/native", json={"position": 1, "session_id": sid},
                              headers={"X-Forwarded-For": US_IP, "User-Agent": ANDROID})
        assert r.status_code == 200
        assert "acmp_baba" in r.json()["rendered_html"]

    async def test_unresolvable_geo_fails_closed(self, client):
        sid = await make_session(client, "cn", ip=CN_IP)
        r = await client.post("/load/native", json={"position": 1, "session_id": sid},
                              headers={"X-Forwarded-For": CN_IP, "User-Agent": IOS})
        assert r.status_code == 204

    async def test_unknown_session_404(self, client):
        r = await client.post("/load/native",
                              json={"position": 1, "session_id": "sess_nope"})
        assert r.status_code == 404

    async def test_position_required(self, client):
        sid = await make_session(client)
        assert (await client.post("/load/native", json={"session_id": sid})).status_code == 422

    async def test_negative_position_rejected(self, client):
        sid = await make_session(client)
        r = await client.post("/load/native", json={"position": -1, "session_id": sid})
        assert r.status_code == 422

    async def test_context_is_optional(self, client):
        sid = await make_session(client, "noctx")
        r = await client.post("/load/native", json={"position": 0, "session_id": sid},
                              headers={"X-Forwarded-For": US_IP, "User-Agent": IOS})
        assert r.status_code == 200

    async def test_serve_is_written_to_mongo(self, client, fake_db):
        sid = await make_session(client, "writer")
        r = await client.post("/load/native", json={"position": 2, "session_id": sid},
                              headers={"X-Forwarded-For": US_IP, "User-Agent": IOS})
        imp = r.json()["impression_id"]
        doc = await fake_db[SERVES].find_one({"impression_id": imp})
        assert doc is not None
        assert doc["country"] == "US" and doc["os"] == "ios"
        assert doc["session_id"] == sid
        assert doc["decision"]["candidates"]

    async def test_fallback_copy_used_when_llm_disabled(self, client, fake_db):
        sid = await make_session(client, "fb")
        r = await client.post("/load/native", json={"position": 1, "session_id": sid},
                              headers={"X-Forwarded-For": US_IP, "User-Agent": IOS})
        doc = await fake_db[SERVES].find_one({"impression_id": r.json()["impression_id"]})
        assert doc["copy_source"] == "fallback"
        assert doc["char_message"]


class TestClickTracking:
    async def test_click_recorded_once(self, client):
        sid = await make_session(client, "clicker")
        r = await client.post("/load/native", json={"position": 1, "session_id": sid},
                              headers={"X-Forwarded-For": US_IP, "User-Agent": IOS})
        imp = r.json()["impression_id"]
        first = await client.post(f"/impressions/{imp}/click")
        assert first.status_code == 202 and first.json()["counted"] is True
        second = await client.post(f"/impressions/{imp}/click")
        assert second.json()["counted"] is False

    async def test_unknown_impression_is_accepted_not_404(self, client):
        """Click pixels are retried by browsers; a 404 would invite retry storms."""
        r = await client.post("/impressions/imp_nope/click")
        assert r.status_code == 202 and r.json()["counted"] is False
