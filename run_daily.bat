@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
echo =================================================== >> logs\cron.log
echo [RUN STARTED] %DATE% %TIME% >> logs\cron.log
echo =================================================== >> logs\cron.log
py -3.12 main.py >> logs\cron.log 2>&1
echo [RUN FINISHED] %DATE% %TIME% with exit code %ERRORLEVEL% >> logs\cron.log
