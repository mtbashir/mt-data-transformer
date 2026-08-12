@echo off
REM Starts the Data Transformer and opens it in your browser.
REM It picks a free port automatically and opens the right URL itself,
REM so an app left running from earlier cannot block this one.
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo.
  echo   Python was not found on your PATH.
  echo   Install Python, or run this app with the full path, for example:
  echo       D:\Python311\python.exe app.py
  echo.
  pause
  exit /b 1
)

python app.py
echo.
echo   The app has stopped.
pause
