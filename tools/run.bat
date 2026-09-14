@echo off
REM ===========================================================================
REM  run.bat -- launch the GUI
REM
REM  The Python modules and the NVIDIA DLLs they load live together in app\,
REM  because dlss_engine.py / rtx_video.py resolve their DLLs relative to their
REM  own file. So we must run from inside app\.
REM ===========================================================================
setlocal
cd /d "%~dp0.."
if not exist "app\gui.py" (
  echo [error] app\gui.py not found.
  echo         Run this script from the tools\ folder.
  pause
  exit /b 1
)
pushd app
python gui.py
popd
