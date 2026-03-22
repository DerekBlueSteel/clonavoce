@echo off
REM ClonaVoce Mobile Server Launcher

cd /d "%~dp0BIN"

echo.
echo ====================================
echo ClonaVoce Mobile Server
echo ====================================
echo.

if exist "%~dp0..\.venv_xtts\Scripts\python.exe" (
    "%~dp0..\.venv_xtts\Scripts\python.exe" clona_voce_mobile_server.py
) else if exist "%~dp0..\..\clona_voce\.venv\Scripts\python.exe" (
    "%~dp0..\..\clona_voce\.venv\Scripts\python.exe" clona_voce_mobile_server.py
) else if exist "%~dp0.\..\..\.venv\Scripts\python.exe" (
    "%~dp0.\..\..\.venv\Scripts\python.exe" clona_voce_mobile_server.py
) else (
    python clona_voce_mobile_server.py
)

if errorlevel 1 (
    echo.
    echo Errore avvio server mobile. Verifica Python sia installato.
    echo Premi un tasto per chiudere.
    pause >nul
)
