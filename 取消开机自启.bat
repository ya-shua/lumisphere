@echo off
chcp 65001 >nul

set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\微光域.lnk"

if exist "%LNK%" (
    del /f /q "%LNK%"
    echo.
    echo 已取消开机自启。
) else (
    echo.
    echo 本来就没有设置开机自启。
)
echo.
pause