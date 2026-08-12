@echo off
REM ============================================================
REM  Headless daily data pipeline - point Windows Task Scheduler
REM  at THIS file (Action: Start a program -> this .bat).
REM
REM  If Task Scheduler cannot find "python", replace it below with
REM  the full path, e.g. C:\Users\you\AppData\Local\Programs\Python\Python311\python.exe
REM ============================================================
cd /d "%~dp0"
python run_pipeline.py
exit /b %errorlevel%
