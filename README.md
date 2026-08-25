# Simula Native Ad Server

Serves native sponsored-character ads. Campaign + ad set CRUD, sessions, geo/OS
targeting, bandit ranking, LLM ad copy.

## Try it

Interactive docs (Swagger) are at `/docs` on the running service, or locally:

```bash
docker compose up -d --build      # api + worker + mongo + redis + temporal
open http://localhost:8080/docs
```

`openapi.json` in this repo is the full API surface if you just want to read it.

To exercise the whole thing end to end:

```bash
python scripts/demo.py            # or pass a base URL to hit a deployment
```

### Two things that trip people up

**Send an `X-Forwarded-For` header.** Geo targeting fails closed, so an IP that
doesn't resolve in the bundled MaxMind test database gets a 204, not an ad. Use
one of the test IPs: `214.78.0.1` (US), `2.125.160.217` (GB), `89.160.20.113`
(SE), `175.16.199.1` (CN).

**Get a `session_id` first.** `POST /session/create`, then pass that id to
`POST /load/native`. An unknown session is a 404 by design.

Worth trying: serve with `214.78.0.1` and an iPhone user-agent, then the same IP
with an Android one. You get a different campaign, because `camp_galaxy` is
iOS-only. Switch to `2.125.160.217` and you get a 204, because the only campaign
targeting GB is inactive in the seed data.

---

## Stack

- **FastAPI** - async, typed, free OpenAPI docs
- **Mongo** - brief said "collections", seed data is already docs w/ nested arrays
- **Redis** - cache + sessions + freq caps + CTR counters. Its TTL *is* the 30s session rule
- **Temporal** - hourly cache refresh (required), 6h counter decay
- **Anthropic API** - character copy, behind a timeout w/ fallback

~8ms p50 per serve, warm.

---

## Flow

```
POST /load/native
  -> session (Redis)          404 if unknown
  -> active campaigns         Redis snapshot, Mongo on miss
  -> geo + OS filters         204 if nothing eligible
  -> features (1 pipelined read)
  -> rank: pCTR_ucb x bid x pacing x fatigue
  -> random ad set -> random variant
  -> copy (LLM, cached, 2.5s timeout -> fallback)
  -> render
  -> respond, then write Serve + counters in background
```

---

## Endpoints

| | |
|---|---|
| `POST /campaigns` | id/timestamps/active are API-owned, new ones start inactive |
| `GET /campaigns` | filters: `ids`, `surface`, `publisher_id`, `active` |
| `GET/PATCH/DELETE /campaigns/{id}` | |
| `POST /adsets` | expands cartesian product -> variants, links campaign |
| `GET /adsets`, `/adsets/{id}/variants` | |
| `POST /session/create` | resolve-or-create from `ppid`, else IP |
| `POST /load/native` | serve an ad. **204** = no eligible ad |
| `POST /impressions/{id}/click` | click callback the creative fires |
| `GET /healthz`, `/readyz` | |

---

## Calls I made where the brief didn't say

**204 not 404 for no ad.** No ad for a slot is normal, not an error. 404 reads
like a bug and invites retries.

**Geo/OS fail closed.** Empty targets = no restriction. But an *unresolvable*
country doesn't match a targeted campaign. Serving a US-only campaign to an
unknown IP is a billing problem; dropping it is a missed impression.

**Unknown `session_id` -> 404.** Silently minting one would detach freq capping
and attribution from the user the caller thinks they have.

**Session keys are namespaced.** `ppid:user_42` vs `ip:1.2.3.4`, so you can't
pass `ppid="1.2.3.4"` and hijack that IP's session.

**Ad set writes are ordered + idempotent, not transactional.** Variants, then ad
set, then campaign link. Partial failure = an unlinked (unservable) ad set a
retry fixes. Other order could expose a campaign pointing at an empty ad set.

**`daily_budget` isn't the bid.** Budget is a ceiling, not a price. Ranking on it
puts the biggest wallet on top regardless of performance. Mild log scaling only.

**Variant pick is two-stage random** (ad set, then variant). Also means a
40-variant ad set doesn't drown a 2-variant one.

**Copy is cached per variant.** Only depends on (character, prompt), so the first
serve pays LLM latency and the rest are a Redis read.

**Click endpoint exists** even though the brief doesn't list it - the template
calls it. 202 on unknown/dupe, since browsers retry click pixels.

