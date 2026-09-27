"""core/reply.py 的单元测试 —— 纯函数，无副作用、不联网。

覆盖最关键的一条安全线：评论目标（`r:`）和转帖目标（`t:`）**绝不能**被
当成引用目标 —— 评论被发成引用转发是内容对形态错，转帖被发成引用更是
凭空多出一条自己写的推文。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import reply  # noqa: E402


@pytest.mark.parametrize("tid,expect", [
    ("987654321", "r:987654321"),
    (987654321, "r:987654321"),
    ("  987654321  ", "r:987654321"),
])
def test_make_reply_target(tid, expect):
    assert reply.make_reply_target(tid) == expect


@pytest.mark.parametrize("bad", ["", None, "   ", 0])
def test_make_reply_target_empty(bad):
    assert reply.make_reply_target(bad) == ""


def test_is_reply_target():
    assert reply.is_reply_target("r:123") is True
    assert reply.is_reply_target("  r:123  ") is True
    assert reply.is_reply_target("123") is False
    assert reply.is_reply_target("") is False
    assert reply.is_reply_target(None) is False


def test_reply_target_id():
    assert reply.reply_target_id("r:123") == "123"
    assert reply.reply_target_id("  r:123  ") == "123"
    assert reply.reply_target_id("123") == ""
    assert reply.reply_target_id(None) == ""


def test_quote_target_id_excludes_replies():
    """安全线：回复目标不能被当作引用目标。"""
    assert reply.quote_target_id("123") == "123"
    assert reply.quote_target_id("r:123") == ""
    assert reply.quote_target_id("") == ""
    assert reply.quote_target_id(None) == ""


def test_make_retweet_target():
    assert reply.make_retweet_target("987654321") == "t:987654321"
    assert reply.make_retweet_target(987654321) == "t:987654321"
    assert reply.make_retweet_target("  987654321  ") == "t:987654321"
    # 转帖目标不会跟评论前缀撞车
    assert reply.make_retweet_target("123") != reply.make_reply_target("123")


@pytest.mark.parametrize("bad", ["", None, "   ", 0])
def test_make_retweet_target_empty(bad):
    assert reply.make_retweet_target(bad) == ""


def test_is_retweet_target():
    assert reply.is_retweet_target("t:123") is True
    assert reply.is_retweet_target("  t:123  ") is True
    assert reply.is_retweet_target("123") is False
    assert reply.is_retweet_target("r:123") is False
    assert reply.is_retweet_target("") is False
    assert reply.is_retweet_target(None) is False


def test_retweet_target_id():
    assert reply.retweet_target_id("t:123") == "123"
    assert reply.retweet_target_id("  t:123  ") == "123"
    assert reply.retweet_target_id("123") == ""
    assert reply.retweet_target_id("r:123") == ""
    assert reply.retweet_target_id(None) == ""


def test_quote_target_id_excludes_retweets():
    """安全线：转帖目标绝不能被当作引用目标（否则会凭空发一条引用推文）。"""
    assert reply.quote_target_id("t:123") == ""
    assert reply.reply_target_id("t:123") == ""
    assert reply.retweet_target_id("r:123") == ""


@pytest.mark.parametrize("stored,kind", [
    ("r:123", "reply"),
    ("t:123", "retweet"),
    ("123", "quote"),
    ("", ""),
    (None, ""),
])
def test_target_kind(stored, kind):
    assert reply.target_kind(stored) == kind


@pytest.mark.parametrize("stored,expect", [
    ("r:123", "123"),
    ("t:123", "123"),
    ("123", "123"),
    ("", ""),
])
def test_target_id(stored, expect):
    assert reply.target_id(stored) == expect


def test_tweet_url():
    assert reply.tweet_url("123") == "https://x.com/i/status/123"
    assert reply.tweet_url("") == ""
