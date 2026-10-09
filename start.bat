@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
title 镜像搜索 / 拉取 / 打包  (关掉本窗口即停止服务)

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   [X] 没有找到 python 命令。
  echo       请到 https://www.python.org/downloads/windows/ 下载安装 Python 3.9 以上版本，
  echo       安装界面第一屏务必勾选 "Add python.exe to PATH"，装完重新双击本文件。
  echo.
  pause
  exit /b 1
)

echo.
echo   正在启动…… 浏览器会自动打开 http://127.0.0.1:8799
echo   这个黑窗口不要关，关掉就等于停止服务。
echo.

python "%~dp0app.py" --port 8799 --open
echo.
echo   服务已停止。
pause
