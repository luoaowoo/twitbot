# Windows 计划任务安装脚本：开机自启 + 崩溃自动重启
# 用法（管理员 PowerShell）：  .\install-task.ps1
#   -RunAsUser "DOMAIN\user"   指定运行身份（默认当前用户）
#   -NoTelegram                不启用 Telegram（默认会启用，没配 token 也不影响启动）
#   -WebOnly                   只跑 Web 控制台，不跑发布循环

param(
    [string]$TaskName = "TwitBot",
    [string]$RunAsUser = "$env:USERDOMAIN\$env:USERNAME",
    [switch]$NoTelegram,
    [switch]$WebOnly
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $root ".venv\Scripts\python.exe"
$entry = Join-Path $root "start.py"

if (-not (Test-Path $python)) {
    Write-Host "[x] 未找到 $python，请先运行 setup.ps1" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Host "[x] 未找到 .env，请先运行 setup.ps1 并配置" -ForegroundColor Red
    exit 1
}

# 组装参数（默认全量启动：Web + 发布循环 + Telegram）
$argList = @("`"$entry`"")
if ($NoTelegram) { $argList += "--no-tg" }
if ($WebOnly)    { $argList += "--web-only" }
$argStr = $argList -join " "

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "[i] 已存在同名任务，先删除旧的"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute $python -Argument $argStr -WorkingDirectory $root
# 开机启动，且进程意外退出后 1 分钟重启（最多 999 次）
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -MultipleInstances IgnoreNew

# S4U：不存密码也能以该用户身份跑（无桌面交互）。注意：
# 浏览器后端首次登录需要图形界面，请在安装为服务**之前**先跑 tools\browser_login.py 完成登录。
$principal = New-ScheduledTaskPrincipal -UserId $RunAsUser -LogonType S4U -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "Telegram/Web -> X 自动发帖（Web 控制台 + 发布循环）" | Out-Null

Write-Host "[OK] 已注册计划任务：$TaskName" -ForegroundColor Green
Write-Host "     ExecStart : $python $argStr"
Write-Host "     立即启动： Start-ScheduledTask -TaskName $TaskName"
Write-Host "     查看状态： Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host "     停止：     Stop-ScheduledTask -TaskName $TaskName"
Write-Host "     卸载：     Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
Write-Host ""
Write-Host "[!] 注意：计划任务以 S4U 运行在无桌面会话中，" -ForegroundColor Yellow
Write-Host "    浏览器后端（无头）可用，但首次登录必须在有桌面的会话里先跑一次：" -ForegroundColor Yellow
Write-Host "      .\.venv\Scripts\python.exe tools\browser_login.py" -ForegroundColor Yellow
