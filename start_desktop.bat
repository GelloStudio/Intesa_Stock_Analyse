@echo off
title Intesa Sanpaolo Swing Dashboard - Desktop
cd /d "%~dp0"
echo ================================================
echo   Intesa Sanpaolo Core + Swing Dashboard
echo ================================================
echo.
python --version >nul 2>&1
if errorlevel 1 (
  echo FEL: Python hittades inte.
  pause
  exit /b 1
)
echo Installerar/kontrollerar paket...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo FEL vid paketinstallation.
  pause
  exit /b 1
)
if not exist intesa.env (
  echo VARNING: intesa.env saknas. Kopiera intesa.env.example till intesa.env och satt losenord.
  echo Dashboarden startar inte utan INTESA_DASHBOARD_PASSWORD.
  pause
  exit /b 1
)
echo Startar dashboard...
start "" "http://127.0.0.1:5000"
python app.py
pause
