#!/usr/bin/env bash
# 首次配置（Linux / macOS）
# 用法：bash setup.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "==> 检查 Python"
command -v python3 >/dev/null || { echo "[x] 未找到 python3"; exit 1; }
python3 --version

echo
echo "==> 创建虚拟环境 .venv"
[ -d .venv ] || python3 -m venv .venv
PY=".venv/bin/python"

echo
echo "==> 安装依赖"
"$PY" -m pip install --upgrade pip -q
"$PY" -m pip install -r requirements.txt

echo
echo "==> 安装 Chromium（无头浏览器后端需要）"
if "$PY" -m playwright install chromium; then
  echo "    Chromium 就绪"
else
  echo "    [!] Chromium 安装失败。仅用 X API 后端可忽略；"
  echo "        需要浏览器后端时手动执行： .venv/bin/python -m playwright install chromium"
fi
# Linux 常缺系统依赖，尝试补齐（无 sudo 时跳过）
if command -v sudo >/dev/null && [ "$(uname)" = "Linux" ]; then
  echo "    （如需系统依赖库，可执行： sudo .venv/bin/python -m playwright install-deps chromium ）"
fi

echo
echo "==> 准备 .env"
[ -f .env ] || { cp .env.example .env; echo "已从 .env.example 生成 .env"; }
chmod 600 .env 2>/dev/null || true

echo
echo "==> 自检（无需任何凭据）"
"$PY" smoke.py

echo
echo "完成！启动方式："
echo ""
echo "  【推荐】一条命令全启动（Web 控制台 + 发布循环 + Telegram）"
echo "    ./run.sh"
echo ""
echo "    然后浏览器打开 http://127.0.0.1:8787"
echo ""
echo "  启动后在网页上完成两件事："
echo "    1) 「发布后端」卡片里选一种方式并登录/配凭据"
echo "       · 无头浏览器：填 X 账号密码点『用账号密码登录』，或点『登录 X』开窗口手工登录"
echo "       · 官方 API ：运行 .venv/bin/python setup_x.py"
echo "    2) 「Telegram 机器人」面板填 BotFather 给的 token，点『校验 Token』→『启动机器人』"
echo ""
echo "  其它组合："
echo "    .venv/bin/python start.py --no-tg      不接 Telegram"
echo "    .venv/bin/python start.py --no-web     无界面，只跑发布循环"
echo "    .venv/bin/python start.py --web-only   只起控制台"
