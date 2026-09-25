# twitbot 服务器版 —— Linux 部署指南

> 这个版本专为**无桌面的 Linux 服务器**准备。
> 与桌面版的唯一区别：**登录不能在服务器上做**，必须先在别处登录再把登录态搬过来。

---

## 一、先搞清楚：为什么不能在服务器登录

浏览器登录需要**弹出一个真实窗口**让你输密码、过验证码。
服务器没有显示器，Chrome 起不来 —— 这是物理限制，不是 bug。

所以流程是：

```
① 在有桌面的机器上登录一次（Windows / macOS / 带桌面的 Linux 都行）
        ↓
② 拿到 data/browser/storage_state.json
        ↓
③ 传到服务器（命令行或 Web 控制台）
        ↓
④ 服务器无头发帖，正常跑
```

**关键前提**：登录态会绑定浏览器指纹。从 Windows 搬到 Linux 有可能失效，
所以搬过去后**务必先跑一次 state check** 确认能用。

---

## 二、安装

```bash
sudo mkdir -p /opt/twitbot
sudo chown $USER /opt/twitbot
# 把本项目文件拷进去（不含 .venv / data）

cd /opt/twitbot

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# 装 Chromium（发布用，无头）
.venv/bin/python -m playwright install chromium

# 无桌面服务器缺的系统库（官方脚本一把装齐）
sudo .venv/bin/python -m playwright install-deps chromium
```

---

## 三、登录态：从别处搬过来

### 步骤 1：在**有桌面的机器**上登录

```bash
python tools/browser_login_chrome.py
```

会弹出 Chrome 窗口，正常登录 X。成功后生成
`data/browser/storage_state.json`。

### 步骤 2：传到服务器

```bash
scp data/browser/storage_state.json user@your-server:/tmp/state.json
```

### 步骤 3：在服务器上导入

```bash
cd /opt/twitbot
.venv/bin/python twitbot-cli.py state import /tmp/state.json
```

会校验格式与 auth_token，通过才写入。

### 步骤 4：**务必**验证能用

```bash
.venv/bin/python twitbot-cli.py state check
```

看到 `连通性：通过` 才算成功。**这一步不能省** —— 跨机器搬的登录态
有可能因指纹变化而失效，提前知道比上线后才发现好。

---

## 四、另一种导入方式：Web 控制台

```bash
.venv/bin/python twitbot-cli.py start --no-tg --host 0.0.0.0 --port 8787
```

打开 `http://<服务器IP>:8787`，用「上传登录态」功能传文件。

> 绑 0.0.0.0 前请先设 WEB_TOKEN（见第六节），否则任何人都能访问。

---

## 五、命令行工具速查

```bash
.venv/bin/python twitbot-cli.py state import <file>   # 导入登录态
.venv/bin/python twitbot-cli.py state show            # 看概况（不含内容）
.venv/bin/python twitbot-cli.py state check           # 联网验证
.venv/bin/python twitbot-cli.py status                # 队列 / 配额 / 设置
.venv/bin/python twitbot-cli.py backend               # 看后端可用性
.venv/bin/python twitbot-cli.py post --text "内容"     # 投料（进待确认）
.venv/bin/python twitbot-cli.py queue                 # 看队列
.venv/bin/python twitbot-cli.py start --no-tg         # 启动服务
```

---

## 六、配置 .env

```bash
cp .env.example .env
```

服务器上至少要改这几项：

```ini
BACKEND=browser
BROWSER_HEADLESS=true
WEB_HOST=0.0.0.0
WEB_PORT=8787
WEB_TOKEN=换成一段随机字符串     # 公网部署必须设
MODE=confirm
```

---

## 七、容器 / root 运行的额外参数

以 root 或 Docker 里跑时，Chrome 需要关闭沙箱。代码已自动检测
（Linux + root 会自己加 --no-sandbox），如果还起不来，可手动指定：

