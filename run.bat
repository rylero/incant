@echo off
REM Launch the speech-to-text tool. Double-click or run from a terminal.
cd /d "%~dp0"
uv run stt
pause
