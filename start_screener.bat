@echo off
setlocal enabledelayedexpansion
title Smart Screener — Control Panel
cd /d c:\Users\91971\Downloads\smart-screener-deploy
color 0A

:MENU
cls
echo.
echo  ============================================================
echo    SMART SCREENER v5  ^|  Control Panel
echo  ============================================================
echo.
echo   [1]  Start Server                (normal launch)
echo   [2]  Clear Cache + Start Server  (fresh boot)
echo   [3]  Kill Server                 (stop running instance)
echo   [4]  Clear Cache Only            (no restart)
echo   [5]  Check Server Status
echo   [6]  Exit
echo.
echo  ============================================================
echo.
set /p CHOICE=  Enter choice [1-6]: 

if "%CHOICE%"=="1" goto START
if "%CHOICE%"=="2" goto CLEAR_AND_START
if "%CHOICE%"=="3" goto KILL
if "%CHOICE%"=="4" goto CLEAR_ONLY
if "%CHOICE%"=="5" goto STATUS
if "%CHOICE%"=="6" goto END
echo   Invalid choice. Try again.
timeout /t 1 >nul
goto MENU

:: ─── START ────────────────────────────────────────────────────
:START
echo.
echo  [INFO] Starting Smart Screener server on port 5050...
echo  [INFO] Open browser at: http://localhost:5050
echo  [INFO] Press CTRL+C to stop the server.
echo.
python app.py
echo.
echo  [INFO] Server exited.
pause
goto MENU

:: ─── CLEAR CACHE + START ──────────────────────────────────────
:CLEAR_AND_START
echo.
echo  [STEP 1/3] Killing any running server on port 5050...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5050 "') do (
    taskkill /PID %%a /F >nul 2>&1
)
echo  [STEP 2/3] Clearing caches...
call :DO_CLEAR
echo  [STEP 3/3] Starting server...
echo.
echo  [INFO] Open browser at: http://localhost:5050
echo  [INFO] Press CTRL+C to stop.
echo.
python app.py
echo.
echo  [INFO] Server exited.
pause
goto MENU

:: ─── KILL SERVER ──────────────────────────────────────────────
:KILL
echo.
echo  [INFO] Killing server on port 5050...
set KILLED=0
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5050 "') do (
    taskkill /PID %%a /F >nul 2>&1
    set KILLED=1
)
if "!KILLED!"=="1" (
    echo  [OK]   Server stopped.
) else (
    echo  [INFO] No server found running on port 5050.
)
echo.
pause
goto MENU

:: ─── CLEAR ONLY ───────────────────────────────────────────────
:CLEAR_ONLY
echo.
call :DO_CLEAR
echo.
pause
goto MENU

:: ─── STATUS ───────────────────────────────────────────────────
:STATUS
echo.
echo  [INFO] Checking server status...
curl -s -o nul -w "  HTTP Status: %%{http_code}\n" http://localhost:5050/api/health 2>nul
if errorlevel 1 (
    echo  [DOWN]  Server is NOT running on port 5050.
) else (
    echo  [UP]    Server is RUNNING. Open: http://localhost:5050
)
echo.
pause
goto MENU

:: ─── CLEAR SUBROUTINE ─────────────────────────────────────────
:DO_CLEAR
echo  Clearing cache directories...

REM Detail page indicator cache
if exist "cache\detail" (
    del /Q "cache\detail\*" >nul 2>&1
    echo  [OK] cache\detail\ cleared
)

REM Financial data cache
if exist "cache\financials" (
    del /Q "cache\financials\*" >nul 2>&1
    echo  [OK] cache\financials\ cleared
)

REM Fundamentals disk cache
if exist "cache\fundamentals" (
    del /Q "cache\fundamentals\*" >nul 2>&1
    echo  [OK] cache\fundamentals\ cleared
)

REM Python bytecode
for /d /r . %%d in (__pycache__) do (
    if exist "%%d" rd /s /q "%%d" >nul 2>&1
)
echo  [OK] __pycache__ cleared

REM Dead Letter Queue
if exist "cache\dlq.jsonl" (
    del /Q "cache\dlq.jsonl" >nul 2>&1
    echo  [OK] DLQ cleared
)

REM Corrupt JSON files
for /r "cache" %%f in (*.corrupt) do (
    del /Q "%%f" >nul 2>&1
)
echo  [OK] Corrupt cache files removed

echo.
echo  [DONE] Cache clear complete.
goto :eof

:: ─── EXIT ─────────────────────────────────────────────────────
:END
echo.
echo  Goodbye.
timeout /t 1 >nul
exit /b 0
