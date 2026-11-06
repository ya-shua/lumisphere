@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   微光域 LumiSphere - 安装运行环境
echo ============================================
echo.

echo [1/3] 创建 Python 虚拟环境（3.11）...
py -3.11 -m venv .venv
if errorlevel 1 (
    echo 创建失败。请确认已安装 Python 3.11。
    pause
    exit /b 1
)

echo [2/3] 安装依赖库...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\pip.exe" install -r requirements.txt
if errorlevel 1 (
    echo 依赖安装失败，请检查网络。
    pause
    exit /b 1
)

echo [3/3] 下载浏览器内核（约 200MB，请耐心等待）...
".venv\Scripts\playwright.exe" install chromium
if errorlevel 1 (
    echo.
    echo 官方源下载失败，改用国内镜像重试...
    set PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
    ".venv\Scripts\playwright.exe" install chromium
)

if errorlevel 1 (
    echo.
    echo 浏览器内核下载失败。
    echo 两个办法：
    echo   1. 检查网络后重新运行本脚本
    echo   2. 临时把 config.yaml 里的 channel 改成 "msedge"（用系统自带的 Edge，免下载）
    pause
    exit /b 1
)

echo.
echo 全部完成！现在可以双击「启动.bat」了。
pause