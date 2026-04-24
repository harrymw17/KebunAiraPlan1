#!/bin/bash
cd "$(dirname "$0")"
echo "🌿 KebunAiraBot — Starting..."
pip3 install -r requirements.txt --quiet --break-system-packages 2>/dev/null || pip3 install -r requirements.txt --quiet
[ ! -f "config.json" ] && python3 setup.py
python3 bot.py
read -p "Bot berhenti. Tekan Enter untuk keluar..."