**Cache is write-through; Temporal is the repair job.** Brief asked for an hourly
refresh, but if that were the only invalidation an operator toggling a campaign
would wait an hour. So writes refresh immediately and the schedule heals drift
(Redis restart, missed invalidation, direct DB edit). TTL is 3h > refresh
interval on purpose, so a failed refresh goes stale instead of empty.

---

## The escaping thing

Most likely thing to break in prod, so it got the most care.

The template puts placeholders in 3 different contexts, and the *same* one shows
up in more than one:

```
line   2  <html data-theme="{{ THEME }}">                HTML attr
line   6  <title>{{ CHAR_NAME }} \u00b7 Sponsored</title>  HTML text
line  10  const CHAR_NAME = "{{ CHAR_NAME }}";           JS string
line  19  const THEME     = "{{ THEME }}";               JS string
line 342  <video src="{{ MEDIA_URL }}">                  HTML attr (URL)
```

`CHAR_MESSAGE` is LLM copy, which is full of apostrophes. Unescaped in a JS
literal = syntax error = blank ad. A `</script>` in there = stored XSS on the
publisher's page.

So escaping is picked **per occurrence**, by finding where each placeholder sits.
JS string -> JSON escaping (+ `<`, `>`, `&`, `/` so nothing can close the tag).
Inside a tag -> attr escaping. Else HTML text. URLs get scheme-checked so a
`javascript:` tracking URL can't fire on click. Mustache sections resolve first,
so the dead `<img>`/`<video>` branch never gets filled.

Proof:

```bash
make escaping
```

10 hostile payloads (`</script><script>`, backslashes, control chars, U+2028)
rendered, then parsed w/ V8. All valid, payload comes back as an inert string.

---

## Ranking + the CTR model

The part-1 LightGBM model **can't be used here**. It was trained on `site_id`,
`device_ip`, `C14`-`C21`, and a `character_id` from a 5k catalogue. None of that
exists in this system. Loading it and passing zeros = confident nonsense.

What transfers is the architecture:

```
score = pCTR_ucb x bid x pacing x fatigue
```

Hard filters before scoring, uncertainty bonus so cold campaigns get explored,
multiplicative modifiers instead of cliffs. `CTRScorer` is a Protocol:

- `BetaBanditScorer` (default) - Beta posterior on the campaign's clicks/imps in
  the context bucket, shrunk toward its global rate then the prior. Right call
  when you start w/ zero history.
- `ModelScorer` - seam for a model trained on *this* system's features. Falls
  back to the bandit on any error.

`Serve.to_feature_row()` already emits that training set, and every serve logs
the **losing candidates + their scores**. That's what makes off-policy eval
possible later - log only the winner and you can only learn from what you already
picked.

### Features at serve time

Serve path can't afford a Mongo aggregation, so features are pre-aggregated Redis
counters, written on serve/click, read in one pipelined round trip:

```
ctr:{campaign}:{bucket}        imps + clicks in a context bucket
ctr:{campaign}:__all__         global rate (denser prior)
user:{user_key}                rolling profile
fatigue:{user_key}:{campaign}  exposure count, TTL'd
```

Buckets are `(country, os, category, position band, nsfw)` - coarse on purpose so
cells stay dense. Counters halve every 6h via Temporal instead of storing events:
keys stay O(1), stale perf fades, quiet campaigns regain uncertainty and get
explored again.

---

## Tests

```bash
make check     # ruff + mypy + 160 tests
```

Mongo/Redis are faked (mongomock + fakeredis), so it runs in ~2s w/ nothing up.

Covers cartesian expansion + variant cap, per-context escaping, all 6 test IPs,
XFF hop-skipping, UA classification, bandit shrinkage + exploration, pacing,
fatigue, session TTL + namespacing, every LLM failure path, full CRUD, the
targeting matrix, 204/404 paths, serve persistence, click dedupe.

---

## Known limits

1. **XFF is trusted** - left-most public hop. Spoofable, so geo targeting is
   bypassable. Real fix needs a trusted-proxy count.
2. **No auth on management endpoints.** Anyone who can reach it can delete
   campaigns.
3. **`daily_budget` proxies a bid.** No bid or spend data exists, so pacing
   tracks a counter nothing increments.
4. **Freq caps are Redis-only.** A flush resets them. Fine for fatigue, not for a
   contractual cap.
5. **First serve of a variant pays LLM latency** (cached after). Pre-generating
   copy at ad-set creation via a Temporal workflow kills this - obvious next step.
6. Single region, single Redis.
