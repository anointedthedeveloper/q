@echo off
echo ================================
echo    ALOC Downloader Setup
echo ================================
echo.

:: Detect python command (python or py)
python --version >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON=python
    goto :found
)

py --version >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON=py
    goto :found
)

echo Python not found. Downloading installer...
curl -o python_installer.exe https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
echo Installing Python...
python_installer.exe /quiet InstallAllUsers=1 PrependPath=1 Include_pip=1
del python_installer.exe
echo Done. Please close and reopen this window, then run setup.bat again.
pause
exit

:found
echo Using: %PYTHON%
%PYTHON% --version
echo.

echo Upgrading pip...
%PYTHON% -m pip install --upgrade pip

echo.
echo Installing packages...
%PYTHON% -m pip install requests --user
%PYTHON% -m pip install python-dotenv --user
%PYTHON% -m pip install watchdog --user

echo.
echo Verifying installs...
%PYTHON% -c "import requests; print('requests OK')"
%PYTHON% -c "import dotenv; print('python-dotenv OK')"
%PYTHON% -c "import watchdog; print('watchdog OK')"

echo.
echo ================================
echo Setup complete!
echo Run: %PYTHON% download.py
echo Run: %PYTHON% autopush.py
echo ================================
pause
