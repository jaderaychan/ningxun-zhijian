@echo off
chcp 936 >nul
title Agnes Harness Launcher

rem This file must keep CRLF line endings: cmd.exe mis-parses a batch file that
rem uses LF only, splitting lines and running fragments as commands.
rem Keep the content ASCII so the console code page cannot garble it.
rem
rem FLOODKIT_ROOT / FLOODKIT_PYTHON are why this launcher exists at all.
rem After installation AGH runs the flood plugin from a snapshot under ~/.agh,
rem so the plugin cannot work out where the project lives and falls back to a
rem bare `python` -- which on this machine has none of the dependencies. The two
rem variables below are the first thing the plugin looks for, so setting them
rem here fixes the tool without touching or reinstalling the plugin itself.

set "ROOT=%~dp0"
set "AGH=%ROOT%_agh_src\agnes-harness-main"
set "ENTRY=%AGH%\packages\cli\dist\local\agnes.mjs"
set "PROFILE=local-dev"

rem %~dp0 ends with a backslash; strip it so paths join cleanly.
set "FLOODKIT_ROOT=%ROOT:~0,-1%"
if exist "%FLOODKIT_ROOT%\.venv\Scripts\python.exe" (
  set "FLOODKIT_PYTHON=%FLOODKIT_ROOT%\.venv\Scripts\python.exe"
) else (
  set "FLOODKIT_PYTHON=python"
)
rem
rem Optional second forecast source. flood_weather already works with no key at
rem all: it asks Open-Meteo for three independent models and reports the spread
rem between them, which is the range the agent quotes. Setting QWeather adds a
rem second opinion from a Chinese provider on top. Both lines are needed, or
rem neither; with them absent the tool simply reports one source.
rem
rem set "QWEATHER_KEY=your-key-here"
rem set "QWEATHER_HOST=abcdefg.re.qweatherapi.com"

echo ============================================================
echo   Agnes Harness (AGH) launcher
echo ============================================================
echo.

if not exist "%ENTRY%" (
  echo [ERROR] AGH entry not found:
  echo         %ENTRY%
  echo.
  echo Run scripts\setup.ps1 first, or check _agh_src\agnes-harness-main.
  echo.
  pause
  exit /b 1
)

where node >nul 2>nul
if errorlevel 1 (
  echo [ERROR] node not found. Install Node.js 24.10 or newer first.
  echo.
  pause
  exit /b 1
)

echo AGH entry        : %ENTRY%
echo AGH home         : %USERPROFILE%\.agh
echo FLOODKIT_ROOT    : %FLOODKIT_ROOT%
echo FLOODKIT_PYTHON  : %FLOODKIT_PYTHON%
if defined QWEATHER_KEY (
  echo QWEATHER_KEY     : set ^(QWeather is the second forecast source^)
) else (
  echo QWEATHER_KEY     : not set ^(forecasts come from Open-Meteo only^)
)
echo.
echo Starting the daemon and the Web workbench...
echo The browser will open http://127.0.0.1:4177/
echo.
echo Closing this window stops AGH.
echo ------------------------------------------------------------
echo.

cd /d "%AGH%"
set "AGNES_PROFILE=%PROFILE%"

rem If the workbench already answers, open the browser instead of starting a
rem second instance on the same port.
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:4177/' -TimeoutSec 3 -UseBasicParsing; if ($r.StatusCode -eq 200) { exit 0 } } catch { }; exit 1" >nul 2>nul
if not errorlevel 1 (
  echo [INFO] AGH workbench is already running. Opening the browser.
  echo.
  echo WARNING: the running instance was NOT started with the FLOODKIT variables,
  echo          so the flood tools may still use the wrong Python. To apply them:
  echo            1. node "%ENTRY%" daemon stop
  echo            2. close this window and start the launcher again
  echo.
  start "" "http://127.0.0.1:4177/"
  pause
  exit /b 0
)

start "" cmd /c "timeout /t 6 /nobreak >nul & start "" http://127.0.0.1:4177/"

node "%ENTRY%" serve

echo.
echo AGH service has exited.
pause