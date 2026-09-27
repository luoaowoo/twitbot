@echo off
REM ============================================================
REM  twitbot 一键启动（Windows）
REM  双击本文件即可：自动建虚拟环境、装依赖、然后拉起全部组件
REM  等价于： python start.py    （Web 控制台 + 发布循环 + Telegram）
REM ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"

REM ── 首次运行：建 venv 并装依赖 ──
if not exist "%PY%" (
    echo [1/3] 首次运行，正在创建虚拟环境...
    where py >nul 2>nul
    if %errorlevel%==0 (
        py -3 -m venv .venv
    ) else (
        python -m venv .venv
    )
    if not exist "%PY%" (
        echo.
        echo [错误] 虚拟环境创建失败。请确认已安装 Python 3.10+ 并加入 PATH。
        echo        下载： https://www.python.org/downloads/
        echo.
        pause
        exit /b 1
    )
    echo [2/3] 正在安装依赖（首次较慢，请耐心等待）...
    "%PY%" -m pip install --upgrade pip
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败，请检查网络后重试。
        pause
        exit /b 1
    )
    echo [3/3] 正在安装 Chromium（无头浏览器发布方式需要，约 150MB）...
    "%PY%" -m playwright install chromium
    echo.
    echo 环境准备完成！
    echo.
)

REM ── 首次运行：生成 .env ──
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo 已从 .env.example 生成 .env（可留空，稍后在 Web 控制台里配置）
    )
)

echo ============================================================
echo   正在启动 twitbot ...
echo   控制台稍后会自动打开浏览器： http://127.0.0.1:8787
echo   关闭本窗口即停止全部服务。
echo ============================================================
echo.

REM 延迟 5 秒后自动开浏览器（等服务起来）
start "" /b cmd /c "timeout /t 5 >nul & start http://127.0.0.1:8787"

"%PY%" start.py %*
set "RC=%errorlevel%"

echo.
if not "%RC%"=="0" (
    echo [服务已退出，退出码 %RC%] 若为端口占用或锁残留，请阅读上面日志。
) else (
    echo [服务已正常退出]
)
pause
endlocal
