# Deploy

Stateless container, everything via env vars. Two options.

## A. Single GCE VM (what we use)

One box running the same compose file as local: api, worker, mongo, redis,
temporal. No extra accounts needed.

```bash
gcloud auth login
gcloud config set project <PROJECT_ID>
PROJECT_ID=<PROJECT_ID> ANTHROPIC_API_KEY=sk-ant-... ./deploy/gce_vm.sh
```

Idempotent - re-run to redeploy onto the same box. Creates the firewall rule and
VM, installs docker, copies source, writes `.env` w/ the VM's IP as `API_URL`,
waits for `/readyz`.

Teardown:

```bash
gcloud compute instances delete simula-ad-server --zone us-central1-a --quiet
```

**Why a VM over Cloud Run:** 3 stateful deps + a long-running Temporal poller.
On Cloud Run each becomes a separate managed service and account (Atlas, Upstash,
Temporal Cloud). On one box it's the compose file that already works. Trade-off
is real though - no autoscaling, no managed backups, single point of failure.
For prod traffic, option B.

## B. Cloud Run + managed services

`./deploy/cloudrun.sh`. Needs Mongo Atlas, Upstash Redis, Temporal Cloud.

```bash
export PROJECT_ID=x MONGO_URI=... REDIS_URL=... ANTHROPIC_API_KEY=...
./deploy/cloudrun.sh
```

`min-instances 1` on purpose - cache + GeoIP warm at startup, don't want a cold
start on a live ad request.

Worker runs as a second service, no ingress:

```bash
gcloud run deploy simula-worker --image "$IMAGE" --region "$REGION" \
  --no-allow-unauthenticated --min-instances 1 --max-instances 1 \
  --cpu-throttling=false --command python --args -m,app.temporal_jobs.worker \
  --set-env-vars "MONGO_URI=...,REDIS_URL=...,TEMPORAL_API_KEY=...,SEED_ON_STARTUP=false"
```

## Env vars

| Var | Default | Note |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | |
| `REDIS_URL` | `redis://localhost:6379/0` | |
| `API_URL` | `http://localhost:8080` | **must be public** - baked into creatives for click tracking |
| `API_KEY` | `dev-key` | sent by the creative's click callback |
| `ANTHROPIC_API_KEY` | unset | without it, copy falls back to `fallback_copy` |
| `LLM_MODEL` | `claude-opus-5` | |
| `LLM_TIMEOUT_S` | `2.5` | hard ceiling on the copy call |
| `SESSION_IDLE_TTL_S` | `30` | the inactivity rule |
| `CAMPAIGN_CACHE_TTL_S` | `10800` | longer than the 1h refresh, on purpose |
| `TEMPORAL_TARGET` | `localhost:7233` | |
| `TEMPORAL_API_KEY` | unset | set for Temporal Cloud (implies TLS) |
| `SEED_ON_STARTUP` | `true` | `false` for the worker |
