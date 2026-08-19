@echo off
REM ============================================================
REM  Data Pipeline Agent - single check. Point Windows Task
REM  Scheduler at THIS file and run it every few minutes. It
REM  looks for a new/changed New Data file in the data folder
REM  and processes it if found; otherwise it exits immediately.
REM
REM  Which config it uses:
REM    1. the path inside agent_config.txt (if that file exists)
REM    2. otherwise pipeline_config.json in this folder
REM  Put your machine's absolute config path in agent_config.txt
REM  - it is git-ignored, so private paths stay off GitHub.
REM
REM  If Task Scheduler can't find "python", replace it with the
REM  full path to python.exe.
REM ============================================================
cd /d "%~dp0"

set "AGENTCFG=pipeline_config.json"
if exist "agent_config.txt" (
  for /f "usebackq delims=" %%C in ("agent_config.txt") do set "AGENTCFG=%%C"
)

python watch.py --once --config "%AGENTCFG%"
exit /b %errorlevel%
