"""End-to-end demo against a running API.

Walks the whole surface -- CRUD, ad set expansion, session semantics, the geo x
OS targeting matrix, serving, and click tracking -- and writes the rendered ads
to `samples/` so they can be opened in a browser.

    python scripts/demo.py                       # against localhost:8080
    python scripts/demo.py https://my-service    # against a deployment
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"

IOS = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
ANDROID = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")
DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

TEST_IPS = {"US": "214.78.0.1", "GB": "2.125.160.217", "SE": "89.160.20.113",
            "CN": "175.16.199.1", "PH": "202.196.224.1", "BT": "67.43.156.1"}


def rule(title: str) -> None:
    print(f"\n{'=' * 74}\n  {title}\n{'=' * 74}")


def js_const(html: str, name: str) -> str:
    m = re.search(rf'const {name}\s*=\s*"(.*?)";', html)
    return m.group(1) if m else "?"


class Demo:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.c = httpx.Client(base_url=self.base, timeout=30.0)

    def session(self, ppid: str | None, ip: str, ua: str) -> str:
        r = self.c.post("/session/create", json={"ppid": ppid} if ppid else {},
                        headers={"X-Forwarded-For": ip, "User-Agent": ua})
        r.raise_for_status()
        return r.json()["session_id"]

    def serve(self, sid: str, ip: str, ua: str, position: int = 1,
              context: dict | None = None) -> httpx.Response:
        body: dict = {"position": position, "session_id": sid}
        if context:
            body["context"] = context
        return self.c.post("/load/native", json=body,
                           headers={"X-Forwarded-For": ip, "User-Agent": ua})

    # ------------------------------------------------------------------
    def run(self) -> None:
        SAMPLES.mkdir(exist_ok=True)

        rule("1. Health")
        print(json.dumps(self.c.get("/readyz").json(), indent=2))

        rule("2. Campaigns loaded from the provided seed data")
        for c in self.c.get("/campaigns").json():
            print(f"  {c['campaign_id']:14} active={c['active']!s:5} "
                  f"geo={c['geo_targets']} os={c['os_targets']} "
                  f"budget={c['daily_budget']}")

        rule("3. Campaign CRUD")
        created = self.c.post("/campaigns", json={
            "campaign_name": "Demo Campaign", "advertiser_company_id": "acmp_demo",
            "daily_budget": 300.0, "geo_targets": ["us"], "os_targets": ["ios"],
            "ios_store_url": "https://apps.apple.com/app/id999",
        }).json()
        cid = created["campaign_id"]
        print(f"  created {cid}  active={created['active']}  (defaults to inactive)")
        patched = self.c.patch(f"/campaigns/{cid}",
                               json={"active": True, "daily_budget": 450.0}).json()
        print(f"  patched -> active={patched['active']} budget={patched['daily_budget']}")

        rule("4. Ad set creation expands to the cartesian product")
        adset = self.c.post("/adsets", json={
            "campaign_id": cid, "ad_set_name": "Demo Heroes",
            "character_names": ["Luna", "Rex", "Nyx"],
            "video_urls": ["https://storage.googleapis.com/simula-public/assets/"
                           "creative-proposals/2b2b5247-1d9d-4b88-b8b9-95d21981edf8/"
                           "sponsored-character-1.mp4"],
            "ctas": ["Play Free", "Install Now"],
            "ai_prompts": ["Tease the daily bonus without naming the game."],
            "fallback_copy": ["Something good is waiting."],
        }).json()
        print(f"  3 characters x 1 video x 2 CTAs x 1 prompt = "
              f"{adset['variant_count']} variants")
        for v in adset["variants"][:4]:
            print(f"    {v['variant_id'][:18]}  {v['character_name']:6} | {v['cta']}")
        print(f"  linked to campaign: {adset['campaign_linked']}")

        rule("5. Session semantics (30s inactivity window)")
        s1 = self.session("demo_user", TEST_IPS["US"], IOS)
        s2 = self.session("demo_user", TEST_IPS["US"], IOS)
        print(f"  same ppid twice      -> {s1}\n                          {s2}")
        print(f"  stable: {s1 == s2}")
        s3 = self.session(None, TEST_IPS["SE"], ANDROID)
        print(f"  no ppid, resolved by IP -> {s3}")

        rule("6. Geo x OS targeting matrix")
        print(f"  {'country':8} {'os':8} {'http':6} served")
        print(f"  {'-'*8} {'-'*8} {'-'*6} {'-'*40}")
        for country, ip in TEST_IPS.items():
            for os_name, ua in (("ios", IOS), ("android", ANDROID), ("desktop", DESKTOP)):
                sid = self.session(f"m_{country}_{os_name}", ip, ua)
                r = self.serve(sid, ip, ua)
                if r.status_code == 200:
                    html = r.json()["rendered_html"]
                    served = f"{js_const(html,'CAMPAIGN')} / {js_const(html,'CHAR_NAME')}"
                else:
                    served = "(no eligible ad)"
                print(f"  {country:8} {os_name:8} {r.status_code:<6} {served}")

        rule("7. Serve two contrasting contexts and save the HTML")
        contexts = [
            ("scifi_feed", TEST_IPS["US"], IOS, 3,
             {"searchTerm": "space adventure", "tags": ["sci-fi", "rpg"],
              "category": "roleplay", "title": "Galaxy Companion", "nsfw": False}),
            ("casual_feed", TEST_IPS["US"], ANDROID, 8,
             {"searchTerm": "casual games", "tags": ["casual", "puzzle"],
              "category": "games", "title": "Puzzle Pal", "nsfw": False}),
        ]
        for name, ip, ua, pos, ctx in contexts:
            sid = self.session(f"save_{name}", ip, ua)
            r = self.serve(sid, ip, ua, pos, ctx)
            if r.status_code != 200:
                print(f"  {name}: http={r.status_code} (no ad)")
                continue
            body = r.json()
            out = SAMPLES / f"{name}.html"
            out.write_text(body["rendered_html"])
            print(f"  {name}: {body['impression_id']}")
            for k in ("CAMPAIGN", "CHAR_NAME", "CTA", "CHAR_MESSAGE"):
                print(f"      {k:13} {js_const(body['rendered_html'], k)[:60]}")
            print(f"      saved -> {out.relative_to(ROOT)}")

            click = self.c.post(f"/impressions/{body['impression_id']}/click")
            print(f"      click -> {click.json()}")

        rule("8. Frequency capping (same user, repeated serves)")
        sid = self.session("fatigue_demo", TEST_IPS["US"], IOS)
        for i in range(1, 7):
            r = self.serve(sid, TEST_IPS["US"], IOS, position=i)
            if r.status_code == 200:
                print(f"  serve {i}: {js_const(r.json()['rendered_html'], 'CAMPAIGN')}")
            else:
                print(f"  serve {i}: http={r.status_code} (all campaigns capped)")

        print(f"\nSamples written to {SAMPLES}")
        self.c.close()


if __name__ == "__main__":
    Demo(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").run()
