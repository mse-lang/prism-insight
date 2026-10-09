@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
set "PRISM_DESK_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%PRISM_DESK_PY%" (
  "%PRISM_DESK_PY%" -m personal.server %*
) else (
  py -3 -m personal.server %*
)
pause
