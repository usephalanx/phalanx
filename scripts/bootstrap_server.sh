#!/bin/bash
# FORGE — One-time LightSail server bootstrap
#
# Run ONCE after creating the LightSail box:
#   ssh -i ~/work/LightsailDefaultKey-us-west-2.pem ubuntu@44.233.157.41 'bash -s' < scripts/bootstrap_server.sh
#
# Installs: Docker, Docker Compose plugin, creates app directory structure

set -euo pipefail

echo "╔══════════════════════════════════════════════╗"
echo "║  FORGE — Server Bootstrap                    ║"
echo "╚══════════════════════════════════════════════╝"

# ── System update ────────────────────────────────────────────────────────
echo "▶ Updating system packages..."
sudo apt-get update -qq
sudo apt-get upgrade -y -qq

# ── Docker ───────────────────────────────────────────────────────────────
echo "▶ Installing Docker..."
sudo apt-get install -y -qq \
  ca-certificates curl gnupg lsb-release

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update -qq
sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Add ubuntu to docker group so we can run docker without sudo
sudo usermod -aG docker ubuntu

echo "▶ Docker version:"
docker --version
docker compose version

# ── App directory structure ───────────────────────────────────────────────
echo "▶ Creating app directory structure..."
mkdir -p /home/ubuntu/forge/nginx
mkdir -p /home/ubuntu/forge/skill-registry
mkdir -p /home/ubuntu/forge/configs
mkdir -p /home/ubuntu/forge/forge-repos
mkdir -p /home/ubuntu/forge/postgres-data

chown -R ubuntu:ubuntu /home/ubuntu/forge
echo "  ✓ /home/ubuntu/forge/ created"

# ── Swap (needed for 1GB instances) ──────────────────────────────────────
SWAP_EXISTS=$(swapon --show | wc -l)
if [ "$SWAP_EXISTS" -eq "0" ]; then
  echo "▶ Adding 2GB swap..."
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  echo "  ✓ Swap enabled"
else
  echo "▶ Swap already configured — skipping"
fi

# ── UFW firewall ─────────────────────────────────────────────────────────
echo "▶ Configuring firewall..."
sudo ufw allow 22/tcp    # SSH
sudo ufw allow 80/tcp    # HTTP (nginx)
sudo ufw allow 8000/tcp  # forge-api (health checks + direct access)
sudo ufw --force enable
echo "  ✓ Ports 22, 80, 8000 open"

# ── Done ─────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  Bootstrap complete!                         ║"
echo "║                                              ║"
echo "║  Next step: run ./deploy.sh from your Mac   ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "NOTE: Log out and back in for docker group to take effect."
