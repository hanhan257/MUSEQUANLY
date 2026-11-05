@echo off
setlocal
cd /d "%~dp0"

echo Cai PyInstaller va Waitress...
python -m pip install -q -r requirements.txt
python -m pip install -q pyinstaller waitress

echo Build MuseManager.exe...
python -m PyInstaller muse_manager.spec --noconfirm --clean
if errorlevel 1 (
  echo Build that bai.
  exit /b 1
)

set OUT=dist\MuseManager.exe
if not exist "%OUT%" (
  echo Khong thay %OUT%
  exit /b 1
)

echo.
echo Xong: %CD%\%OUT%
echo.
echo Copy warehouses.json, cookies.json vao cung thu muc voi MuseManager.exe neu can.
echo Can Google Chrome da cai de tao/dang nhap Muse.
echo.
pause
