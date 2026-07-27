@echo off
rem stockx-arb 24/7 runner: restarts the scan loop if it ever exits.
rem Installed as scheduled task "stockx-arb-scanner" (runs at logon).
cd /d "%~dp0"
:loop
python -u -m arb scan >> scanner.log 2>> scanner.err.log
echo %date% %time% scanner exited, restarting in 60s >> scanner.err.log
timeout /t 60 /nobreak >nul
goto loop
