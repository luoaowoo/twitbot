# 首次配置（Windows）
# 用法：右键 -> 使用 PowerShell 运行；或在 PowerShell 里执行 .\setup.ps1

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "==> 检查 Python" -ForegroundColor Cyan
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Write-Host "[x] 未找到 python，请先装 Python 3.10+ 并勾选 Add to PATH" -ForegroundColor Red; exit 1 }
python --version

Write-Host "`n==> 创建虚拟环境 .venv" -ForegroundColor Cyan
if (-not (Test-Path ".venv")) { python -m venv .venv }
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Write-Host "`n==> 安装依赖" -ForegroundColor Cyan
& $venvPy -m pip install --upgrade pip -q
& $venvPy -m pip install -r requirements.txt

Write-Host "`n==> 安装 Chromium（无头浏览器后端需要，约 150MB）" -ForegroundColor Cyan
try {
    & $venvPy -m playwright install chromium
    Write-Host "    Chromium 就绪" -ForegroundColor Green
} catch {
    Write-Host "    [!] Chromium 安装失败。仅用 X API 后端的话可以忽略；" -ForegroundColor Yellow
    Write-Host "        需要浏览器后端时手动执行： .\.venv\Scripts\python.exe -m playwright install chromium" -ForegroundColor Yellow
}

Write-Host "`n==> 准备 .env" -ForegroundColor Cyan
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host "已从 .env.example 生成 .env" }

Write-Host "`n==> 自检（无需任何凭据）" -ForegroundColor Cyan
& $venvPy smoke.py

Write-Host "`n完成！启动方式：" -ForegroundColor Green
Write-Host ""
Write-Host "  【推荐】一条命令全启动（Web 控制台 + 发布循环 + Telegram）" -ForegroundColor White
Write-Host "    .\run.bat                     # 或双击 run.bat"
Write-Host ""
Write-Host "    然后浏览器打开 http://127.0.0.1:8787"
Write-Host ""
Write-Host "  启动后在网页上完成两件事：" -ForegroundColor White
Write-Host "    1) 「发布后端」卡片里选一种方式并登录/配凭据"
Write-Host "       · 无头浏览器：填 X 账号密码点『用账号密码登录』，或点『登录 X』开窗口手工登录"
Write-Host "       · 官方 API ：运行 python setup_x.py"
Write-Host "    2) 「Telegram 机器人」面板填 BotFather 给的 token，点『校验 Token』→『启动机器人』"
Write-Host ""
Write-Host "  其它组合：" -ForegroundColor White
Write-Host "    python start.py --no-tg        不接 Telegram"
Write-Host "    python start.py --no-web       无界面，只跑发布循环"
Write-Host "    python start.py --web-only     只起控制台"
