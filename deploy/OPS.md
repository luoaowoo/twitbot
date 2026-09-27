# 日常运维命令

## 一、跑起来

**最省事：一条命令全启动**（Web + 发布循环 + Telegram，同进程）

```powershell
.\run.bat                            # Windows：建环境→装依赖→启动→自动开浏览器
```
```bash
./run.sh                             # Linux/macOS
```

手工方式与各组合：

```powershell
# 【默认】Web 控制台 + 发布循环 + Telegram 机器人
.\.venv\Scripts\python.exe start.py

# 不拉 Telegram
.\.venv\Scripts\python.exe start.py --no-tg

# 只起 Web 控制台（不发布、不接 Telegram，适合先看界面）
.\.venv\Scripts\python.exe start.py --web-only

# 只起发布循环（无界面，服务器上跑；Telegram 仍会拉起）
.\.venv\Scripts\python.exe start.py --no-web

# 只起发布循环、不要 Telegram
.\.venv\Scripts\python.exe start.py --worker-only --no-tg
```

浏览器打开 `http://127.0.0.1:8787`。`.env` 里 `WEB_TOKEN` 非空时需带 token：
`http://127.0.0.1:8787/?token=你的token`。

> `start.py` 用单实例锁，重复启动会被拒绝并提示当前持有者。
> **Telegram 没配 token 不会导致启动失败** —— 日志里提示一下，你可以在网页上补。

## 二、两种发布后端

| 后端 | 需要什么 | 特点 |
|---|---|---|
| `x_api` | X API 四项凭据 | 稳定合规，受 API 额度限制 |
| `browser` | 一次登录态（`data/browser/storage_state.json`） | 不耗 API 额度，模拟真人操作；前端改版可能失效 |

切换方式：**Web 控制台点选**（切换前会自动校验目标后端可用性，不可用会拒绝并说明原因），
或改 `.env` 的 `BACKEND=` 后重启。

浏览器后端登录有两条路：

1. **网页上填账号密码** —— 控制台「用账号密码登录 X」，密码不落库、不写日志。
   若 X 要求二次验证码，流程会停下并提示你填入，输入框会自动出现。
2. **有头窗口手工登录**（推荐，最稳）：
   ```powershell
   .\.venv\Scripts\python.exe tools\browser_login.py
   ```

登录态存在 `data/browser/storage_state.json`。**换机器把它一起搬走**即可免重登。

## 三、发图片和视频

在控制台「手工投料」区**拖拽或点击选择**文件即可。上限：

| 类型 | 上限 |
|---|---|
| 图片 | 5 MB（GIF 15 MB） |
| 视频 | 512 MB / 140 秒 |
| 单条图片数 | 4 张 |

超限会在上传时就明确拒绝并说明原因，不会等到发布失败。
视频时长优先用 `ffprobe`；**没装 ffmpeg 也能跑**（降级用内置 MP4 解析，读不出则标"时长未知"并放行）。

素材落在 `data/media/`，库里存相对名 —— 备份整个 `data/` 即可。

## 四、Windows

```powershell
# 看队列与配额
.\.venv\Scripts\python.exe bot.py --status

# 后台跑（关掉窗口也不停）
Start-Process -FilePath ".\.venv\Scripts\python.exe" -ArgumentList "start.py" `
  -WorkingDirectory "$PWD" -WindowStyle Hidden

# 注册为开机自启服务（管理员）
.\deploy\install-task.ps1                    # 默认已是全量启动
Start-ScheduledTask -TaskName TwitBot
Get-ScheduledTaskInfo -TaskName TwitBot
```

> 计划任务以 S4U 跑在无桌面会话，浏览器**无头**可用，但首次登录必须先在有桌面的会话里做一次。

## 五、Linux

```bash
# 前台
.venv/bin/python start.py

# 临时后台
nohup .venv/bin/python start.py >> bot.log 2>&1 &

# systemd 常驻
sudo cp deploy/twitbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now twitbot
journalctl -u twitbot -f
sudo systemctl restart twitbot
sudo systemctl status twitbot
```

**服务器上跑浏览器后端**还需要系统依赖库：

```bash
sudo .venv/bin/python -m playwright install-deps chromium
```

无桌面服务器要确保 `.env` 里 `BROWSER_HEADLESS=true`。

## 六、自检

```powershell
# 全量集成自检（配置/队列/幂等/去重/状态机/.env/跨进程锁/后端契约）
.\.venv\Scripts\python.exe smoke.py

