@echo off
setlocal DisableDelayedExpansion
if "%~1"=="" goto usage
if "%~2"=="" goto usage
if not "%~5"=="" goto usage
if not "%~3"=="" goto candidate
python -X utf8 "%~dp0launch_short.py" "%~f1" "%~f2"
exit /b %errorlevel%
:candidate
if "%~4"=="" goto usage
python -X utf8 "%~dp0launch_short.py" "%~f1" "%~f2" "%~3" "%~4"
exit /b %errorlevel%
:usage
echo run.cmd "original ZIP or extracted wfc.cmd" "Codex 0.164.0 native EXE or npm entry"
echo run.cmd "local extracted wfc.cmd" "Codex 0.164.0 native EXE or npm entry" local-build-id expected-pyz-sha256
exit /b 2
