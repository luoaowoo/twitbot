# twitbot

**在 Linux 服务器上跑一个 X（Twitter）自动发帖服务** —— 通过 Telegram 机器人或 Web 控制台投料，自动排队发布。

不用 X 官方 API：用无头浏览器模拟真实网页操作发帖。

---

## 它能做什么

- **两种投料方式**：Telegram 机器人 / Web 控制台
- **图文混发**：一条推文最多带 4 张图（Telegram 相册自动合并成一条）
- **审核队列**：先预览再发，可逐条「发送 / 修改 / 删除」
- **排队发送**：可设发送间隔，防止连发触发风控
- **失败重试**：限流时精确等待，失败自动截图存证
- **双后端**：无头浏览器（免费）/ X 官方 API（需付费凭据）

---

## 重要前提：服务器上**不能**登录 X

浏览器登录需要**弹出真实窗口**让你输密码、过验证码。服务器没有显示器，弹不出来。

所以流程是：

```
① 在你自己电脑上登录一次（Windows / macOS / 带桌面的 Linux）
        ↓
② 得到 storage_state.json
        ↓
③ 传到服务器导入
        ↓
④ 服务器无头发布
```

**为什么不直接在服务器上自动登录？** 试过，不行：

| 方式 | 结果 |
|---|---|
| 无头 + `channel=chromium` | HTTP 403，页面空白 |
| 无头 + `channel=chrome` | HTTP 403，页面空白 |
| 无头（裸 headless） | HTTP 403，页面空白 |

连续采样页面元素数恒为 0 —— 登录表单根本加载不出来。这是 X 的反自动化策略，不是代码问题。

---

## 快速开始

### 一、准备登录态（在你的电脑上做）

**Windows**：双击

```
tools\一键收集登录态.bat
```

它会自动装依赖、弹出 Chrome 让你登录、把 `storage_state.json` 复制到桌面。

**macOS / Linux 桌面**：

```bash
python tools/browser_login_chrome.py
```

### 二、装到服务器

```bash
# 1) 拉代码
git clone <这个仓库的地址> /opt/twitbot
cd /opt/twitbot

# 2) 建环境和依赖
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# 3) 装浏览器
.venv/bin/python -m playwright install chromium
sudo .venv/bin/python -m playwright install-deps chromium

# 4) 配置
cp .env.example .env
nano .env        # 至少改 WEB_HOST / WEB_PORT
```

### 三、导入登录态并验证

```bash
# 把桌面上的 storage_state.json 传上来
scp storage_state.json user@你的服务器:/tmp/

# 导入
.venv/bin/python twitbot-cli.py state import /tmp/storage_state.json

# 验证（会联网检查，并自动识别出账号是谁）
.venv/bin/python twitbot-cli.py state check
```

看到这样就成了：

```
连通性：通过 ✅ —— 登录态有效（账号 你的名字 @yourhandle）
当前账号：你的名字 @yourhandle
```

### 四、启动

```bash
# 前台跑（调试用）
.venv/bin/python twitbot-cli.py start --no-tg

# 或做成常驻服务
sudo cp deploy/twitbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now twitbot
```

---

## 控制台

启动后访问 `http://你的服务器:8787`。

**首次访问会要求输入密码。** 密码来源（按优先级）：

1. `.env` 里的 `WEB_PASSWORD`
2. 留空则**首次启动自动生成随机密码**，写在 `data/console-password`

```bash
cat data/console-password    # 查看自动生成的密码
```

设为 `WEB_PASSWORD=-` 可关闭密码门（**仅供内网，公网切勿**）。

### 控制台能做什么

- 投料（文字 / 图片 / 视频，可多图）
- 看队列、暂停 / 恢复发布
- 切换发布后端
- 上传 / 查看登录态
- 配置 Telegram 机器人

---

## 命令行工具

服务器上没有图形界面，所以提供了一套 CLI：

