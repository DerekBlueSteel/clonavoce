@echo off
setlocal ENABLEDELAYEDEXPANSION

set "ROOT_DIR=%~dp0"
set "BIN_DIR=%ROOT_DIR%BIN"
set "TOOLS_DIR=%ROOT_DIR%tools"
set "CONFIG_DIR=%ROOT_DIR%config"
set "CONFIG_FILE=%CONFIG_DIR%\xtts_local.env"
set "VENV_DIR=%ROOT_DIR%.venv311"
set "PORT=8010"
set "SERVER_HEALTH_URL=http://127.0.0.1:%PORT%/health"
set "TUNNEL_LOG=%CONFIG_DIR%\cloudflared_tunnel.log"
set "SERVER_LAUNCHER=%CONFIG_DIR%\run_xtts_local.cmd"
set "TUNNEL_LAUNCHER=%CONFIG_DIR%\run_cloudflared_tunnel.cmd"
set "URL_PARSER_SCRIPT=%CONFIG_DIR%\extract_tunnel_url.py"
set "URL_TMP_FILE=%CONFIG_DIR%\extract_tunnel_url.tmp"
set "TUNNEL_URL="
set "CLOUDFLARED_CMD=cloudflared"
set "CLOUDFLARED_FIXED_PUBLIC_URL="
set "CLOUDFLARED_TUNNEL_TOKEN="
set "RENDER_API_KEY="
set "RENDER_SERVICE_ID="
set "RENDER_AUTO_SYNC=1"
set "RENDER_REMOTE_TIMEOUT_SECONDS=180"

echo ==========================================
echo  ClonaVoce - Avvio XTTS Locale + Tunnel
echo ==========================================
echo.

if not exist "%BIN_DIR%\clona_voce_remote_xtts_server.py" (
    echo [ERRORE] File server non trovato: %BIN_DIR%\clona_voce_remote_xtts_server.py
    pause
    exit /b 1
)

if not exist "%CONFIG_DIR%" (
    mkdir "%CONFIG_DIR%" >nul 2>&1
)

if exist "%CONFIG_FILE%" (
    call :load_env_file "%CONFIG_FILE%"
) else (
    if exist "%CONFIG_DIR%\xtts_local.env.example" (
        copy /y "%CONFIG_DIR%\xtts_local.env.example" "%CONFIG_FILE%" >nul
        call :load_env_file "%CONFIG_FILE%"
    )
)

if "%CLONAVOCE_REMOTE_XTTS_KEY%"=="" (
    set /p CLONAVOCE_REMOTE_XTTS_KEY=Inserisci CLONAVOCE_REMOTE_XTTS_KEY: 
)

if "%CLONAVOCE_REMOTE_XTTS_KEY%"=="" (
    echo [ERRORE] Chiave remota vuota.
    pause
    exit /b 1
)

call :persist_key "%CONFIG_FILE%" "%CLONAVOCE_REMOTE_XTTS_KEY%"
echo [OK] Config salvata in: %CONFIG_FILE%

call :ensure_py311_venv
if errorlevel 1 (
    pause
    exit /b 1
)

echo [INFO] Python in uso: %PYTHON_CMD%

echo [INFO] Controllo/installo dipendenze Python (pin versioni sicure)...

call :ensure_torch_pin
if errorlevel 1 goto :deps_fail
call :ensure_transformers_pin
if errorlevel 1 goto :deps_fail

rem --- resto dipendenze da requirements-xtts-local.txt ---
call :ensure_pkg fastapi fastapi
if errorlevel 1 goto :deps_fail
call :ensure_pkg uvicorn uvicorn[standard]
if errorlevel 1 goto :deps_fail
call :ensure_pkg pydantic pydantic
if errorlevel 1 goto :deps_fail
call :ensure_pkg soundfile soundfile
if errorlevel 1 goto :deps_fail
call :ensure_pkg TTS TTS
if errorlevel 1 goto :deps_fail

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo [WARN] ffmpeg non trovato nel PATH. La conversione MP3 potrebbe non funzionare.
)

goto :deps_ok

:deps_fail
echo [ERRORE] Preflight dipendenze fallito.
pause
exit /b 1

:deps_ok
echo [OK] Dipendenze verificate.

call :ensure_cloudflared
if errorlevel 1 (
    pause
    exit /b 1
)

"%CLOUDFLARED_CMD%" --version >nul 2>&1
if errorlevel 1 (
    echo [ERRORE] cloudflared trovato ma non eseguibile: %CLOUDFLARED_CMD%
    pause
    exit /b 1
)
echo [OK] Cloudflared in uso: %CLOUDFLARED_CMD%

