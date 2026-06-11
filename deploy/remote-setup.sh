#!/bin/bash
# Runs ON the Oracle server (uploaded + executed by deploy.ps1).
set -e
sudo apt-get update -qq
sudo apt-get install -y -qq ffmpeg python3-venv python3-pip

cd /home/ubuntu/discord-music-bot
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

sudo cp deploy/musicbot.service /etc/systemd/system/musicbot.service
sudo systemctl daemon-reload
sudo systemctl enable musicbot
sudo systemctl restart musicbot
sleep 5
sudo systemctl --no-pager status musicbot
echo "--- last log lines ---"
tail -n 5 bot.log 2>/dev/null || true
