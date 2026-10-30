@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :nopython
%PY% -c "import customtkinter, minecraft_launcher_lib, requests, PIL" >nul 2>nul || %PY% -m pip install -r requirements.txt
where pyw >nul 2>nul && (start "" pyw -3 biba.py) || (start "" pythonw biba.py)
exit /b 0
:nopython
echo Python не найден. Установите Python 3.10 или новее с python.org и отметьте "Add python.exe to PATH".
pause
exit /b 1