call :is_server_up
if not errorlevel 1 (
    echo [OK] Server XTTS locale gia attivo su porta %PORT%.
) else (
    echo [OK] Avvio server XTTS locale su porta %PORT%...
    start "XTTS Locale Server" /D "%BIN_DIR%" cmd /k "call "%PYTHON_CMD%" clona_voce_remote_xtts_server.py"
)

echo [INFO] Test salute server locale (%SERVER_HEALTH_URL%)...
set "SERVER_READY="
for /L %%I in (1,1,25) do (
    call :is_server_up
    if not errorlevel 1 (
        set "SERVER_READY=1"
        goto :server_ready
    )
    timeout /t 1 >nul
)

if not defined SERVER_READY (
    echo [ERRORE] Server XTTS locale non raggiungibile su %SERVER_HEALTH_URL%.
    echo Controlla la finestra "XTTS Locale Server" per eventuali errori.
    pause
    exit /b 1
)

:server_ready
echo [OK] Server locale risponde correttamente.

echo [OK] Avvio tunnel cloudflared...
del /f /q "%TUNNEL_LOG%" >nul 2>&1
if not "%CLOUDFLARED_TUNNEL_TOKEN%"=="" (
    start "Cloudflared XTTS Tunnel" /MIN cmd /c "call "%CLOUDFLARED_CMD%" tunnel run --token "%CLOUDFLARED_TUNNEL_TOKEN%" > "%TUNNEL_LOG%" 2>&1"
    if "%CLOUDFLARED_FIXED_PUBLIC_URL%"=="" (
        echo [ERRORE] CLOUDFLARED_TUNNEL_TOKEN impostato ma CLOUDFLARED_FIXED_PUBLIC_URL e vuoto.
        echo [INFO] Con tunnel nominato serve anche l'URL pubblico stabile configurato.
        pause
        exit /b 1
    )
    set "TUNNEL_URL=%CLOUDFLARED_FIXED_PUBLIC_URL%"
    echo [OK] Uso tunnel nominato con URL fisso: !TUNNEL_URL!
    goto :tunnel_ready
)

start "Cloudflared XTTS Tunnel" /MIN cmd /c "call "%CLOUDFLARED_CMD%" tunnel --url http://127.0.0.1:%PORT% > "%TUNNEL_LOG%" 2>&1"

if not "%CLOUDFLARED_FIXED_PUBLIC_URL%"=="" (
    set "TUNNEL_URL=%CLOUDFLARED_FIXED_PUBLIC_URL%"
    echo [OK] Uso URL pubblico fisso da config: !TUNNEL_URL!
    goto :tunnel_ready
)

if not exist "%URL_PARSER_SCRIPT%" (
    echo [ERRORE] Parser URL non trovato: %URL_PARSER_SCRIPT%
    pause
    exit /b 1
)

echo [INFO] Attendo URL pubblico trycloudflare...
for /L %%I in (1,1,60) do (
    set "TUNNEL_URL="
    "%PYTHON_CMD%" "%URL_PARSER_SCRIPT%" "%TUNNEL_LOG%" > "%URL_TMP_FILE%" 2>nul
    if exist "%URL_TMP_FILE%" (
        set /p TUNNEL_URL=<"%URL_TMP_FILE%"
    )
    if not "!TUNNEL_URL!"=="" goto :tunnel_ready
    timeout /t 1 >nul
)

echo [ERRORE] URL tunnel non rilevato entro 60 secondi.
echo [INFO] Controlla log: %TUNNEL_LOG%
pause
exit /b 1

:tunnel_ready
call :persist_public_url "%CONFIG_FILE%" "%TUNNEL_URL%"
call :persist_url "%CONFIG_FILE%" "%TUNNEL_URL%/synthesize"
echo [OK] URL tunnel rilevato: %TUNNEL_URL%
echo [OK] Config aggiornata: CLONAVOCE_REMOTE_XTTS_URL=%TUNNEL_URL%/synthesize
if /I "%RENDER_AUTO_SYNC%"=="1" (
    call :sync_render_env "%TUNNEL_URL%/synthesize" "%CLONAVOCE_REMOTE_XTTS_KEY%" "%RENDER_REMOTE_TIMEOUT_SECONDS%"
) else (
    echo [INFO] Sync automatico Render disabilitato: RENDER_AUTO_SYNC=%RENDER_AUTO_SYNC%.
)

