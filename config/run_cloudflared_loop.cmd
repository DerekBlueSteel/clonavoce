@echo off
rem Avvia cloudflared con loop di auto-restart.
rem Riceve %1=cloudflared_cmd %2=porta %3=log_file
set "CF=%~1"
set "PORT=%~2"
set "LOG=%~3"
if "%CF%"==""   set "CF=cloudflared"
if "%PORT%"=="" set "PORT=8010"
if "%LOG%"==""  set "LOG=%~dp0cloudflared_tunnel.log"

:loop
echo [%TIME%] Avvio cloudflared tunnel --url http://127.0.0.1:%PORT% >> "%LOG%" 2>&1
"%CF%" tunnel --url "http://127.0.0.1:%PORT%" >> "%LOG%" 2>&1
echo [%TIME%] cloudflared uscito (errorlevel=%errorlevel%), riavvio tra 8s... >> "%LOG%" 2>&1
timeout /t 8 >nul
goto :loop