# 端到端验证（后端契约 + 控制台所有 API + v2 媒体/Telegram + 启动器装配 + 干跑只读）
.\.venv\Scripts\python.exe verify_all.py

# 分模块测试
.\.venv\Scripts\python.exe -m pytest tests\ -v
```

> `selftest.py` 目前是坏的（用已不存在的注释标记去切 `bot.py` 源码 → `IndexError`，退出码 1）。
> 别拿它当通过依据，用 `smoke.py` 代替。除非要修它，否则不用管。

### 出问题先看日志

`start.py` 会把日志**同时**写进控制台和 `data/logs/`：

| 文件 | 内容 |
|---|---|
| `data/logs/twitbot.log` | 全部运行日志（滚动，单文件 2MB，留 3 份） |
| `data/logs/crash.log` | 段错误等硬崩溃的调用栈（faulthandler） |

未捕获的异常会带完整 traceback 写进 `twitbot.log`。**服务莫名退出时先看这两个文件**，
不要在没有任何现场的情况下猜测原因。

## 七、常见故障

| 现象 | 原因 / 处理 |
|---|---|
| 启动报「已有实例在运行」 | 残留锁。确认没别的进程后删 `data/bot.lock`（会显示持锁者 pid/host） |
| 启动时 Telegram 提示未启动 | token 没配。进控制台「Telegram 机器人」面板填 token 点「启动机器人」 |
| Telegram token 无效 | 到 @BotFather 重新拿；控制台点「校验 Token」会明确告诉你 |
| 控制台打不开 | 检查 `WEB_PORT` 是否被占；确认进程还活着；`WEB_HOST` 默认只绑本地 |
| 切换后端被拒 | 目标后端不可用，控制台会显示原因（缺凭据 / 未登录） |
| 账号密码登录说要验证码 | 正常，X 会二次验证。在控制台出现的验证码框里填入后点「提交验证码并继续」 |
| 账号密码登录报人机验证 | X 判定异常。改用「登录 X」在有头窗口里手工登录 |
| 上传图片/视频被拒 | 看提示的超限原因（大小/时长）；单个文件 > 512MB 一律拒 |
| 上传报 503 core/media.py 不可用 | 项目文件缺失或被改坏，检查 `core/media.py` 是否存在 |
| 视频时长显示"未知" | 没装 ffprobe。不影响发送，X 侧会最终判定 |
| X API 报 401 | App 权限不是 Read and Write，或 Token 在改权限前生成。重生成 Access Token |
| X API 报 403 + duplicate | 内容与近期推文完全一样，X 直接拒。改文案 |
| X API 报 403 其他 | 权限或配额问题（免费层常无 media/upload 权限） |
| 浏览器后端说登录态失效 | 重跑 `tools/browser_login.py` 或在控制台重新登录 |
| 浏览器后端发布失败 | 看控制台任务详情里的 `error`，以及 `data/logs/fail_*.png` 截图 |
| 浏览器相关进程残留 | `taskkill /F /IM chrome.exe`（Windows）或 `pkill -f chromium`（Linux） |
| 中文日志乱码 | 代码已统一切 UTF-8；仍乱码则终端执行 `chcp 65001` |
| 任务一直 pending | 控制台看是否处于「暂停」；或看后端是否可用；或月度配额已满 |
| 任务大量 failed | 控制台逐个看重发；`--status` 看错误分布；必要时 `/retry` 或在控制台重置 |
| 图片找不到 | 媒体存 `data/media/`，库里存相对名 —— 搬走整个 `data/` 目录 |

## 八、数据与备份

```
data/
  queue.db                       队列与设置（SQLite，WAL 模式；含 Telegram 配置）
  media/                         素材（库里存相对名）
  browser/storage_state.json     浏览器登录态（=账号凭据，注意保密）
  logs/                          失败截图等
  bot.lock                       单实例锁
```

备份：整个 `data/` 目录拷走即可。**`storage_state.json` 等同于账号登录凭据，别外传。**

> X 账号密码**不会**存进 `data/` 的任何地方 —— 只在登录那一次请求的内存里用过即弃。