echo.
echo ==========================================
echo  Stato finale
echo ==========================================
echo  Tunnel locale  : %TUNNEL_URL%
echo  Endpoint synth : %TUNNEL_URL%/synthesize
echo  Render URL     : https://clonavoce.onrender.com
echo.
if /I "%RENDER_AUTO_SYNC%"=="1" (
    if not "%RENDER_API_KEY%"=="" (
        echo  Render aggiornato automaticamente.
        echo  - Variabili env aggiornate: OK
        echo  - Deploy avviato alle: %TIME%
    ) else (
        echo  [WARN] Render NON aggiornato: RENDER_API_KEY mancante.
        echo  Aggiorna manualmente CLONAVOCE_REMOTE_XTTS_URL su Render:
        echo    %TUNNEL_URL%/synthesize
    )
) else (
    echo  Sync automatico disabilitato.
    echo  Aggiorna manualmente su Render:
    echo    CLONAVOCE_REMOTE_XTTS_URL = %TUNNEL_URL%/synthesize
)
echo ==========================================
echo.
echo (Finestra aperta - il tunnel e il server XTTS sono attivi)
pause
exit /b 0

:sync_render_env
set "_SYNC_URL=%~1"
set "_SYNC_KEY=%~2"
set "_SYNC_TIMEOUT=%~3"
if "%RENDER_API_KEY%"=="" (
    echo [WARN] Render sync saltato: RENDER_API_KEY non impostata in %CONFIG_FILE%.
    exit /b 0
)
if "%RENDER_SERVICE_ID%"=="" (
    echo [WARN] Render sync saltato: RENDER_SERVICE_ID non impostato in %CONFIG_FILE%.
    exit /b 0
)
if "%_SYNC_TIMEOUT%"=="" set "_SYNC_TIMEOUT=180"

set "SYNC_RENDER_API_KEY=%RENDER_API_KEY%"
set "SYNC_RENDER_SERVICE_ID=%RENDER_SERVICE_ID%"
set "SYNC_REMOTE_XTTS_URL=%_SYNC_URL%"
set "SYNC_REMOTE_XTTS_KEY=%_SYNC_KEY%"
set "SYNC_REMOTE_XTTS_TIMEOUT=%_SYNC_TIMEOUT%"

echo [INFO] Sync automatico Render in corso...
if not exist "%CONFIG_DIR%\render_sync.py" (
    echo [WARN] render_sync.py non trovato in %CONFIG_DIR%. Sync saltato.
    exit /b 0
)
"%PYTHON_CMD%" "%CONFIG_DIR%\render_sync.py" "%_SYNC_URL%"
if errorlevel 1 (
    echo [WARN] Sync Render fallito. Mantengo comunque il tunnel locale attivo.
    echo [INFO] Verifica RENDER_API_KEY e RENDER_SERVICE_ID in %CONFIG_FILE%.
    exit /b 0
)
echo [OK] Render aggiornato e deploy avviato.
exit /b 0

:ensure_py311_venv
set "PYTHON_CMD="

if exist "%VENV_DIR%\Scripts\python.exe" (
    "%VENV_DIR%\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info < (3,12) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_CMD=%VENV_DIR%\Scripts\python.exe"
        exit /b 0
    )
)

echo [INFO] Cerco Python 3.11...
py -3.11 --version >nul 2>&1
if errorlevel 1 (
    echo [INFO] Python 3.11 non trovato. Provo installazione automatica con winget...
    where winget >nul 2>&1
    if errorlevel 1 (
        echo [ERRORE] winget non disponibile e Python 3.11 non trovato.
        echo [INFO] Installa Python 3.11 e rilancia lo script.
        exit /b 1
    )
    winget install --id Python.Python.3.11 -e --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo [ERRORE] Installazione Python 3.11 fallita.
        exit /b 1
    )
)

py -3.11 --version >nul 2>&1
if errorlevel 1 (
    echo [ERRORE] Python 3.11 non disponibile anche dopo installazione.
    exit /b 1
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [INFO] Creo virtualenv dedicata: %VENV_DIR%
    py -3.11 -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERRORE] Creazione virtualenv fallita.
        exit /b 1
    )
)

set "PYTHON_CMD=%VENV_DIR%\Scripts\python.exe"
"%PYTHON_CMD%" -c "import sys; raise SystemExit(0 if sys.version_info < (3,12) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERRORE] Virtualenv non compatibile con XTTS. Rimuovo e ricreo.
    rmdir /s /q "%VENV_DIR%" >nul 2>&1
    py -3.11 -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERRORE] Ricreazione virtualenv fallita.
        exit /b 1
    )
    set "PYTHON_CMD=%VENV_DIR%\Scripts\python.exe"
)

"%PYTHON_CMD%" --version >nul 2>&1
if errorlevel 1 (
    echo [ERRORE] Python non eseguibile nella virtualenv: %PYTHON_CMD%
    exit /b 1
)
exit /b 0

