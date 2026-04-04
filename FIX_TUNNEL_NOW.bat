@echo off
setlocal ENABLEDELAYEDEXPANSION
chcp 65001 >nul 2>&1

set "ROOT_DIR=%~dp0"
set "CONFIG_DIR=%ROOT_DIR%config"
set "ENV_FILE=%CONFIG_DIR%\xtts_local.env"
set "TUNNEL_LOG=%CONFIG_DIR%\cloudflared_tunnel.log"
set "TUNNEL_LOG_BAK=%CONFIG_DIR%\cloudflared_tunnel.log.bak"
set "RENDER_SYNC=%CONFIG_DIR%\render_sync.py"
set "PORT=8010"
set "CLOUDFLARED_CMD=cloudflared"

echo ==========================================
echo  ClonaVoce - FIX TUNNEL (recupero rapido)
echo ==========================================
echo.

rem --- Carica env ---
if exist "%ENV_FILE%" (
    for /F "usebackq tokens=1,* delims==" %%A in ("%ENV_FILE%") do (
        set "%%A=%%B"
    )
)

if not "%CLOUDFLARED_CMD_OVERRIDE%"=="" set "CLOUDFLARED_CMD=%CLOUDFLARED_CMD_OVERRIDE%"
if not "%CLONAVOCE_REMOTE_PORT%"=="" set "PORT=%CLONAVOCE_REMOTE_PORT%"

echo [1/5] Terminazione cloudflared in esecuzione...
taskkill /F /IM cloudflared.exe /T >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] Processo cloudflared terminato.
) else (
    echo [INFO] Nessun processo cloudflared trovato (gia' spento).
)
timeout /t 2 >nul

echo [2/5] Rotazione vecchio log tunnel...
if exist "%TUNNEL_LOG%" (
    move /y "%TUNNEL_LOG%" "%TUNNEL_LOG_BAK%" >nul
    echo [OK] Log precedente spostato in cloudflared_tunnel.log.bak
) else (
    echo [INFO] Nessun log precedente.
)

echo [3/5] Avvio nuovo tunnel cloudflared (porta %PORT%)...
start "Cloudflared XTTS Tunnel" /MIN cmd /c ""%CLOUDFLARED_CMD%" tunnel --url http://127.0.0.1:%PORT% > "%TUNNEL_LOG%" 2>&1"

echo [4/5] Attendo rilevamento URL (max 90 secondi)...
set "TUNNEL_URL="
set "URL_PARSER=%CONFIG_DIR%\extract_tunnel_url.py"
set "URL_TMP=%CONFIG_DIR%\extract_tunnel_url.tmp"

rem Cerca prima extract_tunnel_url.py, altrimenti usa regex powershell
if exist "%URL_PARSER%" (
    rem Trova PYTHON
    set "PY="
    if exist "%ROOT_DIR%.venv311\Scripts\python.exe" (
        set "PY=%ROOT_DIR%.venv311\Scripts\python.exe"
    ) else (
        where python >nul 2>&1 && set "PY=python"
    )
    if not "!PY!"=="" (
        for /L %%I in (1,1,30) do (
            "!PY!" "%URL_PARSER%" "%TUNNEL_LOG%" > "%URL_TMP%" 2>nul
            if exist "%URL_TMP%" (
                set /p TUNNEL_URL=<"%URL_TMP%"
            )
            if not "!TUNNEL_URL!"=="" goto :got_url
            timeout /t 3 >nul
        )
    )
) else (
    for /L %%I in (1,1,30) do (
        if exist "%TUNNEL_LOG%" (
            for /F "delims=" %%L in ('powershell -NoProfile -Command "if(Test-Path '%TUNNEL_LOG%'){(Select-String -Path '%TUNNEL_LOG%' -Pattern 'https://[\w-]+\.trycloudflare\.com').Matches.Value | Select-Object -Last 1}"') do set "TUNNEL_URL=%%L"
        )
        if not "!TUNNEL_URL!"=="" goto :got_url
        timeout /t 3 >nul
    )
)

echo.
echo [ERRORE] URL tunnel non rilevato entro 90 secondi.
echo Controlla che cloudflared funzioni e che il server XTTS locale
echo sia attivo su http://127.0.0.1:%PORT%/health
echo.
echo Premi R per riprovare, qualsiasi altro tasto per uscire.
choice /C RQ /N
if "%errorlevel%"=="1" goto :eof
goto :eof

:got_url
echo [OK] URL tunnel: !TUNNEL_URL!
set "SYNTH_URL=!TUNNEL_URL!/synthesize"

rem Aggiorna xtts_local.env
echo [4b] Aggiornamento xtts_local.env...
powershell -NoProfile -Command ^
  "$f='%ENV_FILE%'; $lines=Get-Content $f; $changed=$false; $out=@(); foreach($l in $lines){ if($l -match '^CLOUDFLARED_PUBLIC_URL='){$out+='CLOUDFLARED_PUBLIC_URL=!TUNNEL_URL!'; $changed=$true}elseif($l -match '^CLONAVOCE_REMOTE_XTTS_URL='){$out+='CLONAVOCE_REMOTE_XTTS_URL=!SYNTH_URL!'; $changed=$true}else{$out+=$l}}; if(-not $changed){$out+='CLOUDFLARED_PUBLIC_URL=!TUNNEL_URL!'; $out+='CLONAVOCE_REMOTE_XTTS_URL=!SYNTH_URL!'}; $out | Set-Content $f"
echo [OK] Config aggiornata.

echo [5/5] Verifica tunnel pubblico...
set "HEALTH_URL=!TUNNEL_URL!/health"
set "TUNNEL_OK="
for /L %%I in (1,1,15) do (
    powershell -NoProfile -Command "try{$r=Invoke-WebRequest -UseBasicParsing '!HEALTH_URL!' -TimeoutSec 6; if($r.StatusCode -eq 200){exit 0}else{exit 1}}catch{exit 1}"
    if not errorlevel 1 (
        set "TUNNEL_OK=1"
        goto :tunnel_health_ok
    )
    timeout /t 3 >nul
)
echo [WARN] Health tunnel non risponde. Aspetta qualche secondo e riprova.
goto :do_render_sync

:tunnel_health_ok
echo [OK] Tunnel risponde.

:do_render_sync
if not exist "%RENDER_SYNC%" (
    echo [WARN] render_sync.py non trovato. Aggiorna manualmente su Render:
    echo   CLONAVOCE_REMOTE_XTTS_URL = !SYNTH_URL!
    goto :done
)

rem Trova Python
set "PY="
if exist "%ROOT_DIR%.venv311\Scripts\python.exe" (
    set "PY=%ROOT_DIR%.venv311\Scripts\python.exe"
) else (
    where python >nul 2>&1 && set "PY=python"
)

if "%PY%"=="" (
    echo [WARN] Python non trovato. Aggiorna manualmente su Render.
    goto :done
)

echo.
echo [RENDER] Avvio render_sync.py per aggiornare CLONAVOCE_REMOTE_XTTS_URL...
echo [RENDER] Questo puo' richiedere fino a 10 minuti (deploy Render)...
echo.
"%PY%" "%RENDER_SYNC%" "!SYNTH_URL!"
if %errorlevel% equ 0 (
    echo [OK] Render aggiornato! L'app ora vedra' il PC come online.
) else (
    echo [WARN] render_sync.py ha segnalato un problema. Verifica i log sopra.
)

:done
echo.
echo ==========================================
echo  Tunnel attivo: !TUNNEL_URL!
echo  Endpoint:      !SYNTH_URL!
echo ==========================================
echo.
pause
