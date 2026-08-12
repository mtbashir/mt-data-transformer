@echo off
REM ============================================================
REM  Data Pipeline Agent - continuous watcher. Leave this window
REM  open; it polls the data folder and runs the pipeline as
REM  soon as a new New Data file appears. Closing the window (or
REM  a reboot) stops it - for hands-off operation use Task
REM  Scheduler with "Run Agent (once).bat" instead.
REM ============================================================
cd /d "%~dp0"
echo Data Pipeline Agent watcher running. Leave this window open. Press Ctrl+C to stop.
python watch.py --config "pipeline_config.json"
pause
