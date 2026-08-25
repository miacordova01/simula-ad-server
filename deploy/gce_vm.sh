#!/usr/bin/env bash
# Deploy the whole stack to a single Google Compute Engine VM using the same
# docker-compose file that runs locally.
#
# Why a VM rather than Cloud Run: this stack has three stateful dependencies
# (Mongo, Redis, Temporal) and a long-running Temporal poller. On Cloud Run all
# four become separate managed services and accounts. On one box they are the
# compose file that already works, which is the right trade for a demo
# deployment -- at the cost of no autoscaling and no managed backups.
#
# Prerequisites (yours, one-time):
#   gcloud auth login
#   gcloud config set project <PROJECT_ID>
#   a billing account attached to the project
#
# Usage:
#   PROJECT_ID=my-project ./deploy/gce_vm.sh
#   PROJECT_ID=my-project ANTHROPIC_API_KEY=sk-ant-... ./deploy/gce_vm.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
[ -n "$PROJECT_ID" ] && [ "$PROJECT_ID" != "(unset)" ] || {
  echo "error: set PROJECT_ID or run 'gcloud config set project <id>'" >&2; exit 1; }

ZONE="${ZONE:-us-central1-a}"
INSTANCE="${INSTANCE:-simula-ad-server}"
MACHINE="${MACHINE:-e2-medium}"
PORT=8080
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
API_KEY="${API_KEY:-$(openssl rand -hex 16)}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

say "project=$PROJECT_ID zone=$ZONE instance=$INSTANCE"

say "enabling compute API (idempotent)"
gcloud services enable compute.googleapis.com --project "$PROJECT_ID" --quiet

say "firewall rule for tcp:$PORT"
gcloud compute firewall-rules describe "allow-simula-$PORT" --project "$PROJECT_ID" >/dev/null 2>&1 || \
gcloud compute firewall-rules create "allow-simula-$PORT" \
  --project "$PROJECT_ID" --allow "tcp:$PORT" \
  --target-tags simula-ads --description "Simula ad server HTTP" --quiet

say "creating VM (skipped if it exists)"
if ! gcloud compute instances describe "$INSTANCE" --zone "$ZONE" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud compute instances create "$INSTANCE" \
    --project "$PROJECT_ID" --zone "$ZONE" --machine-type "$MACHINE" \
    --image-family ubuntu-2204-lts --image-project ubuntu-os-cloud \
    --boot-disk-size 30GB --tags simula-ads \
    --metadata=startup-script='#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker
touch /var/log/simula-bootstrap-done' \
    --quiet
  say "waiting for docker bootstrap (2-3 min on first boot)"
  for i in $(seq 1 60); do
    if gcloud compute ssh "$INSTANCE" --zone "$ZONE" --project "$PROJECT_ID" \
         --command "test -f /var/log/simula-bootstrap-done" --quiet >/dev/null 2>&1; then
      echo "  docker ready"; break
    fi
    printf '.'; sleep 10
  done
else
  echo "  instance already exists, reusing"
fi

EXTERNAL_IP="$(gcloud compute instances describe "$INSTANCE" --zone "$ZONE" \
  --project "$PROJECT_ID" --format='get(networkInterfaces[0].accessConfigs[0].natIP)')"
API_URL="http://${EXTERNAL_IP}:${PORT}"
say "external ip: $EXTERNAL_IP"

say "copying source to the VM"
TARBALL=$(mktemp -t simula).tar.gz
tar --exclude-vcs --exclude='.venv' --exclude='__pycache__' --exclude='samples' \
    --exclude='.pytest_cache' --exclude='.mypy_cache' --exclude='.ruff_cache' \
    -czf "$TARBALL" -C "$REPO_DIR" .
gcloud compute scp "$TARBALL" "${INSTANCE}:~/simula.tar.gz" \
  --zone "$ZONE" --project "$PROJECT_ID" --quiet
rm -f "$TARBALL"

say "starting the stack"
# API_URL must be the public address: it is baked into rendered creatives so
# the click tracker in the template can call back to this host.
gcloud compute ssh "$INSTANCE" --zone "$ZONE" --project "$PROJECT_ID" --quiet --command "
  set -e
  sudo usermod -aG docker \$USER || true
  rm -rf ~/simula && mkdir -p ~/simula
  tar -xzf ~/simula.tar.gz -C ~/simula
  cd ~/simula
  cat > .env <<ENVEOF
API_URL=${API_URL}
API_KEY=${API_KEY}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
ENVEOF
  sudo docker compose down --remove-orphans 2>/dev/null || true
  sudo docker compose up -d --build
"

say "waiting for readiness"
for i in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${API_URL}/readyz" || true)
  if [ "$code" = "200" ]; then echo "  ready"; break; fi
  printf '.'; sleep 5
done

cat <<EOF

===============================================================
  deployed: ${API_URL}
===============================================================
  docs      ${API_URL}/docs
  health    ${API_URL}/readyz
  campaigns ${API_URL}/campaigns
  api key   ${API_KEY}

  demo:     python scripts/demo.py ${API_URL}
  logs:     gcloud compute ssh ${INSTANCE} --zone ${ZONE} --command 'cd ~/simula && sudo docker compose logs -f api'
  teardown: gcloud compute instances delete ${INSTANCE} --zone ${ZONE} --quiet
EOF