:ensure_cloudflared
if exist "%TOOLS_DIR%\cloudflared-windows-amd64.exe" (
    set "CLOUDFLARED_CMD=%TOOLS_DIR%\cloudflared-windows-amd64.exe"
    exit /b 0
)
if exist "%TOOLS_DIR%\cloudflared.exe" (
    set "CLOUDFLARED_CMD=%TOOLS_DIR%\cloudflared.exe"
    exit /b 0
)
if exist "%TOOLS_DIR%\cloudflared-windows-386.exe" (
    set "CLOUDFLARED_CMD=%TOOLS_DIR%\cloudflared-windows-386.exe"
    exit /b 0
)

where cloudflared >nul 2>&1
if not errorlevel 1 (
    set "CLOUDFLARED_CMD=cloudflared"
    exit /b 0
)

echo [INFO] cloudflared non trovato. Provo installazione automatica con winget...
where winget >nul 2>&1
if not errorlevel 1 (
    winget install --id Cloudflare.cloudflared -e --accept-package-agreements --accept-source-agreements
    if not errorlevel 1 (
        where cloudflared >nul 2>&1
        if not errorlevel 1 (
            set "CLOUDFLARED_CMD=cloudflared"
            exit /b 0
        )
    )
)

if not exist "%TOOLS_DIR%" mkdir "%TOOLS_DIR%" >nul 2>&1
echo [INFO] Provo download diretto cloudflared amd64...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -UseBasicParsing 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile '%TOOLS_DIR%\cloudflared-windows-amd64.exe'"
if errorlevel 1 (
    echo [ERRORE] Download cloudflared fallito.
    exit /b 1
)

set "CLOUDFLARED_CMD=%TOOLS_DIR%\cloudflared-windows-amd64.exe"
exit /b 0

:ensure_torch_pin
"%PYTHON_CMD%" -m pip show torch 2>nul | findstr /I "2.5.1" >nul 2>&1
if not errorlevel 1 (
    echo [OK] torch 2.5.1 gia presente.
    exit /b 0
)
echo [INFO] Installo torch 2.5.1+cpu e torchaudio 2.5.1+cpu...
"%PYTHON_CMD%" -m pip install --index-url https://download.pytorch.org/whl/cpu "torch==2.5.1" "torchaudio==2.5.1" --quiet
exit /b %errorlevel%

:ensure_transformers_pin
"%PYTHON_CMD%" -m pip show transformers 2>nul | findstr /I "4.40.2" >nul 2>&1
if not errorlevel 1 (
    echo [OK] transformers 4.40.2 gia presente.
    exit /b 0
)
echo [INFO] Installo transformers==4.40.2...
"%PYTHON_CMD%" -m pip install "transformers==4.40.2" --quiet
exit /b %errorlevel%

:ensure_pkg
set "_MODULE=%~1"
set "_PKG=%~2"

"%PYTHON_CMD%" -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec(r'%_MODULE%') else 1)" >nul 2>&1
if not errorlevel 1 (
    echo [OK] Python module presente: %_MODULE%
    exit /b 0
)

echo [INFO] Modulo mancante: %_MODULE% - installo package: %_PKG%
"%PYTHON_CMD%" -m pip install "%_PKG%"
if errorlevel 1 (
    echo [ERRORE] Installazione fallita per package: %_PKG%
    exit /b 1
)

"%PYTHON_CMD%" -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec(r'%_MODULE%') else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERRORE] Modulo ancora non disponibile dopo install: %_MODULE%
    exit /b 1
)

echo [OK] Modulo installato: %_MODULE%
exit /b 0

:is_server_up
powershell -NoProfile -Command "try { $r=Invoke-WebRequest -UseBasicParsing '%SERVER_HEALTH_URL%' -TimeoutSec 2; if($r.StatusCode -eq 200){exit 0}else{exit 1} } catch { exit 1 }"
exit /b %errorlevel%

:write_server_launcher
>"%SERVER_LAUNCHER%" (
    echo @echo off
    echo setlocal
    echo cd /d "%BIN_DIR%"
    echo call "%PYTHON_CMD%" clona_voce_remote_xtts_server.py
)
if errorlevel 1 exit /b 1
exit /b 0

:write_tunnel_launcher
>"%TUNNEL_LAUNCHER%" (
    echo @echo off
    echo call "%CLOUDFLARED_CMD%" tunnel --url http://127.0.0.1:%PORT% ^> "%TUNNEL_LOG%" 2^>^&1
)
if errorlevel 1 exit /b 1
exit /b 0

