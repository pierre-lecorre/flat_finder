@echo off
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
echo ====================================================================
echo [Flat Finder] Launching Localhost Web Application...
echo ====================================================================
echo Open your browser and navigate to: http://127.0.0.1:8000
echo ====================================================================
py -m app.cli serve
pause
