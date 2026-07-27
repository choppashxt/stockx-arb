@echo off
rem stockx-arb live dashboard: http://127.0.0.1:8787
rem Restarts itself if it ever exits.
cd /d "%~dp0"
:loop
python -u -m arb dashboard >> dashboard.log 2>&1
echo %date% %time% dashboard exited, restarting in 30s >> dashboard.log
timeout /t 30 /nobreak >nul
goto loop
