@echo off
REM ============================================================
REM  Data Pipeline Agent - continuous watcher. Leave this window
REM  open; it polls the data folder and runs the pipeline as
REM  soon as a new New Data file appears. Closing the window (or
REM  a reboot) stops it - for hands-off operation use Task
REM  Scheduler with "Run Agent (once).bat" instead.
REM
REM  Which config it uses:
REM    1. the path inside agent_config.txt (if that file exists)
REM    2. otherwise pipeline_config.json in this folder
REM ============================================================
cd /d "%~dp0"

set "AGENTCFG=pipeline_config.json"
if exist "agent_config.txt" (
  for /f "usebackq delims=" %%C in ("agent_config.txt") do set "AGENTCFG=%%C"
)

echo Data Pipeline Agent watcher running. Config: %AGENTCFG%
echo Leave this window open. Press Ctrl+C to stop.
python watch.py --config "%AGENTCFG%"
pause
