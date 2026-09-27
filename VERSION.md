# twitbot —— 服务器版

这是**服务器版**，专为**无桌面的 Linux 服务器**准备。

## 与桌面版的关键区别

**服务器上不能登录。** 浏览器登录需要弹出真实窗口，
服务器没显示器 —— 这是物理限制，不是 bug。

正确流程：

```
① 在有桌面的机器上登录一次（用桌面版）
        ↓
② 拿到 data/browser/storage_state.json
        ↓
③ 传到服务器（用本版的 CLI 或 Web 控制台）
        ↓
④ 服务器无头发帖
```

## 本版额外提供

| 功能 | 说明 |
|---|---|
| 命令行工具 | `twitbot-cli.py` —— 导入登录态、投料、看队列、看状态 |
| Web 上传登录态 | 控制台新增「上传登录态」接口 |
| root/容器兼容 | 自动加 `--no-sandbox`（Linux + root 时） |
| 无图形界面提示 | 在无桌面环境跑登录会明确告诉你怎么办，而不是卡死 |
| 自定义 Chrome 参数 | `TWITBOT_CHROME_ARGS` 环境变量（代理等场景） |

## 快速开始

**完整步骤见 `deploy/LINUX-DEPLOY.md`。** 简版：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
sudo .venv/bin/python -m playwright install-deps chromium

cp .env.example .env        # 改 WEB_TOKEN / WEB_HOST 等

# 导入在别处登录好的登录态
.venv/bin/python twitbot-cli.py state import /tmp/state.json
.venv/bin/python twitbot-cli.py state check      # 务必验证

# 启动
.venv/bin/python twitbot-cli.py start --no-tg
```

## 命令行速查

```bash
.venv/bin/python twitbot-cli.py state import <file>   # 导入登录态
.venv/bin/python twitbot-cli.py state show            # 看概况
.venv/bin/python twitbot-cli.py state check           # 联网验证
.venv/bin/python twitbot-cli.py status                # 队列/配额
.venv/bin/python twitbot-cli.py backend               # 后端可用性
.venv/bin/python twitbot-cli.py post --text "内容"     # 投料
.venv/bin/python twitbot-cli.py queue                 # 看队列
.venv/bin/python twitbot-cli.py start --no-tg         # 启动
```

## 文档

- **`deploy/LINUX-DEPLOY.md`** —— 服务器部署完整指南（先看这个）
- `deploy/OPS.md` —— 通用运维
- `README.md` —— 功能说明
