@echo off
chcp 65001 >nul
title FloodKit plugin sync + preflight
set "ROOT=%~dp0"
set "AGNES_PROFILE=local-dev"

echo.
echo   FloodKit AGH plugin: sync + install preflight
echo   ============================================
echo   This does NOT finish the install. AGH requires the Web workbench for the
echo   confirm/trust/enable step; the script prints exactly what to do next.
echo.

where node >nul 2>nul
if errorlevel 1 (
  echo [ERROR] node not found. Install Node.js 24.10 or newer first.
  echo.
  pause
  exit /b 1
)

node "%ROOT%scripts\install-plugin.mjs"

echo.
pause
