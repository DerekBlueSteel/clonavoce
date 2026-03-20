@echo off
setlocal ENABLEDELAYEDEXPANSION

set "ROOT_DIR=%~dp0"
set "BIN_DIR=%ROOT_DIR%BIN"
set "PORT=8010"
set "SERVER_HEALTH_URL=http://127.0.0.1:%PORT%/health"

echo ==========================================
echo  ClonaVoce - Avvio XTTS Locale + Tunnel
echo ==========================================
echo.

if not exist "%BIN_DIR%\clona_voce_remote_xtts_server.py" (
    echo [ERRORE] File server non trovato: %BIN_DIR%\clona_voce_remote_xtts_server.py
    pause
    exit /b 1
)

if "%CLONAVOCE_REMOTE_XTTS_KEY%"=="" (
    set /p CLONAVOCE_REMOTE_XTTS_KEY=Inserisci CLONAVOCE_REMOTE_XTTS_KEY: 
)

if "%CLONAVOCE_REMOTE_XTTS_KEY%"=="" (
    echo [ERRORE] Chiave remota vuota.
    pause
    exit /b 1
)

set "PYTHON_CMD=python"
if exist "%ROOT_DIR%.venv\Scripts\python.exe" (
    set "PYTHON_CMD=%ROOT_DIR%.venv\Scripts\python.exe"
)

echo [INFO] Python in uso: %PYTHON_CMD%

"%PYTHON_CMD%" --version >nul 2>&1
if errorlevel 1 (
    echo [ERRORE] Python non trovato: %PYTHON_CMD%
    pause
    exit /b 1
)

echo [INFO] Controllo/installo dipendenze Python...
call :ensure_pkg fastapi fastapi
if errorlevel 1 goto :deps_fail
call :ensure_pkg uvicorn uvicorn[standard]
if errorlevel 1 goto :deps_fail
call :ensure_pkg pydantic pydantic
if errorlevel 1 goto :deps_fail
call :ensure_pkg soundfile soundfile
if errorlevel 1 goto :deps_fail
call :ensure_pkg TTS.api TTS
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

where cloudflared >nul 2>&1
if errorlevel 1 (
    echo [ERRORE] cloudflared non trovato nel PATH.
    echo Installa cloudflared oppure aggiungilo al PATH e riprova.
    pause
    exit /b 1
)

echo [OK] Avvio server XTTS locale su porta %PORT%...
start "XTTS Locale Server" cmd /k "cd /d \"%BIN_DIR%\" && set CLONAVOCE_REMOTE_XTTS_KEY=%CLONAVOCE_REMOTE_XTTS_KEY% && \"%PYTHON_CMD%\" clona_voce_remote_xtts_server.py"

echo [INFO] Test salute server locale (%SERVER_HEALTH_URL%)...
set "SERVER_READY="
for /L %%I in (1,1,25) do (
    powershell -NoProfile -Command "try { $r=Invoke-WebRequest -UseBasicParsing '%SERVER_HEALTH_URL%' -TimeoutSec 2; if($r.StatusCode -eq 200){exit 0}else{exit 1} } catch { exit 1 }"
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
start "Cloudflared XTTS Tunnel" cmd /k "cloudflared tunnel --url http://127.0.0.1:%PORT%"

echo.
echo ------------------------------------------
echo Prossimi passi:
echo 1) Nella finestra Cloudflared copia URL https://...trycloudflare.com
echo 2) Su Render imposta:
echo    CLONAVOCE_REMOTE_XTTS_URL=https://...trycloudflare.com/synthesize
echo    CLONAVOCE_REMOTE_XTTS_KEY=%CLONAVOCE_REMOTE_XTTS_KEY%
echo 3) Riavvia/deploy il servizio Render.
echo ------------------------------------------
echo.
pause
exit /b 0

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
