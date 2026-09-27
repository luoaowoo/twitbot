"""控制台前端（web/static/index.html）的结构回归。

背景：2026-09-26 把控制台改成「左侧选项卡」布局。那张页面是**一个巨大的
内联 HTML + JS**，没有构建步骤也没有框架 —— 一旦重构时手滑删掉某个 id，
只有真人在浏览器里点才会发现。这组用例把契约钉死：

  * JS 依赖的元素 id 必须都在
  * 选项卡与分区的 id 必须一一对应
  * 危险的内联脚本（`onclick="..."` 引用的函数）必须存在

纯静态检查，不开浏览器、不联网。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "web" / "static" / "index.html"

# JS 里用 $("...") 取过的元素 —— 缺一个就会在运行时 null.xxx 报错
REQUIRED_IDS = [
    # 框架
    "dot", "ver", "nav", "foot-note", "toasts",
    # 顶部状态
    "s-backend", "s-depth", "s-month", "s-total", "s-pipe", "btn-pause",
    # 概览
    "ov-status",
    # 发布后端
    "backs", "loginbox", "login-spin", "login-msg", "login-log",
    "pw-user", "pw-pass", "pw-code", "pw-code-row", "btn-pw-login", "btn-pw-code",
    # Telegram
    "tg-badge", "tg-who", "tg-token", "tg-token-hint", "tg-admin-list",
    "tg-add-admin", "btn-tg-add-admin", "tg-contacts", "tg-pending", "tg-chats",
    "tg-auto", "btn-tg-verify", "btn-tg-start", "btn-tg-stop", "btn-tg-restart",
    "tg-info",
    # 投料
    "f-text", "counter", "preview", "drop", "f-file", "drop-in", "media-limits",
    "drop-picked", "f-media", "f-quote", "f-reply", "f-retweet", "btn-post",
    "f-await",
    # 队列
    "jobs-hint", "filters", "jobs",
    # 数据日报
    "btn-an-refresh", "an-hint", "an-summary", "an-detail", "an-progress",
    # 素材库选择器（发送页 —— 梗图编辑页已按用户要求移除，素材库保留）
    "btn-lib-pick", "lib-picker",
    # 采集
    "col-platforms", "col-min-likes", "col-min-comments", "col-unused",
    "btn-col-refresh", "col-stats", "col-items", "col-count",
    "col-login", "col-login-title", "col-login-qr", "col-login-msg",
    # 设置
    "s-prefix", "s-suffix", "s-limit", "s-dedup", "s-quote", "btn-save",
    "f-datadir",
    # 其它
    "f-state", "btn-an-refresh",
]

TABS = ["overview", "publish", "queue", "collect", "analytics",
        "backend", "telegram", "settings"]


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_index_exists(html):
    assert "<html" in html and "</html>" in html


@pytest.mark.parametrize("element_id", REQUIRED_IDS)
def test_required_element_id_present(html, element_id):
    """每个 id 都要在页面上真实存在（JS 会直接 $() 取）。"""
    assert re.search(rf'id="{re.escape(element_id)}"', html), \
        f"index.html 里找不到 id={element_id}（JS 会取到 null）"


@pytest.mark.parametrize("tab", TABS)
def test_tab_section_exists(html, tab):
    """每个导航按钮都要有对应的 section。"""
    assert f'data-tab="{tab}"' in html, f"导航里没有 {tab}"
    assert f'id="tab-{tab}"' in html, f"没有 {tab} 对应的 section"


def test_tabs_match_sections(html):
    """导航里的选项卡集合必须和 section 集合完全一致（不多不少）。"""
    nav = set(re.findall(r'data-tab="([a-z]+)"', html))
    secs = {m for m in re.findall(r'id="tab-([a-z]+)"', html)}
    assert nav == secs == set(TABS), f"导航={sorted(nav)} section={sorted(secs)}"


def test_only_one_section_initially_visible(html):
    """初始只展示一个分区（否则所有卡片会堆在一起）。"""
    assert html.count('class="section on"') == 1


def test_inline_onclick_handlers_exist(html):
    """`onclick="foo()"` 引用的函数必须在脚本里定义过（拼错就会点了没反应）。"""
    called = set(re.findall(r'onclick="([A-Za-z_$][\w$]*)\s*\(', html))
    defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", html))
    missing = sorted(c for c in called if c not in defined)
    assert not missing, f"这些 onclick 函数没有定义：{missing}"


def test_switch_and_helpers_defined(html):
    """选项卡核心函数与日报加载函数必须都在。"""
    for fn in ("showTab", "bindTabs", "renderOverview", "loadAnalytics",
               "loadAdmins", "loadTgContacts", "doUploadState", "addAdmin",
               "openLibraryPicker", "pickLibraryItem",
               "loadCollect", "renderCollectPlatforms", "colRun",
               "loadCollectItems", "renderCollectItems", "colPublish", "colLogin",
               "colLoginRestart", "showCollectQr"):
        assert re.search(rf"function\s+{fn}\s*\(", html), f"缺少函数 {fn}()"


def test_tab_state_is_remembered(html):
    """切选项卡要写进 localStorage / hash，刷新后能回到原来那页。"""
    assert "localStorage.setItem" in html
    assert "location.hash" in html


def test_light_theme_colors(html):
    """确认用的是浅色主题（防止有人把设计改回深色）。"""
    assert "--bg:#ffffff" in html.replace(" ", "")
    assert "--accent:#b5664c" in html.replace(" ", "")


def test_no_dark_background_leftover(html):
    """旧深色主题的主色不该再出现。"""
    assert "#0f1216" not in html
