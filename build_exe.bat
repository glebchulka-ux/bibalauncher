@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :nopython
echo Ставлю библиотеки...
%PY% -m pip install -r requirements.txt pyinstaller || goto :fail
echo Собираю BibaLauncher.exe, это займёт пару минут...
%PY% -m PyInstaller --noconfirm --clean --onefile --noconsole --name BibaLauncher --icon assets\icon.ico --collect-all customtkinter --collect-all minecraft_launcher_lib biba.py || goto :fail
echo.
echo Готово! Программа лежит здесь: dist\BibaLauncher.exe
pause
exit /b 0
:nopython
echo Python не найден. Установите Python 3.10 или новее с python.org и отметьте "Add python.exe to PATH".
pause
exit /b 1
:fail
echo Не получилось собрать. Посмотрите сообщения выше.
pause
exit /b 1
