#!/usr/bin/env python3
"""端到端集成验证 —— 所有 agent 交付物落地后跑这个。

覆盖"模块各自单测通过"之外的**跨模块**风险：
  1. 所有后端能被注册表加载并满足契约（不抛异常、返回正确类型）
  2. pipeline 在真实 registry + 真实后端上能走完 tick（不联网，用不可用后端验证容错）
  3. Web 控制台能启动、API 全通、切后端校验生效、投料端到端入队
  4. start.py 的装配能跑起来（--web-only 模式）
  5. 干跑不改变数据库状态
  6. v2：媒体上限/判定/穿越防护、控制台 tg/media/密码登录接口、全量启动

用法：
    python verify_all.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
TMP = Path(tempfile.mkdtemp(prefix="twitbot-e2e-"))

PASS = FAIL = 0
NOTES: list[str] = []


def check(name: str, got, want=True) -> None:
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}\n      got ={got!r}\n      want={want!r}")


def note(msg: str) -> None:
    NOTES.append(msg)
    print(f"      · {msg}")


def env_for(**extra) -> dict:
    e = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "DATA_DIR": str(TMP),
        # 探针脚本写在临时目录里，其所在目录（而非 cwd）会被 Python 加入 sys.path；
        # 必须显式给 PYTHONPATH，否则 `import core` 找不到包。
        "PYTHONPATH": str(ROOT),
        **extra,
    }
    e.pop("TG_TOKEN", None)   # 确保不误拉 Telegram
    # 清空凭据：验证脚本要断言「未配置凭据 → 后端不可用」，不能被本机真实凭据干扰
    for _k in ("X_CONSUMER_KEY", "X_CONSUMER_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"):
        e[_k] = ""
    return e


def run(args: list[str], timeout: int = 120, **envx) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, *args], cwd=str(ROOT), env=env_for(**envx),
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def http(path: str, method: str = "GET", body: dict | None = None, port: int = 8899,
         token: str = "") -> tuple[int, str]:
    url = f"http://127.0.0.1:{port}{path}"
    if token:
        url += ("&" if "?" in url else "?") + f"token={token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


# ═══════════════════════════════════════════════════════════
print("=" * 66)
print(" 1. 静态检查：编译所有模块")
print("=" * 66)
targets = ["bot.py", "start.py", "core/pipeline.py", "core/notifier.py",
           "core/backends/x_api.py", "core/backends/browser.py",
           "web/server.py", "tools/browser_login.py"]
existing = [t for t in targets if (ROOT / t).exists()]
missing = [t for t in targets if not (ROOT / t).exists()]
r = run(["-m", "py_compile", *existing])
check(f"编译通过（{len(existing)} 个文件）", r.returncode, 0)
if r.returncode != 0:
    print(r.stderr[-1500:])
for m in missing:
    note(f"缺失（该 agent 未交付）: {m}")

# ═══════════════════════════════════════════════════════════
print()
print("=" * 66)
print(" 2. 后端契约一致性（真实 registry）")
print("=" * 66)
probe = TMP / "probe_backends.py"
probe.write_text(
    "import json, sys\n"
    "from core import backends\n"
    "from core.backends.base import PublishBackend, Job\n"
    "out = {}\n"
    "for d in backends.describe():\n"
    "    out[d['name']] = d\n"
    "for name, d in out.items():\n"
    "    if not d['loaded']:\n"
    "        continue\n"
    "    be = backends.get(name)\n"
    "    d['is_protocol'] = isinstance(be, PublishBackend)\n"
    "    d['has_name'] = getattr(be, 'name', None)\n"
    "    d['has_label'] = getattr(be, 'label', None)\n"
    "    for m in ('available', 'publish', 'verify', 'login'):\n"
    "        d['has_' + m] = callable(getattr(be, m, None))\n"
    "    try:\n"
    "        v = be.verify()\n"
    "        d['verify_returns_tuple'] = isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], bool)\n"
    "    except BaseException as e:\n"
    "        d['verify_returns_tuple'] = False\n"
    "        d['verify_raised'] = f'{type(e).__name__}: {e}'\n"
    "print('JSON_START' + json.dumps(out, ensure_ascii=False) + 'JSON_END')\n",
    encoding="utf-8")
r = run([str(probe)], timeout=180)
if "JSON_START" in r.stdout:
    payload = json.loads(r.stdout.split("JSON_START")[1].split("JSON_END")[0])
    for name, d in payload.items():
        if not d["loaded"]:
            note(f"{name} 未加载（原因：{d['reason'][:80]}）")
            continue
        check(f"{name}: 符合 PublishBackend 协议", d.get("is_protocol"), True)
        check(f"{name}: name/label 齐备", bool(d.get("has_name") and d.get("has_label")), True)
        check(f"{name}: 四个契约方法齐备",
              all(d.get(f"has_{m}") for m in ("available", "publish", "verify", "login")), True)
        check(f"{name}: verify() 返回 (bool, str) 不抛异常",
              d.get("verify_returns_tuple"), True)
        if d.get("verify_raised"):
            note(f"verify 抛了异常: {d['verify_raised'][:120]}")
    if not payload:
        note("无后端被描述（registry 为空？）")
else:
    check("后端契约探测脚本可运行", r.returncode, 0)
    print(r.stdout[-800:])
    print(r.stderr[-1500:])

# ═══════════════════════════════════════════════════════════
print()
print("=" * 66)
print(" 3. pipeline 在真实 registry 上的容错（后端不可用 → 回退 attempts）")
print("=" * 66)
pl_probe = TMP / "probe_pipeline.py"
pl_probe.write_text(
    "import asyncio, json\n"
    "from core import queue, settings, config\n"
    "from core.pipeline import Pipeline\n"
    "queue.init_db()\n"
    "settings.all_settings()\n"
    "settings.set_many({'paused': '0', 'backend': 'x_api'})\n"
    "with queue.db() as con:\n"
    "    con.execute('DELETE FROM jobs')\n"
    "jid = queue.enqueue(kind='text', tg_chat_id=1, tg_msg_id=1, raw_text='e2e', content_hash='e2e')\n"
    "before = dict(queue.get(jid))\n"
    "p = Pipeline(sleeper=lambda s: asyncio.sleep(0))   # 不真等\n"
    "asyncio.run(p.tick())\n"
    "after = dict(queue.get(jid))\n"
    "print(json.dumps({\n"
    "  'before_status': before['status'], 'before_attempts': before['attempts'],\n"
    "  'after_status': after['status'], 'after_attempts': after['attempts'],\n"
    "  'error': after['error'],\n"
    "  'backend_available': settings.current_backend() in [d['name'] for d in __import__('core.backends', fromlist=['x']).describe() if d['available']],\n"
    "}, ensure_ascii=False))\n",
    encoding="utf-8")
r = run([str(pl_probe)], timeout=180)
if r.returncode == 0 and r.stdout.strip():
    try:
        d = json.loads(r.stdout.strip().splitlines()[-1])
        check("入队后状态为 pending", d["before_status"], "pending")
        # 未配凭据 → 后端不可用 → 任务应回到 pending 且 attempts 回退为 0
        check("后端不可用时任务回到 pending（不消耗重试）", d["after_status"], "pending")
        check("attempts 被回退（未被坏配置耗光）", d["after_attempts"], 0)
        check("记录了可读原因", bool(d["error"]), True)
        note(f"error = {d['error'][:100]}")
    except Exception as e:
        check("pipeline 探测输出可解析", False, True)
        print(f"      {e}\n      stdout={r.stdout[-500:]}")
else:
    check("pipeline 探测脚本可运行", r.returncode, 0)
    print(r.stdout[-500:])
    print(r.stderr[-1500:])

# ═══════════════════════════════════════════════════════════
print()
print("=" * 66)
print(" 4. Web 控制台：启动 + API 全通 + 切后端校验")
print("=" * 66)
if not (ROOT / "web" / "server.py").exists():
    note("web/server.py 未交付，跳过")
else:
    port = 8899
    env = env_for(WEB_PORT=str(port), WEB_HOST="127.0.0.1", DATA_DIR=str(TMP / "webdata"))
    proc = subprocess.Popen(
        [PY, "-c",
         "from web.server import create_app; import uvicorn; from core import config; "
         "uvicorn.run(create_app(), host=config.WEB_HOST, port=config.WEB_PORT, log_level='warning')"],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace")
    try:
        up = False
        for _ in range(40):
            time.sleep(0.5)
            code, _ = http("/api/status", port=port)
            if code == 200:
                up = True
                break
        check("控制台能启动并响应 /api/status", up, True)
        if up:
            code, body = http("/api/status", port=port)
            check("GET /api/status 200", code, 200)
            try:
                st = json.loads(body)
                for k in ("queue", "backend", "backends", "paused"):
                    check(f"/api/status 含字段 {k}", k in st, True)
            except Exception as e:
                check("/api/status 返回合法 JSON", False, True)
                print(f"      {e}")

            code, body = http("/api/backends", port=port)
            check("GET /api/backends 200", code, 200)

            code, _ = http("/", port=port)
            check("GET / 返回页面（200）", code, 200)

            # 投料
            code, body = http("/api/jobs", "POST", {"text": "e2e 手工投料测试"}, port=port)
            check("POST /api/jobs 投料成功（200/201）", code in (200, 201), True)
            job_id = None
            try:
                job_id = json.loads(body).get("id") or json.loads(body).get("job_id")
            except Exception:
                pass
            if job_id:
                note(f"新建任务 id={job_id}")
            else:
                note(f"投料响应（未能取到 id）: {body[:160]}")

            code, body = http("/api/jobs?limit=10", port=port)
            check("GET /api/jobs 200", code, 200)

            # 切到不可用后端必须被拒（这是关键校验）
            code, body = http("/api/backend", "POST", {"backend": "x_api"}, port=port)
            note(f"切到 x_api（未配凭据）→ HTTP {code}: {body[:120]}")
            check("切到不可用后端被拒绝（400）或已可用（200）", code in (200, 400), True)
            if code == 400:
                check("拒绝时给出原因", "error" in body or "原因" in body, True)

            code, body = http("/api/backend", "POST", {"backend": "不存在的后端"}, port=port)
            check("切到不存在的后端被拒（400/422）", code in (400, 422), True)

            # 暂停
            code, _ = http("/api/pause", "POST", {"paused": True}, port=port)
            check("POST /api/pause 200", code, 200)
            code, body = http("/api/status", port=port)
            try:
                check("暂停状态已生效", json.loads(body)["paused"], True)
            except Exception:
                check("暂停状态可读取", False, True)
            http("/api/pause", "POST", {"paused": False}, port=port)

            # 设置
            code, _ = http("/api/settings", "POST", {"tweet_prefix": "[E2E] "}, port=port)
            check("POST /api/settings 200", code, 200)

            # 任务操作
            if job_id:
                code, _ = http(f"/api/jobs/{job_id}/cancel", "POST", {}, port=port)
                check("POST /api/jobs/{id}/cancel 200", code, 200)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = proc.stderr.read() if proc.stderr else ""
        if err and "Traceback" in err:
            print("      [控制台 stderr 有异常]")
            print("\n".join(err.splitlines()[-12:]))

# ═══════════════════════════════════════════════════════════
print()
print("=" * 66)
print(" 5. start.py 装配（--web-only 能起来）")
print("=" * 66)
if not (ROOT / "start.py").exists():
    note("start.py 未交付，跳过")
else:
    port = 8901
    env = env_for(WEB_PORT=str(port), WEB_HOST="127.0.0.1", DATA_DIR=str(TMP / "startdata"))
    proc = subprocess.Popen([PY, "start.py", "--web-only"], cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    try:
        up = False
        for _ in range(40):
            time.sleep(0.5)
            code, _ = http("/api/status", port=port)
            if code == 200:
                up = True
                break
        check("start.py --web-only 起得来", up, True)
        if not up:
            proc.terminate()
            out = proc.stdout.read() if proc.stdout else ""
            print("      [start.py 输出]")
            print("\n".join(out.splitlines()[-15:]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

# ═══════════════════════════════════════════════════════════
print()
print("=" * 66)
print(" 6. 干跑不改变数据库状态（--dry-run 只读）")
print("=" * 66)
if (ROOT / "bot.py").exists():
    dr = TMP / "drydata"
    dr.mkdir(parents=True, exist_ok=True)
    setup = run(["-c",
                 "from core import queue; queue.init_db(); "
                 "queue.enqueue(kind='text', tg_chat_id=7, tg_msg_id=7, raw_text='dry', content_hash='d')"],
                DATA_DIR=str(dr))
    check("干跑前置：任务已入队", setup.returncode, 0)
    before = run(["-c",
                  "from core import queue; r=queue.get(1); print(r['status'], r['attempts'])"],
                 DATA_DIR=str(dr))
    # 干跑用超时杀掉（它是长驻进程）；随后检查状态未变
    try:
        subprocess.run([PY, "bot.py", "--dry-run"], cwd=str(ROOT),
                       env=env_for(DATA_DIR=str(dr), TG_TOKEN="123:FAKE"),
                       timeout=8, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        pass
    after = run(["-c",
                 "from core import queue; r=queue.get(1); print(r['status'], r['attempts'])"],
                DATA_DIR=str(dr))
    check("干跑前后任务状态不变（pending 0）", after.stdout.strip(), "pending 0")
    note(f"before={before.stdout.strip()!r} after={after.stdout.strip()!r}")

# ═══════════════════════════════════════════════════════════
print()
print("=" * 66)
print(" 7. 全量 smoke.py（项目自带兜底自检）")
print("=" * 66)
if (ROOT / "smoke.py").exists():
    r = run(["smoke.py"], timeout=300)
    tail = [ln for ln in r.stdout.splitlines() if ln.startswith(("PASS", "FAIL"))]
    p = sum(1 for ln in tail if ln.startswith("PASS"))
    f = sum(1 for ln in tail if ln.startswith("FAIL"))
    check("smoke.py 无 FAIL", f, 0)
    note(f"smoke.py: {p} PASS / {f} FAIL")
    if f:
        print(r.stdout[-2000:])

# ═══════════════════════════════════════════════════════════
print()
print("=" * 66)
print(" 8. v2 新功能：媒体校验 / Telegram 接口 / 密码登录 / 全量启动")
print("=" * 66)

# ── 8.1 core.media 契约与安全 ──
probe_media = TMP / "probe_media.py"
probe_media.write_text(
    "import json, sys\n"
    "from core import media as m\n"
    "out = {}\n"
    "out['limits'] = [m.IMAGE_MAX_BYTES, m.GIF_MAX_BYTES, m.VIDEO_MAX_BYTES, m.VIDEO_MAX_SECONDS]\n"
    "out['kinds'] = {e: m.detect_kind('x' + e) for e in ('.jpg', '.mp4', '.pdf', '.png')}\n"
    "out['probe_garbage'] = list(m.probe_video(__import__('pathlib').Path(sys.argv[1])))\n"
    "out['validate_missing'] = list(m.validate(__import__('pathlib').Path(sys.argv[2])))\n"
    "print(json.dumps(out))\n",
    encoding="utf-8")
garbage = TMP / "garbage.mp4"
garbage.write_bytes(b"\x00" * 64)
r = run([str(probe_media), str(garbage), str(TMP / "nope.jpg")])
check("core.media 探针可运行", r.returncode, 0)
if r.returncode == 0:
    d = json.loads(r.stdout.strip().splitlines()[-1])
    check("媒体上限符合 X 平台", d["limits"], [5 * 1024 * 1024, 15 * 1024 * 1024,
                                              512 * 1024 * 1024, 140])
    check("媒体类型判定正确", d["kinds"],
          {".jpg": "photo", ".mp4": "video", ".pdf": "document", ".png": "photo"})
    check("畸形视频不抛异常（返回 0.0）", d["probe_garbage"][0], 0.0)
    check("不存在的文件被拒且给原因", d["validate_missing"][0], False)

# ── 8.2 媒体目录穿越防护（直接调 core.media.save_upload）──
probe_traversal = TMP / "probe_traversal.py"
probe_traversal.write_text(
    "import json, sys\n"
    "from pathlib import Path\n"
    "from core import media as m\n"
    "md = Path(sys.argv[1]); md.mkdir(parents=True, exist_ok=True)\n"
    "res = {}\n"
    "for name in ('../../evil.jpg', '..\\\\evil.jpg', '/etc/passwd', 'C:\\\\Windows\\\\evil.jpg'):\n"
    "    ok, err, rel = m.save_upload(name, b'\\xff\\xd8\\xff' + b'0' * 32, md)\n"
    "    target = (md / rel).resolve() if rel else None\n"
    "    res[name] = {'ok': ok, 'rel': rel,\n"
    "                 'inside': bool(target and target.parent == md.resolve())}\n"
    "print(json.dumps(res))\n",
    encoding="utf-8")
tdir = TMP / "traversal"
r = run([str(probe_traversal), str(tdir)])
check("目录穿越探针可运行", r.returncode, 0)
if r.returncode == 0:
    tr = json.loads(r.stdout.strip().splitlines()[-1])
    all_inside = all(v["inside"] for v in tr.values())
    check("所有穿越文件名都被挡在 media 目录内", all_inside, True)
    if not all_inside:
        note(f"穿越明细：{tr}")
    check("父目录未被写入 evil.jpg", (tdir.parent / "evil.jpg").exists(), False)

# ── 8.3 控制台的 Telegram / 媒体 / 密码登录接口（真起服务打 HTTP）──
PROBE_PORT = 8901
srv = subprocess.Popen(
    [PY, "-c",
     "import uvicorn; from web.server import create_app; "
     f"uvicorn.run(create_app(), host='127.0.0.1', port={PROBE_PORT}, log_level='error')"],
    cwd=str(ROOT), env=env_for(DATA_DIR=str(TMP / "v2data")),
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    up = False
    for _ in range(60):
        time.sleep(0.5)
        code, _body = http("/healthz", port=PROBE_PORT)
        if code == 200:
            up = True
            break
    check("v2 控制台能启动", up, True)

    if up:
        # /api/status 带 tg / media 视图
        code, body = http("/api/status", port=PROBE_PORT)
        check("GET /api/status 200", code, 200)
        st = json.loads(body)
        check("/api/status 含 tg 视图", "tg" in st, True)
        check("/api/status 含 media 视图", "media" in st, True)
        check("media 视图给出视频时长上限",
              st.get("media", {}).get("video_max_seconds"), 140)

        # /api/tg/status 必须 200（模块缺失也 graceful）
        code, body = http("/api/tg/status", port=PROBE_PORT)
        check("GET /api/tg/status 200（模块缺失也优雅）", code, 200)
        if code == 200:
            tg = json.loads(body).get("tg", {})
            check("tg 视图有 available 字段", "available" in tg, True)
            check("tg 视图不泄漏完整 token", "token" not in json.dumps(tg) or
                  "token_masked" in json.dumps(tg), True)

        # /api/tg/verify 缺 token → 400 或 503（都是可读错误，不是 500）
        code, body = http("/api/tg/verify", "POST", {"token": ""}, port=PROBE_PORT)
        check("POST /api/tg/verify 空 token → 400/503", code in (400, 503), True)

        # /api/media/list 可用
        code, body = http("/api/media/list", port=PROBE_PORT)
        check("GET /api/media/list 200", code, 200)
        if code == 200:
            check("media/list 带 limits", "limits" in json.loads(body), True)

        # 媒体预览接口的穿越防护（HTTP 层）
        code, _ = http("/api/media/file?name=../../../etc/passwd", port=PROBE_PORT)
        check("媒体预览挡住目录穿越", code in (400, 404), True)

        # 密码登录接口：后端不支持时→400，不应 500
        code, body = http("/api/backends/browser/login-password", "POST",
                          {"username": "u", "password": "p"}, port=PROBE_PORT)
        check("POST login-password 不返回 500", code != 500, True)
        check("POST login-password 返回 JSON 错误体",
              "error" in json.loads(body) if code != 200 else True, True)
finally:
    srv.terminate()
    try:
        srv.wait(timeout=10)
    except subprocess.TimeoutExpired:
        srv.kill()

# ── 8.4 start.py 默认全量启动（--no-tg 确保不真的连 Telegram）──
lj = TMP / "v2start"
lj.mkdir(parents=True, exist_ok=True)
proc = subprocess.Popen(
    [PY, "start.py", "--no-tg"],
    cwd=str(ROOT), env=env_for(DATA_DIR=str(lj), WEB_PORT="8902"),
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    encoding="utf-8", errors="replace")
started = False
try:
    for _ in range(60):
        time.sleep(0.5)
        code, _ = http("/healthz", port=8902)
        if code == 200:
            started = True
            break
    check("start.py 默认全量启动可用（Web 起来了）", started, True)
    if started:
        code, body = http("/api/status", port=8902)
        if code == 200:
            st = json.loads(body)
            check("全量启动时发布循环就绪",
                  st.get("pipeline", {}).get("ready"), True)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    out = ""
    try:
        out = proc.stdout.read() if proc.stdout else ""
    except Exception:
        pass
    if not started:
        print(out[-2500:])

# ═══════════════════════════════════════════════════════════
print()
print("=" * 66)
print(" 9. 分模块 pytest（tests/）")
print("=" * 66)
if (ROOT / "tests").is_dir():
    r = run(["-m", "pytest", "tests/", "-q", "--no-header"], timeout=600)
    last = [ln for ln in r.stdout.splitlines() if "passed" in ln or "failed" in ln or "error" in ln]
    note(f"pytest: {last[-1] if last else r.stdout.strip()[-200:]}")
    check("pytest 全部通过", r.returncode, 0)
    if r.returncode != 0:
        print(r.stdout[-2500:])

# ═══════════════════════════════════════════════════════════
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(ROOT / "__pycache__", ignore_errors=True)
for p in ROOT.rglob("__pycache__"):
    shutil.rmtree(p, ignore_errors=True)

print()
print("=" * 66)
print(f" 结果：{PASS} PASS / {FAIL} FAIL")
print("=" * 66)
if NOTES:
    print("备注：")
    for n in NOTES:
        print(f"  · {n}")
sys.exit(1 if FAIL else 0)