```bash
export TWITBOT_CHROME_ARGS="--no-sandbox --disable-dev-shm-usage"
```

需要走代理时：

```bash
export TWITBOT_CHROME_ARGS="--proxy-server=http://127.0.0.1:7890"
```

---

## 八、做成常驻服务

```bash
sudo cp deploy/twitbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now twitbot
journalctl -u twitbot -f          # 看日志
```

---

## 九、排障

| 现象 | 原因 / 处理 |
|---|---|
| state check 报「登录态失效」 | 跨机器搬导致指纹变了。重新登录再搬，或改走官方 API |
| Chrome 起不来，报 sandbox 相关 | 见第七节：加 --no-sandbox |
| 报缺少 .so 库 | sudo .venv/bin/python -m playwright install-deps chromium |
| state import 说「没有 auth_token」 | 导出时没登录成功。回桌面机器重登 |
| 发布报「登录态失效」 | 登录态过期，重新搬 |
| 无法访问 x.com | 服务器网络出口问题，检查代理/防火墙 |
| 控制台打不开 | 检查 WEB_HOST、防火墙、WEB_TOKEN |

---

## 十、桌面版 vs 服务器版 差异

| | 桌面版 | 服务器版 |
|---|---|---|
| 弹窗登录 | 可以 | 不行（无显示器） |
| 命令行导入登录态 | — | 本版新增 |
| Web 上传登录态 | — | 本版新增 |
| 命令行投料/看队列 | — | 本版新增 |
| root/容器 --no-sandbox | — | 本版自动处理 |
| 无图形界面友好提示 | — | 本版新增 |
| 代理/自定义 Chrome 参数 | — | TWITBOT_CHROME_ARGS |

---

## 十一、控制台密码门（服务器版专属）

服务器版给控制台加了一道**密码门**：访问任何页面都会先要求输密码，
登录后凭会话 Cookie 访问（密码不会出现在 URL 里）。

### 默认密码

```
<首次启动自动生成，见 data/console-password>
```

### 改密码

编辑 `.env`：

```ini
WEB_PASSWORD=你的新密码
```

或者用环境变量：

```bash
export WEB_PASSWORD='你的新密码'
```

### 关闭密码门

```ini
WEB_PASSWORD=-
```

> ⚠ 仅供内网或本地测试。公网部署**务必保留密码门**，
> 因为控制台能直接发推、能上传登录态。

### 会话有效期

登录后 12 小时内有效。想立刻失效就点控制台的登出，或重启服务。

### 与 WEB_TOKEN 的关系

两者**可以并存**，是两道独立的门：

| | 用途 | 缺点 |
|---|---|---|
| `WEB_PASSWORD` | 登录页 + 会话 Cookie | — |
| `WEB_TOKEN` | `?token=xxx` 或 Bearer | token 会出现在 URL / 浏览器历史里 |

服务器版**推荐用 WEB_PASSWORD**。`WEB_TOKEN` 保留兼容。

---

## 十二、Windows 一键收集登录态

服务器上登不了（见第一节），所以要在 Windows 上收集。本版提供了一个
**一键工具**：

```
tools\一键收集登录态.bat
```

**双击它即可**，它会自动：

1. 检查 Python 环境（版本不够会提示）
2. 检查依赖（缺了自动安装 playwright / websockets）
3. 弹出真实 Chrome 让你正常登录 X
4. 把生成的 `storage_state.json` **复制到桌面**

然后用 `scp` 传到服务器导入即可（第三节的步骤 3–4）。

### 前提

- 装了 Python 3.10+
- 装了 Google Chrome
- 能访问 x.com（需要代理就开着）

### 为什么用这个而不是服务器上登

因为登录必须弹出真实窗口。Windows 上有桌面，能弹；
服务器上没有 —— 这是唯一的区别。

### 安全提醒

`storage_state.json` **等同于你的 X 账号凭据**。别发给别人、
别传到公开的地方。用完可以删掉桌面那份（服务器上还要留一份）。
