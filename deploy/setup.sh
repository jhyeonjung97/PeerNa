#!/usr/bin/env bash
# One-time setup of PeerNa on the haileylab-1 Lightsail server (Ubuntu 24.04, where
# ScholyMap and Caddy already run). Run as the ubuntu user, after cloning:
#   git clone -b main git@github-peerna:jhyeonjung97/PeerNa.git && bash PeerNa/deploy/setup.sh
# and for the dev server (dev branch, dev.peerna.haileylab.com):
#   git clone -b dev git@github-peerna:jhyeonjung97/PeerNa.git PeerNa-dev && bash PeerNa-dev/deploy/setup.sh dev
set -euo pipefail
WHICH="${1:-main}"
if [ "$WHICH" = "dev" ]; then SVC=peerna-dev; DATA=peerna-dev-data; else SVC=peerna; DATA=peerna-data; fi
cd "$(dirname "$0")/.."

sudo apt-get update -y
sudo apt-get install -y python3-venv libgl1 libglib2.0-0

# 2 GB of swap: the server has 2 GB of memory shared with ScholyMap.
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
mkdir -p /home/ubuntu/$DATA

if [ ! -f /etc/peerna.env ]; then
  sudo cp deploy/peerna.env.example /etc/peerna.env
  sudo chown root:root /etc/peerna.env && sudo chmod 600 /etc/peerna.env
fi

sudo cp deploy/$SVC.service /etc/systemd/system/$SVC.service
sudo systemctl daemon-reload
sudo systemctl enable $SVC

echo
echo "Next: sudo nano /etc/peerna.env  (fill in the keys, if not done), then: sudo systemctl restart $SVC"
