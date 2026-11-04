@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\微光域.lnk"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell; $sc = $ws.CreateShortcut('%LNK%'); $sc.TargetPath = '%~dp0启动.bat'; $sc.WorkingDirectory = '%~dp0'; $sc.WindowStyle = 7; $sc.Description = '微光域 LumiSphere'; $sc.Save()"

if exist "%LNK%" (
    echo.
    echo 已设置开机自启：登录 Windows 后微光域会自动启动到托盘。
    echo 快捷方式位置：%LNK%
) else (
    echo.
    echo 设置失败，请确认有权限写入启动目录。
)
echo.
pause