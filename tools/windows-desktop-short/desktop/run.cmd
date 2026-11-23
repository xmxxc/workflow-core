@echo off
setlocal DisableDelayedExpansion
if "%~1"=="" goto usage
if "%~2"=="" goto usage
if not "%~3"=="" goto usage
python -X utf8 "%~dp0launch_short.py" "%~f1" "%~f2"
exit /b %errorlevel%
:usage
echo run.cmd "candidate ZIP or extracted wfc.cmd" "Codex 0.164.0 native EXE or npm entry"
exit /b 2
