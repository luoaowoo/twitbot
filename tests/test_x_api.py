"""core/backends/x_api.py 的单元测试（★B）—— 全部离线，**绝不真联网**。

跑法（在 twitbot 目录下）：
    $env:PYTHONIOENCODING="utf-8"
    .\\.venv\\Scripts\\python.exe -m pytest tests\\test_x_api.py -v

覆盖：
  * 无凭据 available() 返回 False 且不抛
  * 发布成功 → tweet_url 形如 https://x.com/i/status/<id>
  * 429 → retryable=True，wait_seconds 由 x-rate-limit-reset 头算出
  * 401 → retryable=False，error 含 "Read and Write"
  * 403 + duplicate → error 含 "重复"
  * 403 其它 → error 含 "权限"
  * 媒体上传 403 但发文字成功 → ok=True，degraded 非空
  * 未知异常 → 仍返回 PublishResult(ok=False)，不外抛
  * verify() 只读鉴权、不产生可见内容
  * 引用转发按 settings.quote_mode 决定；客户端实例被复用
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
import tweepy

# 保证从项目根导入 core（`python -m pytest` 已把 cwd 入 path，这里再兜一层）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import config, settings  # noqa: E402
from core.backends import x_api as xapi  # noqa: E402
from core.backends.base import Job, PublishResult  # noqa: E402


# ── 测试替身 ──────────────────────────────────────────────

class FakeResponse:
    """够 tweepy.HTTPException 用的最小 requests.Response 替身。"""

    def __init__(self, status_code: int, reason: str = "", headers: dict | None = None, text: str = ""):
        self.status_code = status_code
        self.reason = reason
        self.headers = headers or {}
        self.text = text

    def json(self):  # pragma: no cover - 我们总是显式传 response_json
        return {}


def http_error(cls, status_code, *, reason="Error", headers=None, body=None, text="", **kw):
    """构造一个 tweepy 的 HTTP 异常，带假响应。"""
    resp = FakeResponse(status_code, reason, headers, text)
    return cls(resp, response_json=(body if body is not None else {}), **kw)


class FakeV2:
    """tweepy.Client 替身：记录调用参数，按脚本抛异常或返回响应。"""

    def __init__(self, *, tweet_id="1234567890", raise_on_tweet=None, me_username="tester"):
        self.calls: list[dict] = []
        self._tweet_id = tweet_id
        self._raise = raise_on_tweet
        self._me_username = me_username

    def create_tweet(self, **kwargs):
        self.calls.append(("create_tweet", kwargs))
        if self._raise is not None:
            raise self._raise
        return type("R", (), {"data": {"id": self._tweet_id, "text": kwargs.get("text", "")}})()

    def get_me(self, **kwargs):
        self.calls.append(("get_me", kwargs))
        if self._raise is not None:
            raise self._raise
        if self._me_username is None:
            return type("R", (), {"data": None})()
        return type("R", (), {"data": {"id": "1", "username": self._me_username}})()


class FakeV1:
    """tweepy.API 替身：媒体上传按脚本抛异常或返回 media_id。"""

    def __init__(self, *, media_id="555", raise_on_upload=None):
        self.calls: list[dict] = []
        self._media_id = media_id
        self._raise = raise_on_upload

    def media_upload(self, filename, **kwargs):
        self.calls.append({"filename": filename, **kwargs})
        if self._raise is not None:
            raise self._raise
        return type("M", (), {"media_id": self._media_id, "id": self._media_id})()


# ── fixtures ──────────────────────────────────────────────

@pytest.fixture
def be():
    """干净的后端实例（不共享注册表里的单例缓存）。"""
    return xapi.XApiBackend()


@pytest.fixture
def no_creds(monkeypatch):
    """抹掉凭据 —— available() 必须为 False。"""
    for k in ("X_CONSUMER_KEY", "X_CONSUMER_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"):
        monkeypatch.setattr(config, k, "")


@pytest.fixture
def creds(monkeypatch):
    """注入假凭据（不会真连网，客户端被替换）。"""
    monkeypatch.setattr(config, "X_CONSUMER_KEY", "ck")
    monkeypatch.setattr(config, "X_CONSUMER_SECRET", "cs")
    monkeypatch.setattr(config, "X_ACCESS_TOKEN", "at")
    monkeypatch.setattr(config, "X_ACCESS_SECRET", "asec")


@pytest.fixture
def offline_settings(monkeypatch):
    """不碰 SQLite：quote_mode 默认 auto。"""
    monkeypatch.setattr(xapi.settings, "get", lambda key, default="": default if default else "auto")


@pytest.fixture
def media_dir(monkeypatch, tmp_path):
    """把 MEDIA_DIR 指到临时目录，并放一个假图片。"""
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    (tmp_path / "pic.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return tmp_path


def wire(be, monkeypatch, v2=None, v1=None):
    """把后端内部客户端替换成替身（绕过 tweepy 真实构造）。"""
    v2 = v2 or FakeV2()
    monkeypatch.setattr(be, "_v2_client", lambda: v2)
    monkeypatch.setattr(be, "_v1_client", lambda: (v1 if v1 is not None else FakeV1()))
    return v2


# ── available / login ─────────────────────────────────────

def test_available_without_creds_is_false_and_does_not_raise(be, no_creds):
    ok, reason = be.available()          # 不抛异常即通过
    assert ok is False
    assert "未配置 X API 凭据" in reason
    assert "setup_x.py" in reason


def test_available_with_creds_is_true(be, creds):
    ok, reason = be.available()
    assert ok is True
    assert reason == "已配置 API 凭据"


def test_login_needs_no_interaction(be):
    events: list[str] = []
    ok, reason = be.login(on_event=events.append)
    assert ok is True
    assert reason == "无需登录（使用 API 凭据）"
    assert events  # 进度回调被调用过


def test_class_attrs(be):
    assert be.name == "x_api"
    assert be.label == "X 官方 API"


# ── publish：成功路径 ─────────────────────────────────────

def test_publish_success_url_format(be, monkeypatch, creds, offline_settings):
    v2 = wire(be, monkeypatch, FakeV2(tweet_id="987654321"))
    res = be.publish(Job(id=7, kind="text"), "hello world")

    assert isinstance(res, PublishResult)
    assert res.ok is True
    assert res.tweet_id == "987654321"
    assert res.tweet_url == "https://x.com/i/status/987654321"
    assert res.backend == "x_api"
    assert res.text == "hello world"
    assert res.media_uploaded is False
    assert res.degraded == ""
    _, kwargs = v2.calls[0]
    assert kwargs["text"] == "hello world"
    assert "media_ids" not in kwargs
    assert "quote_tweet_id" not in kwargs


def test_publish_does_not_trim_text(be, monkeypatch, creds, offline_settings):
    v2 = wire(be, monkeypatch)
    long_text = "长" * 500 + " tail"
    res = be.publish(Job(id=8, kind="text"), long_text)
    assert res.ok is True
    assert res.text == long_text                       # 原样返回，未裁剪
    assert v2.calls[0][1]["text"] == long_text         # 原样传给 API


def test_publish_media_uploaded(be, monkeypatch, creds, offline_settings, media_dir):
    v1 = FakeV1(media_id="555")
    v2 = wire(be, monkeypatch, v2=FakeV2(), v1=v1)
    job = Job(id=9, kind="photo", media_path="pic.jpg")

    res = be.publish(job, "with pic")

    assert res.ok is True
    assert res.media_uploaded is True
    assert res.degraded == ""
    assert v1.calls and v1.calls[0]["filename"].endswith("pic.jpg")
    assert v2.calls[0][1]["media_ids"] == ["555"]


# ── publish：错误分类 ─────────────────────────────────────

def test_publish_429_uses_rate_limit_reset_header(be, monkeypatch, creds, offline_settings):
    reset = int(time.time()) + 300
    exc = http_error(
        tweepy.TooManyRequests,
        429,
        reason="Too Many Requests",
        headers={"x-rate-limit-reset": str(reset)},
        body={"errors": [{"code": 88, "message": "Rate limit exceeded"}]},
        reset_time=None,           # 强制走响应头分支
    )
    wire(be, monkeypatch, FakeV2(raise_on_tweet=exc))

    res = be.publish(Job(id=10, kind="text"), "hi")

    assert res.ok is False
    assert res.retryable is True
    assert 250 <= res.wait_seconds <= 310          # ≈ reset - now
    assert res.wait_seconds >= 60
    assert "429" in res.error


def test_publish_429_without_header_falls_back_to_900(be, monkeypatch, creds, offline_settings):
    exc = http_error(tweepy.TooManyRequests, 429, reason="Too Many Requests")
    wire(be, monkeypatch, FakeV2(raise_on_tweet=exc))

    res = be.publish(Job(id=11, kind="text"), "hi")
    assert res.retryable is True
    assert res.wait_seconds == 900


def test_publish_429_expired_reset_clamps_to_60(be, monkeypatch, creds, offline_settings):
    exc = http_error(
        tweepy.TooManyRequests, 429,
        headers={"x-rate-limit-reset": str(int(time.time()) - 500)},
        reset_time=None,
    )
    wire(be, monkeypatch, FakeV2(raise_on_tweet=exc))
    res = be.publish(Job(id=12, kind="text"), "hi")
    assert res.wait_seconds == 60                  # 至少 60 秒


def test_publish_401_hints_read_and_write(be, monkeypatch, creds, offline_settings):
    exc = http_error(
        tweepy.Unauthorized, 401,
        reason="Unauthorized",
        body={"title": "Unauthorized", "detail": "Unauthorized"},
    )
    wire(be, monkeypatch, FakeV2(raise_on_tweet=exc))

    res = be.publish(Job(id=13, kind="text"), "hi")

    assert res.ok is False
    assert res.retryable is False                  # 重试无意义
    assert res.wait_seconds == 0
    assert "Read and Write" in res.error
    assert "重新生成" in res.error


def test_publish_403_duplicate_is_distinguished(be, monkeypatch, creds, offline_settings):
    exc = http_error(
        tweepy.Forbidden, 403,
        reason="Forbidden",
        body={"errors": [{"code": 187, "message": "Status is a duplicate."}]},
        text='{"errors":[{"code":187,"message":"Status is a duplicate."}]}',
    )
    wire(be, monkeypatch, FakeV2(raise_on_tweet=exc))

    res = be.publish(Job(id=14, kind="text"), "same as before")

    assert res.ok is False
    assert res.retryable is False
    assert "重复" in res.error
    assert "改文案" in res.error


def test_publish_403_other_mentions_permission(be, monkeypatch, creds, offline_settings):
    exc = http_error(
        tweepy.Forbidden, 403,
        reason="Forbidden",
        body={"detail": "You are not allowed to upload media."},
    )
    wire(be, monkeypatch, FakeV2(raise_on_tweet=exc))

    res = be.publish(Job(id=15, kind="text"), "hi")

    assert res.ok is False
    assert res.retryable is False
    assert "权限" in res.error
    assert "配额" in res.error


def test_publish_5xx_is_retryable(be, monkeypatch, creds, offline_settings):
    exc = http_error(tweepy.TwitterServerError, 503, reason="Service Unavailable")
    wire(be, monkeypatch, FakeV2(raise_on_tweet=exc))

    res = be.publish(Job(id=16, kind="text"), "hi")
    assert res.ok is False
    assert res.retryable is True
    assert res.wait_seconds == 0


def test_publish_network_error_is_retryable(be, monkeypatch, creds, offline_settings):
    import requests

    wire(be, monkeypatch, FakeV2(raise_on_tweet=requests.exceptions.ConnectTimeout("timed out")))

    res = be.publish(Job(id=17, kind="text"), "hi")
    assert res.ok is False
    assert res.retryable is True
    assert "网络" in res.error


def test_publish_unknown_exception_returns_result_not_raised(be, monkeypatch, creds, offline_settings):
    wire(be, monkeypatch, FakeV2(raise_on_tweet=ValueError("boom")))

    res = be.publish(Job(id=18, kind="text"), "hi")   # 不得抛异常

    assert isinstance(res, PublishResult)
    assert res.ok is False
    assert res.retryable is False
    assert "ValueError" in res.error                  # 带异常类型名便于排障
    assert res.extra.get("exception") == "ValueError"


def test_publish_without_creds_returns_result(be, no_creds):
    res = be.publish(Job(id=19, kind="text"), "hi")
    assert res.ok is False
    assert "未配置 X API 凭据" in res.error
    assert res.backend == "x_api"


# ── publish：媒体降级 ─────────────────────────────────────

def test_media_403_degrades_but_text_still_sent(be, monkeypatch, creds, offline_settings, media_dir):
    exc = http_error(tweepy.Forbidden, 403, reason="Forbidden",
                     body={"detail": "media upload not allowed"})
    v1 = FakeV1(raise_on_upload=exc)
    v2 = wire(be, monkeypatch, v2=FakeV2(tweet_id="42"), v1=v1)
    job = Job(id=20, kind="photo", media_path="pic.jpg")

    res = be.publish(job, "文字还是要发出去的")

    assert res.ok is True                      # 降级而不是整体失败
    assert res.degraded != ""                  # 降级原因写清楚了
    assert "403" in res.degraded
    assert "纯文本" in res.degraded
    assert res.media_uploaded is False
    assert res.tweet_url == "https://x.com/i/status/42"
    _, kwargs = v2.calls[0]
    assert "media_ids" not in kwargs
    assert kwargs["text"] == "文字还是要发出去的"


def test_media_upload_fails_and_text_fails_is_overall_failure(be, monkeypatch, creds, offline_settings, media_dir):
    media_exc = http_error(tweepy.Forbidden, 403, reason="Forbidden", body={})
    tweet_exc = http_error(tweepy.Forbidden, 403, reason="Forbidden",
                           body={"errors": [{"code": 187, "message": "Status is a duplicate."}]})
    wire(be, monkeypatch, v2=FakeV2(raise_on_tweet=tweet_exc), v1=FakeV1(raise_on_upload=media_exc))
    job = Job(id=21, kind="photo", media_path="pic.jpg")

    res = be.publish(job, "same text")

    assert res.ok is False
    assert "重复" in res.error
    assert res.degraded != ""                  # 媒体降级信息仍保留，便于排障


def test_missing_media_file_is_not_a_degradation(be, monkeypatch, creds, offline_settings, media_dir):
    v2 = wire(be, monkeypatch)
    job = Job(id=22, kind="photo", media_path="does_not_exist.jpg")

    res = be.publish(job, "hi")

    assert res.ok is True
    assert res.degraded == ""                  # 文件本来就不在 → 静默纯文本
    assert res.media_uploaded is False
    assert "media_ids" not in v2.calls[0][1]


# ── publish：引用转发 ─────────────────────────────────────

def test_quote_id_passed_when_mode_auto(be, monkeypatch, creds):
    monkeypatch.setattr(xapi.settings, "get", lambda key, default="": "auto")
    v2 = wire(be, monkeypatch)

    res = be.publish(Job(id=23, kind="text", quote_id="111222333"), "quoting")

    assert res.ok is True
    assert v2.calls[0][1]["quote_tweet_id"] == "111222333"
    assert res.extra.get("quote_tweet_id") == "111222333"


def test_quote_id_ignored_when_mode_off(be, monkeypatch, creds):
    monkeypatch.setattr(xapi.settings, "get", lambda key, default="": "off")
    v2 = wire(be, monkeypatch)

    res = be.publish(Job(id=24, kind="text", quote_id="111222333"), "plain")

    assert res.ok is True
    assert "quote_tweet_id" not in v2.calls[0][1]


# ── verify ────────────────────────────────────────────────

def test_verify_ok_reports_username(be, monkeypatch, creds):
    v2 = wire(be, monkeypatch, FakeV2(me_username="alice"))

    ok, reason = be.verify()

    assert ok is True
    assert reason == "鉴权正常，账号 @alice"
    assert [c[0] for c in v2.calls] == ["get_me"]        # 只读，不发推
    assert v2.calls[0][1].get("user_auth") is True


def test_verify_401_returns_reason(be, monkeypatch, creds):
    exc = http_error(tweepy.Unauthorized, 401, reason="Unauthorized", body={})
    wire(be, monkeypatch, FakeV2(raise_on_tweet=exc))

    ok, reason = be.verify()

    assert ok is False
    assert "Read and Write" in reason


def test_verify_without_creds_is_false(be, no_creds):
    ok, reason = be.verify()
    assert ok is False
    assert "未配置 X API 凭据" in reason


def test_verify_unknown_exception_does_not_raise(be, monkeypatch, creds):
    wire(be, monkeypatch, FakeV2(raise_on_tweet=RuntimeError("weird")))

    ok, reason = be.verify()

    assert ok is False
    assert "RuntimeError" in reason


# ── 客户端缓存 ────────────────────────────────────────────

def test_client_is_cached_and_not_rebuilt(be, creds, monkeypatch):
    built = {"v2": 0, "v1": 0}
    real_client, real_api = tweepy.Client, tweepy.API

    def counting_client(*a, **kw):
        built["v2"] += 1
        return real_client(*a, **kw)

    def counting_api(*a, **kw):
        built["v1"] += 1
        return real_api(*a, **kw)

    monkeypatch.setattr(xapi.tweepy, "Client", counting_client)
    monkeypatch.setattr(xapi.tweepy, "API", counting_api)

    first_v2, first_v1 = be._v2_client(), be._v1_client()
    for _ in range(5):
        assert be._v2_client() is first_v2
        assert be._v1_client() is first_v1

    assert built == {"v2": 1, "v1": 1}          # 构造只发生一次


def test_client_rebuilt_after_creds_change(be, creds, monkeypatch):
    first = be._v2_client()
    monkeypatch.setattr(config, "X_ACCESS_TOKEN", "at-new")
    second = be._v2_client()
    assert first is not second                   # 凭据变了要重建
    be.reset_clients()
    assert be._v2 is None


# ── 契约不变式 ────────────────────────────────────────────

def test_publish_never_raises_on_any_exception(be, monkeypatch, creds, offline_settings):
    """用一批稀奇古怪的异常轰炸 publish，必须全部收敛成 PublishResult。"""
    # 注意：KeyboardInterrupt / SystemExit 属 BaseException，按 Python 惯例必须
    # 穿透（否则 Ctrl-C 与 sys.exit 会失灵），契约里的"不抛异常"指 Exception 家族。
    weird = [
        MemoryError("oom"),
        tweepy.TweepyException("generic"),
        OSError("disk"),
        RuntimeError("generic runtime"),
    ]
    for exc in weird:
        wire(be, monkeypatch, FakeV2(raise_on_tweet=exc))
        res = be.publish(Job(id=99, kind="text"), "hi")
        assert isinstance(res, PublishResult)
        assert res.ok is False
        assert res.error != ""


if __name__ == "__main__":  # 允许 `python tests/test_x_api.py` 直接跑
    raise SystemExit(pytest.main([__file__, "-v"]))
