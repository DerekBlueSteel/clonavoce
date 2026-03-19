@echo off
cd /d "%~dp0BIN"
if exist "%~dp0.venv_xtts\Scripts\python.exe" (
    "%~dp0.venv_xtts\Scripts\python.exe" clona_voce_gui.py
) else if exist "%~dp0..\.venv\Scripts\python.exe" (
    call "%~dp0..\.venv\Scripts\activate.bat" 2>nul
    "%~dp0..\.venv\Scripts\python.exe" clona_voce_gui.py
) else (
    python clona_voce_gui.py
)

if errorlevel 1 (
    echo.
    echo Errore avvio interfaccia ClonaVoce. Premi un tasto per chiudere.
    pause >nul
)