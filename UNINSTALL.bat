@echo off
chcp 65001 >nul
echo Удаляю программу Biba Launcher. Настройки, сборки и миры не трогаю.
taskkill /f /im BibaLauncher.exe >nul 2>nul
rmdir /s /q "%LOCALAPPDATA%\BibaLauncher" 2>nul
del "%USERPROFILE%\Desktop\Biba Launcher.lnk" 2>nul
del "%USERPROFILE%\OneDrive\Desktop\Biba Launcher.lnk" 2>nul
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Biba Launcher.lnk" 2>nul
echo Готово.
pause
