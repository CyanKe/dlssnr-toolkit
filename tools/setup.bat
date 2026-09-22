@echo off
REM ===========================================================================
REM  setup.bat -- install the Python dependencies
REM ===========================================================================
setlocal
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python 3 not found.
  echo         Install Python 3 and tick "Add Python to PATH", then re-run.
  echo         Download: https://www.python.org/downloads/
  pause
  exit /b 1
)
echo Checking / installing dependencies (numpy, opencv-python, pillow, tkinterdnd2)...
python -m pip install --upgrade pip >nul 2>nul
python -m pip install numpy opencv-python pillow tkinterdnd2
echo.
echo ========================================
echo  Dependencies installed.
echo  1) Double-click tools\run.bat
echo     (or run: python app\gui.py  from the project root)
echo  2) Requires an NVIDIA GPU + a recent driver
echo ========================================
pause
