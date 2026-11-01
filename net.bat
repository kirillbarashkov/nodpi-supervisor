@echo off
chcp 65001 >nul
rem Thin wrapper around net.py so the project works from a double click too.
rem   net.bat status | start | stop | restart | check | logs access
where python >nul 2>&1 || (echo Python not found in PATH & pause & exit /b 1)
python "%~dp0net.py" %*
if "%~1"=="" pause
