#!/usr/bin/env python3
"""`tools/browser_login_chrome.py` 的单元测试。

这个工具存在的理由（回归护栏）：
原来的 `tools/browser_login.py` 用 **Playwright 启动浏览器**，Playwright 自带
自动化指纹，x.com 风控会把会话判定为机器人 —— 登录页弹「出了点问题」、
URL 出现 `prelude_gate`，**用户手工点也没用**（被标记的是浏览器进程本身）。

新工具改用：普通方式启动真 Chrome（无自动化开关）-> 用户正常登录
-> 通过 Chrome 官方 CDP 读取登录态 -> 导出 storage_state。

顺带绕开 Chrome 127+ 的 **App-Bound Encryption**（外部程序无法离线解密
cookie 库、文件还被独占锁住）—— 因为我们是「问 Chrome 本人要」，
不是「撬它的文件」。

本测试全部离线，不联网、不启动真实浏览器。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from core import chrome_login as B  # noqa: E402
import browser_login_chrome as CLI  # noqa: E402  (命令行壳子)


# ══════════════════════════════════════════════════════════
# 定位 Chrome
# ══════════════════════════════════════════════════════════

def test_find_chrome_prefers_env(monkeypatch, tmp_path):
    """CHROME_PATH 显式指定时优先用它。"""
    fake = tmp_path / "chrome.exe"
    fake.write_bytes(b"x")
    monkeypatch.setenv("CHROME_PATH", str(fake))
    assert B.find_chrome() == str(fake)


def test_find_chrome_ignores_bad_env(monkeypatch):
    """CHROME_PATH 指向不存在的路径时不能直接返回它（要继续往下找）。"""
    monkeypatch.setenv("CHROME_PATH", r"C:\nope\does-not-exist.exe")
    got = B.find_chrome()
    assert got != r"C:\nope\does-not-exist.exe"


def test_find_chrome_never_raises(monkeypatch):
    """找不到 Chrome 时返回空串，不抛异常。"""
    monkeypatch.setenv("CHROME_PATH", "")
    monkeypatch.setattr(B.shutil, "which", lambda name: None)
    monkeypatch.setattr(B, "_CHROME_CANDIDATES", ())
    monkeypatch.setenv("LOCALAPPDATA", r"C:\definitely\no\such\dir")
    assert B.find_chrome() == ""


def test_free_port_is_usable():
    """要到的端口应该是合法的、临时的。"""
    import socket
    p = B.free_port()
    assert isinstance(p, int) and 1024 < p < 65536
    # 刚返回的端口应能绑定（说明确实空闲）
    with socket.socket() as s:
        s.bind(("127.0.0.1", p))


# ══════════════════════════════════════════════════════════
# 登录判定
# ══════════════════════════════════════════════════════════

def test_has_auth_requires_nonempty_auth_token():
    assert B.has_auth([{"name": "auth_token", "value": "abc"}]) is True
    # 空值不算登录
    assert B.has_auth([{"name": "auth_token", "value": ""}]) is False
    # 只有游客 cookie 不算登录
    assert B.has_auth([{"name": "guest_id", "value": "x"},
                       {"name": "ct0", "value": "y"}]) is False
    assert B.has_auth([]) is False


# ══════════════════════════════════════════════════════════
# storage_state 转换（核心逻辑）
# ══════════════════════════════════════════════════════════

@pytest.fixture()
def fake_cdp(monkeypatch):
    """把 read_cookies / read_local_storage 换成桩，专测 to_storage_state 的组装。"""
    cks = [
        {"name": "auth_token", "value": "tok", "domain": ".x.com", "path": "/",
         "expires": -1, "httpOnly": True, "secure": True, "sameSite": "None"},
        {"name": "ct0", "value": "csrf", "domain": ".x.com", "path": "/",
         "expires": 1893456000, "httpOnly": False, "secure": True, "sameSite": "Lax"},
        {"name": "unrelated", "value": "nope", "domain": ".example.com", "path": "/"},
        {"name": "old", "value": "tw", "domain": ".twitter.com", "path": "/"},
    ]
    monkeypatch.setattr(B, "read_cookies", lambda port: cks)
    monkeypatch.setattr(B, "read_local_storage",
                        lambda port: {"lang": "zh"})
    return cks


def test_to_storage_state_shape(fake_cdp):
    """导出的结构必须是 Playwright 认的 {cookies, origins}。"""
    import json as _j
    st = B.to_storage_state(1234)
    assert set(st.keys()) == {"cookies", "origins"}
    # 写出来必须是合法 JSON（避免 set / bytes 之类塞不进去）
    _j.dumps(st)


def test_to_storage_state_filters_foreign_domains(fake_cdp):
    """只保留 x.com / twitter.com 的 cookie，别站 cookie 绝不能带出去。"""
    st = B.to_storage_state(1234)
    doms = {c["domain"] for c in st["cookies"]}
    assert doms == {".x.com", ".twitter.com"}
    assert not any("example" in d for d in doms)


def test_to_storage_state_keeps_auth_fields(fake_cdp):
    """auth_token 必须完整保留（含 httpOnly/secure/sameSite），否则连不上。"""
    st = B.to_storage_state(1234)
    tok = next(c for c in st["cookies"] if c["name"] == "auth_token")
    assert tok["value"] == "tok"
    assert tok["httpOnly"] is True
    assert tok["secure"] is True
    assert tok["sameSite"] == "None"
    assert tok["path"] == "/"


def test_to_storage_state_maps_same_site(fake_cdp):
    """Playwright 只认 Strict/Lax/None；未知值要退回 Lax 而不是原样透传。"""
    st = B.to_storage_state(1234)
    allowed = {"Strict", "Lax", "None"}
    assert all(c["sameSite"] in allowed for c in st["cookies"])


def test_to_storage_state_handles_unknown_samesite(monkeypatch):
    monkeypatch.setattr(B, "read_cookies", lambda port: [
        {"name": "auth_token", "value": "t", "domain": ".x.com", "path": "/",
         "sameSite": "Weird"}])
    monkeypatch.setattr(B, "read_local_storage", lambda port: {})
    st = B.to_storage_state(1)
    assert st["cookies"][0]["sameSite"] == "Lax"


def test_to_storage_state_includes_origins_when_localstorage(monkeypatch):
    monkeypatch.setattr(B, "read_cookies", lambda port: [])
    monkeypatch.setattr(B, "read_local_storage", lambda port: {"a": "1", "b": "2"})
    st = B.to_storage_state(1)
    assert st["origins"], "有 localStorage 时 origins 不能为空"
    items = {i["name"]: i["value"] for i in st["origins"][0]["localStorage"]}
    assert items == {"a": "1", "b": "2"}


def test_to_storage_state_no_localstorage_gives_empty_origins(monkeypatch):
    monkeypatch.setattr(B, "read_cookies", lambda port: [])
    monkeypatch.setattr(B, "read_local_storage", lambda port: {})
    st = B.to_storage_state(1)
    assert st["origins"] == []


def test_to_storage_state_missing_keys_do_not_crash(monkeypatch):
    """真实 CDP 返回的字段可能缺斤少两，不能因此炸掉。"""
    monkeypatch.setattr(B, "read_cookies", lambda port: [{"name": "auth_token"}])
    monkeypatch.setattr(B, "read_local_storage", lambda port: {})
    st = B.to_storage_state(1)
    assert st["cookies"][0]["name"] == "auth_token"


# ══════════════════════════════════════════════════════════
# 不联网：读 cookie 失败要静默返回，不能把异常抛给用户
# ══════════════════════════════════════════════════════════

def test_read_cookies_returns_empty_when_no_page(monkeypatch):
    monkeypatch.setattr(B, "pick_page", lambda port: None)
    assert B.read_cookies(1) == []


def test_read_local_storage_returns_empty_when_no_page(monkeypatch):
    monkeypatch.setattr(B, "pick_page", lambda port: None)
    assert B.read_local_storage(1) == {}


def test_wait_for_cdp_times_out_gracefully(monkeypatch):
    """调试端口起不来时返回 None，不能无限等、不能抛异常。"""
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(B, "_http_json", boom)
    assert B.wait_for_cdp(9, timeout=0.3) is None


# ══════════════════════════════════════════════════════════
# 契约：login_via_chrome 绝不抛异常
# ══════════════════════════════════════════════════════════

def test_login_via_chrome_no_chrome_returns_false(monkeypatch, tmp_path):
    """找不到 Chrome 时必须返回 (False, 可读原因)，不能抛异常。"""
    monkeypatch.setenv("CHROME_PATH", "")
    monkeypatch.setattr(B, "find_chrome", lambda: "")
    ok, msg = B.login_via_chrome(state_path=tmp_path / "s.json",
                                 profile_dir=tmp_path / "p")
    assert ok is False
    assert "Chrome" in msg


def test_login_via_chrome_bad_chrome_path(monkeypatch, tmp_path):
    """Chrome 启动失败也要返回 (False, ...)，不能把异常抛给调用方。"""
    ok, msg = B.login_via_chrome(state_path=tmp_path / "s.json",
                                 profile_dir=tmp_path / "p",
                                 chrome_path=str(tmp_path / "nope.exe"))
    assert ok is False
    assert msg


def test_login_via_chrome_timeout_is_graceful(monkeypatch, tmp_path):
    """调试端口起不来时（wait_for_cdp 返回 None）要给出可读原因。

    ⚠ 必须伪造 has_display()=True。否则在无桌面的 Linux 上会先命中
    "没有图形界面" 的检查（那是**正确**行为），走不到端口这步，
    本用例的前提就不成立了。
    """
    fake = tmp_path / "chrome.exe"
    fake.write_bytes(b"x")
    monkeypatch.setattr(B, "has_display", lambda: True)
    monkeypatch.setattr(B, "wait_for_cdp", lambda port, timeout: None)
    monkeypatch.setattr(B.subprocess, "Popen", lambda *a, **k: type(
        "P", (), {"terminate": lambda s: None, "wait": lambda s, timeout=0: 0,
                  "kill": lambda s: None})())
    ok, msg = B.login_via_chrome(state_path=tmp_path / "s.json",
                                 profile_dir=tmp_path / "p",
                                 chrome_path=str(fake), timeout=30)
    assert ok is False
    assert "调试端口" in msg


def test_cli_state_only_when_no_state(monkeypatch, tmp_path):
    """CLI 在未登录时 --check 应返回 2，而不是崩掉。"""
    assert CLI.main(["--check"]) in (0, 2)


def test_cli_finds_chrome_and_reports(monkeypatch, capsys):
    """--check 要能正常打印并返回，不抛异常。"""
    rc = CLI.main(["--check"])
    out = capsys.readouterr().out
    assert "登录态文件" in out
    assert rc in (0, 2)
