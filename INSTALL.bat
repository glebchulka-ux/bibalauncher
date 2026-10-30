@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Установка Biba Launcher

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :nopython

echo [1/3] Ставлю библиотеки...
%PY% -m pip install -r requirements.txt pyinstaller || goto :fail
echo.
echo [2/3] Собираю приложение, это займёт пару минут...
%PY% -m PyInstaller --noconfirm --clean --onefile --noconsole --name BibaLauncher --icon assets\icon.ico --collect-all customtkinter --collect-all minecraft_launcher_lib biba.py || goto :fail
echo.
echo [3/3] Устанавливаю и делаю ярлыки...
set "DEST=%LOCALAPPDATA%\BibaLauncher"
if not exist "%DEST%" mkdir "%DEST%"
taskkill /f /im BibaLauncher.exe >nul 2>nul
taskkill /f /im GlebSter.exe >nul 2>nul
copy /y "dist\BibaLauncher.exe" "%DEST%\BibaLauncher.exe" >nul || goto :fail
copy /y "assets\icon.ico" "%DEST%\BibaLauncher.ico" >nul

rem убираем прежнюю установку под старым именем
rmdir /s /q "%LOCALAPPDATA%\GlebSter" 2>nul
del "%USERPROFILE%\Desktop\GlebSter.lnk" 2>nul
del "%USERPROFILE%\OneDrive\Desktop\GlebSter.lnk" 2>nul
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\GlebSter.lnk" 2>nul

set "VBS=%TEMP%\biba_shortcut.vbs"
> "%VBS%" echo Set s = CreateObject("WScript.Shell")
>> "%VBS%" echo For Each p In Array(s.SpecialFolders("Desktop"), s.SpecialFolders("Programs"))
>> "%VBS%" echo   Set l = s.CreateShortcut(p ^& "\Biba Launcher.lnk")
>> "%VBS%" echo   l.TargetPath = "%DEST%\BibaLauncher.exe"
>> "%VBS%" echo   l.WorkingDirectory = "%DEST%"
>> "%VBS%" echo   l.IconLocation = "%DEST%\BibaLauncher.ico"
>> "%VBS%" echo   l.Description = "Biba Launcher"
>> "%VBS%" echo   l.Save
>> "%VBS%" echo Next
cscript //nologo "%VBS%" >nul 2>nul
if errorlevel 1 powershell -NoProfile -Command "$s=New-Object -ComObject WScript.Shell; foreach($p in @($s.SpecialFolders('Desktop'),$s.SpecialFolders('Programs'))){$l=$s.CreateShortcut($p+'\Biba Launcher.lnk');$l.TargetPath='%DEST%\BibaLauncher.exe';$l.WorkingDirectory='%DEST%';$l.IconLocation='%DEST%\BibaLauncher.ico';$l.Save()}"
del "%VBS%" >nul 2>nul

echo.
echo Готово! Ярлык Biba Launcher появился на рабочем столе и в меню Пуск.
start "" "%DEST%\BibaLauncher.exe"
pause
exit /b 0

:nopython
echo Python не найден. Сейчас откроется страница загрузки.
echo Установите Python 3.10 или новее, отметьте "Add python.exe to PATH" и запустите этот файл снова.
echo Или соберите exe без Python через GitHub: инструкция в README.txt.
start "" https://www.python.org/downloads/windows/
pause
exit /b 1

:fail
echo.
echo Не получилось. Посмотрите сообщения выше.
pause
exit /b 1
