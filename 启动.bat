@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [错误] 还没安装运行环境。
    echo 请先双击运行「安装环境.bat」。
    pause
    exit /b 1
)

rem 用 pythonw 启动：不弹黑色控制台窗口，程序就在右下角托盘里
start "" ".venv\Scripts\pythonw.exe" "main.py"