```bash
# 登录态
.venv/bin/python twitbot-cli.py state import <文件>   # 导入
.venv/bin/python twitbot-cli.py state show            # 看概况（含账号名）
.venv/bin/python twitbot-cli.py state check           # 联网验证 + 识别账号
.venv/bin/python twitbot-cli.py state why             # 为什么服务器上不能登录

# 日常
.venv/bin/python twitbot-cli.py status                # 队列 / 配额 / 设置
.venv/bin/python twitbot-cli.py backend               # 后端可用性
.venv/bin/python twitbot-cli.py queue                 # 看队列
.venv/bin/python twitbot-cli.py post --text "内容"     # 投料
.venv/bin/python twitbot-cli.py start --no-tg         # 启动服务
```

---

## Telegram 机器人（可选）

1. 找 [@BotFather](https://t.me/BotFather) 发 `/newbot`，拿到 token
2. 填进 `.env` 的 `TG_TOKEN`，然后**去掉 systemd 里的 `--no-tg`**
3. 重启服务

```bash
nano .env
sed -i 's/ --no-tg//' /etc/systemd/system/twitbot.service
systemctl daemon-reload && systemctl restart twitbot
```

**⚠️ 一定要设白名单**，否则任何人搜到你的机器人就能往你的 X 发东西：

在控制台的「🔌 后端设置 → 管理员 ID」里添加自己的 Telegram user id。

机器人命令：`/start` `/menu` `/queue` `/status` `/pause` `/resume` `/backend` `/retry`

---

## 配置说明

`.env` 里常用的项：

| 项 | 说明 |
|---|---|
| `WEB_HOST` / `WEB_PORT` | 控制台监听地址。`0.0.0.0` 才能外部访问 |
| `WEB_PASSWORD` | 控制台密码。留空 = 自动生成 |
| `BROWSER_HEADLESS` | 服务器上必须是 `true` |
| `MODE` | `confirm` = 先审核再发（推荐）；`auto` = 收到就发 |
| `TG_TOKEN` | Telegram bot token |
| `MONTHLY_LIMIT` | 月度发帖上限 |
| `DEDUP_WINDOW` | 秒，同内容去重窗口 |

队列调度在控制台的「⚙️ 设置」里改：

- **队列发送开关**：关掉后内容只入库，必须手动逐条发
- **发送间隔**：两条推文之间至少隔多久

---

## 容器 / root 运行

以 root 或在 Docker 里跑时，Chrome 需要关闭沙箱。代码会自动检测
（Linux + root 时自动加 `--no-sandbox`），如果还起不来可以手动指定：

```bash
export TWITBOT_CHROME_ARGS="--no-sandbox --disable-dev-shm-usage"
```

需要走代理时：

```bash
export TWITBOT_CHROME_ARGS="--proxy-server=http://127.0.0.1:7890"
```

---

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

---

## 排障

| 现象 | 处理 |
|---|---|
| `state check` 报登录态失效 | 登录态过期了，在桌面重新收集再导入 |
| Chrome 起不来，报 sandbox | 见「容器 / root 运行」 |
| 报缺少 `.so` 库 | `sudo .venv/bin/python -m playwright install-deps chromium` |
| 控制台打不开 | 检查 `WEB_HOST=0.0.0.0`、防火墙、`WEB_PASSWORD` |
| 发布失败 | 看控制台任务详情里的错误 + `data/logs/fail_*.png` 截图 |
| 忘了密码 | `cat data/console-password` |

详细部署步骤见 [`deploy/LINUX-DEPLOY.md`](deploy/LINUX-DEPLOY.md)。

---

## 安全提醒

- **`storage_state.json` 等同于你的 X 账号凭据**，别提交、别外发
- 公网部署**务必**设 `WEB_PASSWORD`
- Telegram 机器人**务必**设白名单
- `.gitignore` 已排除 `.env`、`data/`、`storage_state.json`

---

## 免责声明

本项目通过模拟网页操作实现自动发帖，**可能违反 X 的服务条款**，账号存在被限制的风险。
请自行评估并承担后果。建议控制发布频率，避免批量互动行为。
