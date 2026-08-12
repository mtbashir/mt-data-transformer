@echo off
REM ============================================================
REM  Data Pipeline Agent - single check. Point Windows Task
REM  Scheduler at THIS file and run it every few minutes. It
REM  looks for a new/changed New Data file in the data folder
REM  and processes it if found; otherwise it exits immediately.
REM
REM  If Task Scheduler can't find "python", replace it with the
REM  full path to python.exe.
REM ============================================================
cd /d "%~dp0"
python watch.py --once --config "pipeline_config.json"
exit /b %errorlevel%
