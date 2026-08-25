#!/usr/bin/env bash
# Deploy the API to Google Cloud Run and the Temporal worker to Cloud Run Jobs.
#
# Prerequisites (one-time, needs YOUR credentials -- see deploy/README.md):
#   gcloud auth login && gcloud config set project <PROJECT_ID>
#   A reachable MongoDB   (MongoDB Atlas free tier works)
#   A reachable Redis     (Upstash / Memorystore)
#   A Temporal Cloud namespace + API key (or skip the worker)
#
# Usage:
#   PROJECT_ID=my-proj MONGO_URI=... REDIS_URL=... ./deploy/cloudrun.sh
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-simula-ad-server}"
REPO="${REPO:-simula}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}"
TAG="$(git rev-parse --short HEAD 2>/dev/null || date +%s)"

: "${MONGO_URI:?set MONGO_URI}"
: "${REDIS_URL:?set REDIS_URL}"
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
API_KEY="${API_KEY:-$(openssl rand -hex 16)}"

echo "==> enabling services"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com --project "$PROJECT_ID" --quiet

echo "==> ensuring artifact registry repo"
gcloud artifacts repositories describe "$REPO" --location "$REGION" \
  --project "$PROJECT_ID" >/dev/null 2>&1 || \
gcloud artifacts repositories create "$REPO" --repository-format=docker \
  --location "$REGION" --project "$PROJECT_ID" --quiet

echo "==> building ${IMAGE}:${TAG}"
gcloud builds submit --tag "${IMAGE}:${TAG}" --project "$PROJECT_ID" --quiet

echo "==> deploying service"
# The first deploy cannot know its own URL, so API_URL is patched in a second
# pass below -- the rendered creative embeds it for click tracking.
gcloud run deploy "$SERVICE" \
  --image "${IMAGE}:${TAG}" \
  --region "$REGION" --project "$PROJECT_ID" \
  --platform managed --allow-unauthenticated \
  --port 8080 --cpu 1 --memory 512Mi \
  --min-instances 1 --max-instances 10 \
  --concurrency 80 --timeout 30s \
  --set-env-vars "ENVIRONMENT=production,MONGO_URI=${MONGO_URI},REDIS_URL=${REDIS_URL},API_KEY=${API_KEY},ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY},LOG_LEVEL=INFO" \
  --quiet

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" \
       --project "$PROJECT_ID" --format='value(status.url)')"

echo "==> patching API_URL=${URL} so click tracking points at the real host"
gcloud run services update "$SERVICE" --region "$REGION" --project "$PROJECT_ID" \
  --update-env-vars "API_URL=${URL}" --quiet

echo
echo "deployed: ${URL}"
echo "  docs:   ${URL}/docs"
echo "  health: ${URL}/readyz"
echo "  api key: ${API_KEY}"
