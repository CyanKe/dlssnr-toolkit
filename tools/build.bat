@echo off
REM ===========================================================================
REM  build.bat -- rebuild the native components from source
REM
REM  Requires Visual Studio 2022 (or Build Tools) with the C++ workload, and the
REM  SDKs placed under deps\ :
REM      deps\sdk_include\   NVIDIA NGX headers   (for dlssnr_host2)
REM      deps\sdk_lib\       NVIDIA NGX static lib
REM      deps\rtx_video_sdk\ official RTX Video SDK 1.1
REM
REM  deps\ is NOT part of the repository: those files are NVIDIA proprietary and
REM  must be obtained by the user. See docs\THIRD_PARTY.md.
REM
REM  Usage:  tools\build.bat            build everything possible
REM          tools\build.bat host       build only dlssnr_host2.dll
REM
REM  NOTE: the DirectShow filter (dlssnr_dshow.dll) lives in a SEPARATE
REM  repository, dlssnr-filter. It is not built here.
REM ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set ROOT=%CD%
set OUT=%ROOT%\app
REM Intermediates go here, NOT into the source tree: `cl` writes .obj next to the
REM current directory, which used to litter the repo root. build\ is gitignored.
set BUILD=%ROOT%\build
if not exist "%BUILD%" mkdir "%BUILD%"

REM ---- locate vcvars64.bat ----
set VCVARS=
for %%P in (
  "E:\MSVC\Product\VC\Auxiliary\Build\vcvars64.bat"
  "%ProgramFiles%\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
  "%ProgramFiles%\Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvars64.bat"
  "%ProgramFiles%\Microsoft Visual Studio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat"
  "%ProgramFiles(x86)%\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
) do (
  if exist %%P set VCVARS=%%~P
)
if "%VCVARS%"=="" (
  echo [error] vcvars64.bat not found.
  echo         Install Visual Studio 2022 with the "Desktop development with C++" workload.
  pause
  exit /b 1
)
echo Using %VCVARS%

set WHAT=%~1
if "%WHAT%"=="" set WHAT=all

REM ------------------------------------------------------------- engine host
:hosts
if not exist "%ROOT%\deps\sdk_include\nvsdk_ngx.h" (
  echo.
  echo [skip] deps\sdk_include not present - cannot build dlssnr_host2.dll
  echo        See docs\THIRD_PARTY.md for how to obtain the NVIDIA NGX SDK.
  goto rtx
)

echo.
echo === building dlssnr_host2.dll (needs the NGX SDK in deps\) ===
call "%VCVARS%" >nul 2>&1
cl /nologo /EHsc /O2 /utf-8 ^
   /Fo"%BUILD%\\" /I "%ROOT%\deps\sdk_include" "%ROOT%\src\dlssnr_host2.cpp" ^
   /LD /Fe:"%OUT%\dlssnr_host2.dll" ^
   /link d3d12.lib dxgi.lib user32.lib advapi32.lib "%ROOT%\deps\sdk_lib\nvsdk_ngx_s.lib"
if errorlevel 1 ( echo [FAILED] dlssnr_host2.dll & pause & exit /b 1 )
if /i "%WHAT%"=="host" goto cleanup

REM ---------------------------------------------------------- rtx video hosts
:rtx
if not exist "%ROOT%\deps\rtx_video_sdk\include\nvsdk_ngx.h" (
  echo.
  echo [skip] deps\rtx_video_sdk not present - cannot build vfx_host.dll / truehdr_host.dll
  goto cleanup
)

echo.
echo === building rtx_video hosts (need the RTX Video SDK in deps\) ===
call "%VCVARS%" >nul 2>&1
cl /nologo /EHsc /O2 /utf-8 /I "%ROOT%\deps\rtx_video_sdk\include" ^
   /Fo"%BUILD%\\vfx_" "%ROOT%\src\vfx_host.cpp" /LD /Fe:"%OUT%\vfx_host.dll" ^
   /link d3d11.lib dxgi.lib
if errorlevel 1 ( echo [FAILED] vfx_host.dll & pause & exit /b 1 )
cl /nologo /EHsc /O2 /utf-8 /I "%ROOT%\deps\rtx_video_sdk\include" ^
   /Fo"%BUILD%\\thdr_" "%ROOT%\src\truehdr_host.cpp" /LD /Fe:"%OUT%\truehdr_host.dll" ^
   /link d3d11.lib dxgi.lib advapi32.lib user32.lib ^
   "%ROOT%\deps\rtx_video_sdk\lib\Windows\x64\nvsdk_ngx_s.lib"
if errorlevel 1 ( echo [FAILED] truehdr_host.dll & pause & exit /b 1 )

:cleanup
REM cl drops an import .lib/.exp next to the DLL; they are not wanted.
del /q "%OUT%\*.exp" "%OUT%\dlssnr_host2.lib" "%OUT%\vfx_host.lib" "%OUT%\truehdr_host.lib" 2>nul

:done
echo.
echo ========================================
echo  Build finished. Output in app\
echo.
echo  The DirectShow filter lives in a separate repository (dlssnr-filter).
echo ========================================
