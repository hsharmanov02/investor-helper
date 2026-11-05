@echo off
REM Builds InvestorHelper.exe on this PC. Double-click it from the unzipped folder.
setlocal
cd /d "%~dp0"

where py >nul 2>nul && (set PY=py -3) || (where python >nul 2>nul && (set PY=python))
if not defined PY (
  echo Python was not found. Installing Python 3.12 with winget...
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)

echo Creating a private Python environment in .venv ...
%PY% -m venv .venv || goto :fail
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
pip install -r requirements-dev.txt tzdata || goto :fail

echo Running tests...
set QT_QPA_PLATFORM=offscreen
python -m pytest -q || goto :fail
set QT_QPA_PLATFORM=

echo Building the EXE (takes a few minutes)...
pyinstaller --noconfirm --clean packaging\InvestorHelper.spec || goto :fail

copy /y dist\InvestorHelper.exe "%~dp0InvestorHelper.exe" >nul
echo.
echo Done: %~dp0InvestorHelper.exe
explorer /select,"%~dp0InvestorHelper.exe"
pause
exit /b 0

:fail
echo.
echo Build failed. Scroll up for the error, or send a screenshot.
pause
exit /b 1
