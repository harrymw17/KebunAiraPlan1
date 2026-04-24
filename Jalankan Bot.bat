@echo off
cd /d "%~dp0"
title KebunAiraBot
pip install -r requirements.txt --quiet
if not exist "config.json" python setup.py
python bot.py
pause
