@echo off
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
echo ====================================================================
echo [Flat Finder] Starting SQLite database setup and active site crawls...
echo ====================================================================
py -m app.cli init-db
py -m app.cli scrape --site all
echo ====================================================================
echo Crawl session complete. Run start_webapp.bat to launch browser board.
echo ====================================================================
pause
