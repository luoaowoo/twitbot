# twitbot — Telegram / Web → X (Twitter) 自动发帖

转发内容给 Telegram 机器人，或直接在 Web 控制台投料 → 自动发布到 X。
**支持文字、图片、视频**；**两种发布方式可随时切换**：X 官方 API / 无头浏览器。
Telegram 机器人可以在网页上直接启停配置。**Windows 与 Linux 同一套代码。**

一条命令全启动：`run.bat`（Windows）或 `./run.sh`（Linux/macOS）。

---

## 目录

- [一、它是什么](#一它是什么)
- [二、两种发布方式（重点）](#二两种发布方式重点)
- [三、官方 API 要钱吗](#三官方-api-要钱吗)
- [四、快速开始](#四快速开始)
- [五、Web 控制台](#五web-控制台)
- [六、Telegram 用法](#六telegram-用法)
- [七、配置项](#七配置项)
- [八、自检与测试](#八自检与测试)
- [九、部署为常驻服务](#九部署为常驻服务)
- [十、架构与设计取舍](#十架构与设计取舍)
- [十一、FAQ / 排障](#十一faq--排障)

---

## 一、它是什么

一个"内容进来 → 排队 → 发到 X"的网关。三个部分解耦：

```
来源                       中间层                  出口
Telegram 机器人  ┐                                ┌ X 官方 API
                 ├─→  SQLite 队列  ─→  发布循环  ─┤
Web 控制台投料   ┘     （幂等/去重）              └ 无头浏览器
      ↑
  也在这里启停 Telegram 机器人、上传图片视频
```

解耦的好处：进程崩了、被限流、重启，待发内容都不丢；换个出口不用改来源。

三个组件默认跑在**同一个进程**里，所以控制台的操作（暂停、切后端、启停机器人）
都是**立刻生效**的，不需要重启。

## 二、两种发布方式（重点）

你要的"让用户自己选"，做成了 Web 控制台上的**两个卡片**，点一下切换，
切换前会**校验目标方式是否真的可用**，不可用会拒绝并告诉你缺什么。

| | **X 官方 API** | **无头浏览器** |
|---|---|---|
| 原理 | 调 `POST /2/tweets` | Playwright 驱动 Chromium 模拟真人操作 |
| 需要 | 开发者账号 + 四项凭据 | 一次登录态（`data/browser/storage_state.json`） |
| 花钱 | **要**（见下节） | 不花 API 钱，但要跑一个浏览器 |
| 额度 | 受 API 档位限制 | 基本只受账号本身行为限制 |
| 稳定性 | 高，官方接口 | 中，X 前端改版可能需要更新选择器 |
| 风险 | 无（官方通道） | 自动化行为可能被平台判定异常，**账号有一定风险** |
| 适合 | 长期稳定跑、量大 | 不想花 API 钱、量不大 |

**怎么选：**
- 认真长期做 → `x_api`
- 只是偶尔发几条、不想付月费 → `browser`
- 先跑通流程 → `browser`（不用申请开发者账号，登录一次就能用）

> 浏览器方式**不保证**永远有效：X 可能调整前端结构，也可能对自动化行为有额外验证。
> 代码里做了失败截图（`data/logs/fail_*.png`）方便你排障，但这是它的固有代价。

## 三、官方 API 要钱吗

**要。** X 自 2023 年起关掉免费发推，之后多次调价。当前形态（以
[developer.x.com](https://developer.x.com) 实际页面为准，这块改得很勤）：

| 档位 | 大致费用 | 发推额度 | 媒体上传 |
|---|---|---|---|
| Free | $0 | ~500 条/月，**仅文本** | ❌ 常被砍 |
| Basic | ~$200/月 | ~3,000 条/月 | 通常有 |
| Pro | ~$5,000/月 | 更高 | ✅ |

近期两个变化对你影响直接：X 已宣布转向**按量计费**替代固定月费，且**含链接的推文单价大幅上调**。
参考报道：[Gigazine](https://gigazine.net/gsc_news/en/20260209-x-api-pay-per-use)、
[TechJuice](https://www.techjuice.pk/x-announces-new-pay-per-use-api-pricing-to-attract-developers-back/)、
[链接涨价](https://gigazine.net/gsc_news/en/20260422-x-api-link-price-up/)。

**所以浏览器那条路的意义**：不用申请开发者账号、不付月费。代价是稳定性与账号风险。

---

## 四、快速开始

### 最简：一条命令全启动

**Windows** —— 双击 `run.bat`，或在终端：

```powershell
cd twitbot
.\run.bat
```

**Linux / macOS**：

```bash
cd twitbot
./run.sh
```

`run.sh` / `run.bat` 会自动完成**建虚拟环境 → 装依赖 → 装 Chromium → 生成 .env → 启动**，
并自动打开浏览器到控制台。首次运行会慢几分钟（下载依赖），之后就很快。

> 启动后**Web 控制台 + 发布循环 + Telegram 机器人**三个组件**一起跑**在同一个进程里。
> Telegram 没配 token 也不会报错，只是机器人的状态显示"未配置"——你在网页上填 token 点一下就起来了。

### 等价的手工方式

```powershell
cd twitbot
.\setup.ps1                       # 建 venv + 装依赖 + 装 Chromium + 生成 .env + 自检
.\.venv\Scripts\python.exe start.py
```

### 启动参数

| 命令 | 作用 |
|---|---|
| `python start.py` | **默认**：Web 控制台 + 发布循环 + Telegram 机器人 |
| `python start.py --no-tg` | 同上，但不拉 Telegram |
| `python start.py --no-web` | 只跑发布循环（+Telegram），无界面 |
| `python start.py --web-only` | 只起控制台（不发布、不接 Telegram） |
| `python start.py --worker-only` | 只起发布循环 |

### 启动后要做的两件事

1. 打开控制台 → 在**「发布后端」**卡片上选一种方式并让它变"可用"：
   - 想用**无头浏览器**：在那张卡片里填 X 账号密码点「用账号密码登录」，或点「登录 X」开窗口手工登录。
   - 想用**官方 API**：跑 `python setup_x.py` 配好四项凭据。
2. 在**「Telegram 机器人」**面板填 BotFather 给的 token，点「校验 Token」→「启动机器人」。

两个都配好后，往 Telegram 发东西就会自动发到 X 了。

## 五、Web 控制台

打开 `http://127.0.0.1:8787`。进入页面先输入控制台账号密码：

| 账号 | 密码 |
|---|---|
| `qwqcon` | `qwqcon_qwqcon` |
| `luoaowoo` | `luoaowoo_luoaowoo` |

两个账号的数据完全独立：各自使用 `data/accounts/<账号>/` 下的队列数据库、设置、
媒体、浏览器登录态、采集结果和数据日报。登录态放在签名 Cookie 中，密码不写数据库、
不写日志。`.env` 里 `WEB_TOKEN` 仍可作为旧版访问 token 使用（非空时需要携带
`?token=xxx`）。

| 区域 | 能做什么 |
|---|---|
| 顶部状态栏 | 当前后端、队列深度、本月已发/上限、暂停开关 |
| 发布后端 | 两张卡片显示可用性（绿=可用/红=原因），点选切换，「测试连接」真实鉴权 |
| 账号密码登录 | 在浏览器卡片里直接填 X 账号密码登录（**密码不落库、不写日志**） |
| Telegram 机器人 | 填 token、启停/重启机器人、配白名单、看机器人用户名与收料数 |
| 手工投料 | 输正文（实时折算 X 字数）+ **拖拽/点击上传图片视频** + 引用推文 ID |
| 任务列表 | id/状态/正文/后端/时间/失败原因，可按状态筛选，可重发或取消 |
| 运行期设置 | 前缀/后缀/月度上限/去重窗口/引用模式 |

### 发图片和视频

在「手工投料」区域的**虚线框里点一下选文件，或直接把图片/视频拖进去**。
上传后立刻显示缩略图、类型、大小、时长，并**当场校验是否符合 X 的限制**：

| 类型 | 上限 |
|---|---|
| 图片（JPG/PNG/GIF/WebP/BMP） | 5 MB（GIF 放宽到 15 MB） |
| 视频（MP4/MOV/MKV/WebM） | 512 MB，时长 ≤ 140 秒 |
| 单条最多图片数 | 4 张 |

超限会明确告诉你超了什么，**不会**等到发布时才失败。校验通过后点「提交到队列」即可。

> 视频时长优先用 `ffprobe` 读取；没装 ffmpeg 时会自动降级用内置的 MP4 解析，
> 仍读不出则标注"时长未知"并放行（由 X 侧最终判定）。**ffmpeg 不是必装依赖。**

**安全提醒**：控制台能直接发推，默认只绑 `127.0.0.1`。
要给别人访问务必设 `WEB_HOST=0.0.0.0` **并且**设一个强 `WEB_TOKEN`。

## 六、Telegram 用法

在控制台的「Telegram 机器人」面板里填 token 就能用，**不必**改 `.env`、**不必**重启。

| 动作 | 结果 |
|---|---|
| 转发文本/图片/视频/文件（可带说明） | 入队 → 发布 |
| 发含推文链接的文本 | 默认转引用转发 |
| `/status` | 队列统计 + 本月额度 |
| `/queue` | 最近 15 条待办/失败 |
| `/retry` | 重置所有失败任务 |

`MODE=confirm` 时会先回预览 + ✅/❌ 按钮，点了才发（建议先用这个）。

> ⚠️ 白名单留空 = **任何知道 bot 的人都能让它替你发推**。
> 控制台面板里的「允许的 user id / chat id」建议填上；`setup_tg.py` 也能自动探测。

## 七、配置项

改 `.env`（`setup.ps1` 会从 `.env.example` 生成）：

| 键 | 说明 |
|---|---|
| `TG_TOKEN` | BotFather 给的 token；**可留空**，在控制台面板里填也行 |
| `ALLOWED_USERS` / `ALLOWED_CHATS` | 白名单（强烈建议填）；控制台面板里也能改 |
| `X_CONSUMER_KEY/SECRET`、`X_ACCESS_TOKEN/SECRET` | `setup_x.py` 自动写入 |
| `BACKEND` | 默认发布方式：`x_api` / `browser`（控制台里可随时改，以控制台为准） |
| `BROWSER_HEADLESS` | `false` 会弹出浏览器窗口，方便首次登录/排障 |
| `BROWSER_SLOWMO` | 操作放慢毫秒数，排障可用 300 |
| `WEB_HOST` / `WEB_PORT` | 控制台监听地址与端口 |
| `WEB_TOKEN` | 非空则访问控制台需 token |
| `MODE` | `auto` 收到就发 / `confirm` 先确认 |
| `TWEET_PREFIX` / `TWEET_SUFFIX` | 每条自动加的前后缀 |
| `QUOTE_MODE` | 含推文链接时是否转引用转发 |
| `DEDUP_WINDOW` | 秒，同内容去重窗口 |
| `MONTHLY_LIMIT` | 月度发推软上限，到顶自动暂停出队 |
| `MAX_ATTEMPTS` | 单条最多重试次数 |
| `POLL_SECONDS` | 队列轮询间隔 |

## 八、自检与测试

```powershell
.\.venv\Scripts\python.exe smoke.py                 # 全量集成自检（99 项）
.\.venv\Scripts\python.exe verify_all.py            # 端到端（含控制台 API/媒体/Telegram）
.\.venv\Scripts\python.exe -m pytest tests\ -v      # 分模块测试（350+ 项）
.\.venv\Scripts\python.exe bot.py --status          # 看队列与配额
```

> `selftest.py` 已失效（退出码 1），别当通过依据用 —— 用上面的 `smoke.py`。

## 九、部署为常驻服务

### Windows（开机自启 + 崩溃重启）

```powershell
.\deploy\install-task.ps1                 # 默认 Web+发布循环
.\deploy\install-task.ps1 -WithTelegram   # 再加 Telegram
Start-ScheduledTask -TaskName TwitBot
```

### Linux（systemd）

```bash
sudo cp deploy/twitbot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now twitbot
journalctl -u twitbot -f
```

服务器上跑浏览器方式需要补系统库：`sudo .venv/bin/python -m playwright install-deps chromium`。

详细运维命令与排障见 **`deploy/OPS.md`**。

## 十、架构与设计取舍

```
core/
  config.py          静态配置与路径
  textutil.py        文案合成 + X 字数折算（CJK/emoji 计 2、URL 计 23）
  queue.py           SQLite 队列：幂等入队、原子出队、状态机
  settings.py        运行期设置（后端选择/暂停/前缀等），Web 可改
  lock.py            跨平台单实例锁
  pipeline.py        发布循环（后端无关）
  notifier.py        回执通知（Telegram / 日志）
  backends/
    base.py          后端契约（Job / PublishResult / Protocol）
    registry.py      后端注册表（惰性加载，单后端故障不影响其它）
    x_api.py         X 官方 API 实现
    browser.py       无头浏览器实现
web/                 控制台（FastAPI + 单文件前端，无构建步骤）
bot.py               Telegram 收料
start.py             统一启动器
```

**每条设计对应的真实故障模式：**

| 设计 | 解决什么 |
|---|---|
| Telegram 用**长轮询** | 不需要公网 IP / 域名 / 证书，家用机和服务器都能直接跑 |
| 中间隔一层 **SQLite 队列** | 崩溃/重启/限流时内容不丢 |
| `UNIQUE(tg_chat_id, tg_msg_id)` | 长轮询会重投，靠数据库约束保证**不重复发推** |
| 内容 hash + 时间窗去重 | 同一素材被反复转发时自动丢弃 |
| `claim_next()` 事务内先占位再返回 | 防并发双取；也防反复限流把任务**卡死** |
| 后端不可用/配额满时**回退 attempts** | 一份坏配置不该把任务自己的重试次数耗光 |
| **单实例锁** | 开两个实例会互相抢队列导致重复发推 |
| 429 读 `x-rate-limit-reset` | 精确睡到额度恢复，而不是盲目退避 |
| 后端契约规定"绝不抛异常" + pipeline 再兜一层 | 后端 bug 不能崩掉主循环 |
| 控制台切换后端前校验可用性 | 否则用户切到不可用后端，任务会静默堆积 |

## 十一、FAQ / 排障

| 现象 | 处理 |
|---|---|
| 启动报「已有实例在运行」 | 残留锁，确认无进程后删 `data/bot.lock`（会显示持锁者） |
| X API 报 401 | App 权限不是 Read and Write，或 Token 在改权限前生成 → 重新生成 Access Token |
| X API 报 403 + duplicate | 内容与近期推文完全一样，改文案 |
| 浏览器方式说登录态失效 | 重跑 `tools/browser_login.py` |
| 浏览器方式失败 | 看任务详情 `error` + `data/logs/fail_*.png` 截图 |
| 任务一直 pending | 看控制台是否「暂停」、后端是否可用、月度配额是否已满 |
| 中文日志乱码 | 代码已统一 UTF-8；仍乱码则终端 `chcp 65001` |
| 换机器后图片找不到 | 媒体存 `data/media/`，库里存相对名 —— 搬走整个 `data/` |

---

**参考**：[X 按量计费](https://gigazine.net/gsc_news/en/20260209-x-api-pay-per-use)、
[含链接推文涨价](https://gigazine.net/gsc_news/en/20260422-x-api-link-price-up/)。价格以官方页面为准。
