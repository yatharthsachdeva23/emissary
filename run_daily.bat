@echo off
cd /d "%~dp0"

:: Force Windows console to UTF-8 so symbols and emojis print properly
chcp 65001 >nul

:: Unbuffer Python output so terminal updates live in real-time
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8

if not exist logs mkdir logs

echo ===================================================
echo [EMISSARY] Starting Daily Pipeline at %DATE% %TIME%
echo ===================================================

echo =================================================== >> logs\cron.log
echo [RUN STARTED] %DATE% %TIME% >> logs\cron.log
echo =================================================== >> logs\cron.log

:: Run main.py directly in the terminal.
:: DualLogger in main.py outputs live to this console window AND writes to logs\emissary.log simultaneously.
py -3.12 main.py
set EXIT_CODE=%ERRORLEVEL%

echo ===================================================
echo [EMISSARY] Finished at %DATE% %TIME% (exit code: %EXIT_CODE%)
echo ===================================================

echo [RUN FINISHED] %DATE% %TIME% with exit code %EXIT_CODE% >> logs\cron.log

exit /b %EXIT_CODE%

