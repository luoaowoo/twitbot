#!/usr/bin/env bash
# ============================================================
#  twitbot 一键启动（Linux / macOS）
#  用法： ./run.sh          拉起全部组件
#         ./run.sh --no-tg  不拉 Telegram
#  首次运行会自动建虚拟环境、装依赖、装 Chromium。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"

# ── 首次运行：建 venv 并装依赖 ──
if [ ! -x "$PY" ]; then
    echo "[1/3] 首次运行，正在创建虚拟环境..."
    if command -v python3 >/dev/null 2>&1; then
        python3 -m venv .venv
    else
        echo "[错误] 未找到 python3，请先安装 Python 3.10+。" >&2
        exit 1
    fi
    echo "[2/3] 正在安装依赖（首次较慢）..."
    "$PY" -m pip install --upgrade pip
    "$PY" -m pip install -r requirements.txt
    echo "[3/3] 正在安装 Chromium（无头浏览器发布方式需要，约 150MB）..."
    "$PY" -m playwright install chromium
    echo "环境准备完成！"
    echo
fi

# ── 首次运行：生成 .env ──
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    echo "已从 .env.example 生成 .env（可留空，稍后在 Web 控制台里配置）"
fi

echo "============================================================"
echo "  正在启动 twitbot ..."
echo "  控制台： http://127.0.0.1:8787"
echo "  按 Ctrl+C 停止全部服务。"
echo "============================================================"
echo

exec "$PY" start.py "$@"