:write_url_parser
>"%URL_PARSER_SCRIPT%" echo import pathlib, re, sys
>>"%URL_PARSER_SCRIPT%" echo p = pathlib.Path(sys.argv[1]) if len(sys.argv) ^> 1 else None
>>"%URL_PARSER_SCRIPT%" echo if not p or not p.exists():
>>"%URL_PARSER_SCRIPT%" echo ^    print("")
>>"%URL_PARSER_SCRIPT%" echo ^    raise SystemExit(0)
>>"%URL_PARSER_SCRIPT%" echo txt = p.read_text(encoding="utf-8", errors="ignore").replace("\r", "").replace("\n", "")
>>"%URL_PARSER_SCRIPT%" echo m = re.findall(r"https://[-a-z0-9.]+trycloudflare\.com", txt, flags=re.IGNORECASE)
>>"%URL_PARSER_SCRIPT%" echo print(m[-1] if m else "")
if errorlevel 1 exit /b 1
exit /b 0

:persist_url
set "_CFG=%~1"
set "_NEW_URL=%~2"
set "_TMP=%_CFG%.tmp"
set "_FOUND="
if exist "%_CFG%" (
    >"%_TMP%" (
        for /f "usebackq delims=" %%L in ("%_CFG%") do (
            set "_LINE=%%L"
            for /f "tokens=1,* delims==" %%A in ("!_LINE!") do (
                set "_KEY=%%~A"
                set "_VAL=%%~B"
            )
            if /i "!_KEY!"=="CLONAVOCE_REMOTE_XTTS_URL" (
                if not defined _FOUND echo CLONAVOCE_REMOTE_XTTS_URL=%_NEW_URL%
                set "_FOUND=1"
            ) else (
                echo %%L
            )
        )
    )
    if not defined _FOUND echo CLONAVOCE_REMOTE_XTTS_URL=%_NEW_URL%>>"%_TMP%"
    move /y "%_TMP%" "%_CFG%" >nul
) else (
    >"%_CFG%" echo CLONAVOCE_REMOTE_XTTS_URL=%_NEW_URL%
)
exit /b 0

:persist_public_url
set "_CFG=%~1"
set "_NEW_URL=%~2"
set "_TMP=%_CFG%.tmp"
set "_FOUND="
if exist "%_CFG%" (
    >"%_TMP%" (
        for /f "usebackq delims=" %%L in ("%_CFG%") do (
            set "_LINE=%%L"
            for /f "tokens=1,* delims==" %%A in ("!_LINE!") do (
                set "_KEY=%%~A"
                set "_VAL=%%~B"
            )
            if /i "!_KEY!"=="CLOUDFLARED_PUBLIC_URL" (
                if not defined _FOUND echo CLOUDFLARED_PUBLIC_URL=%_NEW_URL%
                set "_FOUND=1"
            ) else (
                echo %%L
            )
        )
    )
    if not defined _FOUND echo CLOUDFLARED_PUBLIC_URL=%_NEW_URL%>>"%_TMP%"
    move /y "%_TMP%" "%_CFG%" >nul
) else (
    >"%_CFG%" echo CLOUDFLARED_PUBLIC_URL=%_NEW_URL%
)
exit /b 0

:load_env_file
set "_ENV_FILE=%~1"
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%_ENV_FILE%") do (
    set "_K=%%~A"
    set "_V=%%~B"
    if not "!_K!"=="" set "!_K!=!_V!"
)
exit /b 0

:persist_key
set "_CFG=%~1"
set "_NEW_KEY=%~2"
set "_TMP=%_CFG%.tmp"
set "_FOUND="
if exist "%_CFG%" (
    >"%_TMP%" (
        for /f "usebackq delims=" %%L in ("%_CFG%") do (
            set "_LINE=%%L"
            for /f "tokens=1,* delims==" %%A in ("!_LINE!") do (
                set "_KEY=%%~A"
                set "_VAL=%%~B"
            )
            if /i "!_KEY!"=="CLONAVOCE_REMOTE_XTTS_KEY" (
                if not defined _FOUND echo CLONAVOCE_REMOTE_XTTS_KEY=%_NEW_KEY%
                set "_FOUND=1"
            ) else (
                echo %%L
            )
        )
    )
    if not defined _FOUND echo CLONAVOCE_REMOTE_XTTS_KEY=%_NEW_KEY%>>"%_TMP%"
    move /y "%_TMP%" "%_CFG%" >nul
) else (
    >"%_CFG%" echo CLONAVOCE_REMOTE_XTTS_KEY=%_NEW_KEY%
)
exit /b 0
