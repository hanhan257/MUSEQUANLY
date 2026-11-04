@echo off
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
set FLASK_RELOAD=1
python app.py
pause
