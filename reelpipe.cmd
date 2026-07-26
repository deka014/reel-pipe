@echo off
REM ReelPipe launcher - runs the CLI from the project venv.
setlocal
set "HERE=%~dp0"
"%HERE%.venv\Scripts\python.exe" -m reelpipe.cli %*
