#!/bin/bash
set -euo pipefail

# Deploy to DigitalOcean droplet
# Usage: ./deploy.sh [droplet-ip]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Get IP from terraform output or argument
if [ -n "${1:-}" ]; then
    IP="$1"
else
    IP=$(cd infra && NETRC=/dev/null terraform output -raw app_ip 2>/dev/null) || {
        echo "Usage: ./deploy.sh <droplet-ip>"
        echo "Or run 'terraform apply' in infra/ first"
        exit 1
    }
fi

echo "Deploying to $IP..."

# Sync code to droplet (exclude local dev artifacts)
rsync -avz --delete \
    --exclude '.git' \
    --exclude 'node_modules' \
    --exclude '.next' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude 'infra/.terraform' \
    --exclude 'infra/*.tfstate*' \
    --exclude 'infra/terraform.tfvars' \
    --exclude '.env' \
    ./ "root@${IP}:/opt/donate/app/"

# Build and start on the droplet
ssh "root@${IP}" bash <<'REMOTE'
set -euo pipefail
cd /opt/donate/app

# Copy env from cloud-init provisioned location
cp /opt/donate/.env .env

# Build and start services
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d

echo ""
echo "=== Deploy complete ==="
docker compose -f docker-compose.prod.yml ps
REMOTE

echo ""
echo "App deployed! URL: http://${IP}"
echo "SSH: ssh root@${IP}"
