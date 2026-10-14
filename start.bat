@echo off
title GreenFile - Code Green for Your Storage
echo Starting GreenFile local engine...
python server.py
if errorlevel 1 (
    echo Python not found in PATH or failed to start.
    echo Trying py launcher...
    py server.py
)
pause
