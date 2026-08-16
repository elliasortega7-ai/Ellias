@echo off
REM ============================================================
REM  Solar Inverter Monitor - one-click launcher (Windows)
REM  Starts the MQTT broker (if installed), the simulator, the
REM  poller/dashboard, and opens the dashboard in your browser.
REM  Close the opened windows (or press a key here) to stop.
REM ============================================================
cd /d "%~dp0"

echo.
echo === Solar Inverter Monitor ===
echo.

REM --- 1. MQTT broker (Mosquitto), if it's installed ---------
set "MOSQ=C:\Program Files\mosquitto\mosquitto.exe"
if exist "%MOSQ%" (
    echo Starting MQTT broker ^(Mosquitto^)...
    start "MQTT Broker" "%MOSQ%" -v
    timeout /t 1 >nul
) else (
    echo Mosquitto not found - skipping MQTT broker.
    echo   ^(Install it, or set mqtt.enabled=false in config.json to hide warnings.^)
)

REM --- 2. The simulated inverter -----------------------------
echo Starting inverter simulator...
start "Inverter Simulator" cmd /k python simulator\inverter_sim.py
timeout /t 2 >nul

REM --- 3. The poller + dashboard -----------------------------
echo Starting poller + dashboard...
start "Poller + Dashboard" cmd /k python poller\poller.py
timeout /t 3 >nul

REM --- 4. Open the dashboard in the browser ------------------
echo Opening dashboard at http://localhost:8080 ...
start "" http://localhost:8080

echo.
echo All started. Two (or three) windows have opened.
echo Close those windows to stop the simulation.
echo.
pause
