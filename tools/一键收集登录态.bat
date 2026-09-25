@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

title twitbot 登录态收集

echo ============================================================
echo   twitbot 登录态收集工具
echo ============================================================
echo.
echo   这个工具会：
echo     1. 打开一个 Chrome 窗口，让你正常登录 X
echo     2. 自动把登录态保存下来
echo     3. 复制到桌面，方便你传到服务器
echo.
echo   登录过程和你平时用 Chrome 完全一样。
echo ============================================================
echo.
pause

REM ── 1) 找 Python ──────────────────────────────────────────
set "PYEXE="
if exist ".venv\Scripts\python.exe" (
    set "PYEXE=.venv\Scripts\python.exe"
) else (
    where py >nul 2>nul && set "PYEXE=py -3"
    if not defined PYEXE (
        where python >nul 2>nul && set "PYEXE=python"
    )
)

if not defined PYEXE (
    echo.
    echo [错误] 找不到 Python。
    echo        请先安装 Python 3.10 以上：https://www.python.org/downloads/
    echo        安装时记得勾选 "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

echo [1/3] 检查 Python 环境...
%PYEXE% -c "import sys; assert sys.version_info >= (3,10)" 2>nul
if errorlevel 1 (
    echo [错误] Python 版本太低，需要 3.10 或更高。
    echo.
    pause
    exit /b 1
)
echo       OK

REM ── 2) 检查依赖 ───────────────────────────────────────────
echo.
echo [2/3] 检查依赖...
%PYEXE% -c "import playwright, websockets" >nul 2>nul
if errorlevel 1 (
    echo       缺少依赖，正在安装（首次较慢）...
    %PYEXE% -m pip install --upgrade pip
    if exist "requirements.txt" (
        %PYEXE% -m pip install -r requirements.txt
    ) else (
        %PYEXE% -m pip install playwright websockets
    )
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败，请检查网络后重试。
        echo.
        pause
        exit /b 1
    )
    %PYEXE% -m playwright install chromium >nul 2>nul
)
echo       OK

REM ── 3) 登录 ───────────────────────────────────────────────
echo.
echo [3/3] 正在打开 Chrome 登录窗口...
echo.
echo       登录完成后不用手动关窗口，程序会自动保存。
echo       超过 10 分钟没完成会自动退出。
echo.

if not exist "tools\browser_login_chrome.py" (
    echo [错误] 找不到 tools\browser_login_chrome.py
    echo        请把本工具放在 twitbot 项目根目录下运行。
    echo.
    pause
    exit /b 1
)

%PYEXE% "tools\browser_login_chrome.py" --timeout 600
set "RC=%errorlevel%"

echo.
if not "%RC%"=="0" (
    echo ============================================================
    echo   登录没有成功（退出码 %RC%）
    echo ============================================================
    echo.
    echo   常见原因：
    echo     · 超时没登完 —— 重新运行，动作快一点
    echo     · X 要求人机验证 —— 在窗口里手动过一下
    echo     · 网络访问不了 x.com —— 检查代理 / VPN
    echo.
    pause
    exit /b %RC%
)

REM ── 4) 复制到桌面 ────────────────────────────────────────
set "STATE=data\browser\storage_state.json"
if not exist "%STATE%" (
    echo.
    echo [警告] 登录报告成功，但找不到：%STATE%
    echo.
    pause
    exit /b 1
)

set "DESKTOP=%USERPROFILE%\Desktop"
if not exist "%DESKTOP%" set "DESKTOP=%USERPROFILE%"
set "OUT=%DESKTOP%\storage_state.json"

copy /y "%STATE%" "%OUT%" >nul
if errorlevel 1 (
    echo.
    echo [警告] 复制到桌面失败，登录态仍在：%STATE%
    echo        请手动复制这个文件。
    echo.
    pause
    exit /b 0
)

echo ============================================================
echo   成功！
echo ============================================================
echo.
echo   登录态已保存到桌面：
echo     %OUT%
echo.
echo   接下来：
echo     1. 传到 Linux 服务器：
echo          scp "%OUT%" user@服务器:/tmp/storage_state.json
echo     2. 在服务器上导入并验证：
echo          .venv/bin/python twitbot-cli.py state import /tmp/storage_state.json
echo          .venv/bin/python twitbot-cli.py state check
echo.
echo   注意：这个文件等同于你的 X 账号凭据，请妥善保管，
echo         不要发给别人或传到公开的地方。
echo.
pause
endlocal
