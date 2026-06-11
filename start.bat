@echo off
title Nuked Music Bot
cd /d "%~dp0"
echo Starting music bot... (close this window to stop the bot)
".venv\Scripts\python.exe" bot.py
pause
