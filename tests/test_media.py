#!/usr/bin/env python3
"""`core/media.py` 单元测试（CONTRACT_V2 §4.3，Agent G 归属文件）。

覆盖：扩展名/mime 判定（含大小写、.jpeg/.mp4/未知）、各上限边界（**刚好等于上限
通过、超 1 字节失败**）、视频超时长、目录穿越攻击（`../../evil.jpg`、`..\\evil.jpg`、
`/etc/passwd`、`C:\\Windows\\evil.jpg`）、重名不覆盖、`probe_video` 对垃圾/空文件
返回 `(0.0, ...)` 不抛异常、mvhd 降级解析（自造 mp4，不依赖真实视频/ffmpeg）。

全部用 `tmp_path` + 自造小文件；**不联网、不依赖 ffmpeg/ffprobe、不碰真实 data/**。
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from core import media


# ══════════════════════════════════════════════════════════
# 工具：自造文件 / 自造最小 mp4
# ══════════════════════════════════════════════════════════

def make_file(path: Path, size: int) -> Path:
    """造一个恰好 size 字节的文件（truncate 稀疏写，快）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        if size:
            fh.write(b"\0" * min(size, 64))
            fh.truncate(size)
    return path


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def mvhd_payload(duration: float, timescale: int = 1000, version: int = 0) -> bytes:
    if version == 1:
        return (b"\x01\x00\x00\x00" + b"\0" * 8 + b"\0" * 8
                + struct.pack(">I", timescale)
                + struct.pack(">Q", int(duration * timescale)))
    return (b"\x00\x00\x00\x00" + b"\0" * 4 + b"\0" * 4
            + struct.pack(">I", timescale)
            + struct.pack(">I", int(duration * timescale)))


def fake_mp4(path: Path, duration: float, version: int = 0) -> Path:
    """造一个含 moov/mvhd 的最小 mp4 —— 供 mvhd 降级解析测试。"""
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41")
    mvhd = _box(b"mvhd", mvhd_payload(duration, version=version))
    data = ftyp + _box(b"moov", mvhd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


GB = 1024 * 1024 * 1024


# ══════════════════════════════════════════════════════════
# 常量（X 平台真实限制 —— 冻结取值）
# ══════════════════════════════════════════════════════════

def test_limit_constants_are_exact():
    assert media.IMAGE_MAX_BYTES == 5 * 1024 * 1024 == 5_242_880
    assert media.GIF_MAX_BYTES == 15 * 1024 * 1024 == 15_728_640
    assert media.VIDEO_MAX_BYTES == 512 * 1024 * 1024 == 536_870_912
    assert media.VIDEO_MAX_SECONDS == 140
    assert media.MAX_IMAGES == 4
    assert media.MAX_IMAGES < 10          # 别被改成 5/10


def test_kind_by_ext_shape():
    assert isinstance(media.KIND_BY_EXT, dict)
    assert media.KIND_BY_EXT[".jpg"] == "photo"
    assert media.KIND_BY_EXT[".jpeg"] == "photo"
    assert media.KIND_BY_EXT[".mp4"] == "video"
    assert media.KIND_BY_EXT[".pdf"] == "document"
    assert media.KIND_BY_EXT[".gif"] == "photo"
    assert all(k == k.lower() and k.startswith(".") for k in media.KIND_BY_EXT)
    assert all(v in ("photo", "video", "text", "document") for v in media.KIND_BY_EXT.values())


def test_limits_for_values():
    assert media.limits_for("photo") == (5 * 1024 * 1024, None)
    assert media.limits_for("gif") == (15 * 1024 * 1024, None)
    assert media.limits_for("video") == (512 * 1024 * 1024, 140)
    assert media.limits_for("document")[1] is None
    assert media.limits_for("") == media.limits_for("document")
    assert media.limits_for("NONSENSE") == media.limits_for("document")
    assert media.limits_for(None)[0] == 5 * 1024 * 1024      # 不抛


# ══════════════════════════════════════════════════════════
# detect_kind
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize("name,expect", [
    ("a.jpg", "photo"), ("a.JPG", "photo"), ("a.JpEg", "photo"),
    ("a.png", "photo"), ("a.webp", "photo"), ("a.gif", "photo"),
    ("a.mp4", "video"), ("a.MP4", "video"), ("a.MoV", "video"),
    ("a.mkv", "video"), ("a.webm", "video"),
    ("a.pdf", "document"), ("a.zip", "document"),
    ("a.txt", "document"),          # .txt 走扩展名 → document（扩展名优先）
    ("archive.tar.gz", "document"),
])
def test_detect_kind_by_extension(name, expect):
    assert media.detect_kind(name) == expect


def test_detect_kind_extension_beats_mime():
    # 扩展名优先：即使 mime 说是 video，.jpg 仍是 photo
    assert media.detect_kind("a.jpg", "video/mp4") == "photo"
    assert media.detect_kind("a.mp4", "image/jpeg") == "video"


@pytest.mark.parametrize("mime,expect", [
    ("image/jpeg", "photo"), ("IMAGE/PNG", "photo"),
    ("video/mp4", "video"), ("Video/QuickTime", "video"),
    ("text/plain", "text"), ("TEXT/CSV", "text"),
    ("application/pdf", "document"), ("application/octet-stream", "document"),
])
def test_detect_kind_by_mime(mime, expect):
    assert media.detect_kind("blob.bin", mime) == expect     # .bin 不在表里 → 用 mime
    assert media.detect_kind("", mime) == expect


def test_detect_kind_mime_with_charset_param():
    assert media.detect_kind("x.unknown", "image/jpeg; charset=binary") == "photo"
    assert media.detect_kind("x.unknown", "text/plain;charset=UTF-8") == "text"


def test_detect_kind_fallback_document():
    assert media.detect_kind("noext") == "document"
    assert media.detect_kind("weird.qqq") == "document"
    assert media.detect_kind("weird.qqq", "application/x-made-up") == "document"
    assert media.detect_kind("", "") == "document"
    assert media.detect_kind(".jpg") == "document"           # 只有扩展名没有名字


def test_detect_kind_never_raises():
    for bad in (None, 123, b"a.jpg", [], {}, "../a.jpg", "a" * 5000 + ".jpg"):
        out = media.detect_kind(bad)                          # type: ignore[arg-type]
        assert out in ("text", "photo", "video", "document")
        assert media.detect_kind("a.jpg", bad) in ("text", "photo", "video", "document")


# ══════════════════════════════════════════════════════════
# validate：边界（刚好等于上限通过 / 超 1 字节失败）
# ══════════════════════════════════════════════════════════

def test_validate_image_exact_limit_passes(tmp_path):
    p = make_file(tmp_path / "exact.jpg", media.IMAGE_MAX_BYTES)
    ok, reason = media.validate(p)
    assert ok is True, reason
    assert "校验通过" in reason


def test_validate_image_one_byte_over_fails(tmp_path):
    p = make_file(tmp_path / "over.jpg", media.IMAGE_MAX_BYTES + 1)
    ok, reason = media.validate(p)
    assert ok is False
    assert "过大" in reason and str(media.IMAGE_MAX_BYTES) in reason


def test_validate_gif_exact_limit_passes_one_over_fails(tmp_path):
    exact = make_file(tmp_path / "exact.gif", media.GIF_MAX_BYTES)
    over = make_file(tmp_path / "over.gif", media.GIF_MAX_BYTES + 1)
    ok, reason = media.validate(exact)
    assert ok is True, reason
    ok2, reason2 = media.validate(over)
    assert ok2 is False and "过大" in reason2


def test_validate_video_exact_size_limit_passes_one_over_fails(tmp_path, monkeypatch):
    """视频 512MB 太大 —— 用假文件 + monkeypatch 尺寸，测的仍是**真边界比较**。"""
    p = make_file(tmp_path / "v.mp4", 16)
    monkeypatch.setattr(media, "_size_of", lambda _p: media.VIDEO_MAX_BYTES)
    monkeypatch.setattr(media, "probe_video", lambda _p: (10.0, ""))
    ok, reason = media.validate(p)
    assert ok is True, reason

    monkeypatch.setattr(media, "_size_of", lambda _p: media.VIDEO_MAX_BYTES + 1)
    ok2, reason2 = media.validate(p)
    assert ok2 is False and "过大" in reason2


def test_validate_photo_exact_size_boundary_via_real_file(tmp_path):
    """图片真文件边界：恰好 5MB 过 / 5MB+1 拒（真实 read size，无 monkeypatch）。"""
    exact = make_file(tmp_path / "b1.png", media.IMAGE_MAX_BYTES)
    over = make_file(tmp_path / "b2.png", media.IMAGE_MAX_BYTES + 1)
    assert exact.stat().st_size == media.IMAGE_MAX_BYTES
    assert over.stat().st_size == media.IMAGE_MAX_BYTES + 1
    assert media.validate(exact)[0] is True
    assert media.validate(over)[0] is False


# ══════════════════════════════════════════════════════════
# validate：视频时长
# ══════════════════════════════════════════════════════════

def test_validate_video_over_duration_fails(tmp_path, monkeypatch):
    p = make_file(tmp_path / "long.mp4", 1024)
    monkeypatch.setattr(media, "probe_video", lambda _p: (media.VIDEO_MAX_SECONDS + 0.1, ""))
    ok, reason = media.validate(p)
    assert ok is False
    assert "过长" in reason and "140" in reason


def test_validate_video_exact_duration_passes(tmp_path, monkeypatch):
    p = make_file(tmp_path / "edge.mp4", 1024)
    monkeypatch.setattr(media, "probe_video", lambda _p: (float(media.VIDEO_MAX_SECONDS), ""))
    ok, reason = media.validate(p)
    assert ok is True, reason


def test_validate_video_unknown_duration_passes_with_note(tmp_path, monkeypatch):
    p = make_file(tmp_path / "unk.mkv", 1024)
    monkeypatch.setattr(media, "probe_video", lambda _p: (0.0, "没装 ffprobe"))
    ok, reason = media.validate(p)
    assert ok is True
    assert "时长未知" in reason


# ══════════════════════════════════════════════════════════
# validate：其它失败路径
# ══════════════════════════════════════════════════════════

def test_validate_missing_file(tmp_path):
    ok, reason = media.validate(tmp_path / "nope.jpg")
    assert ok is False and "不存在" in reason


def test_validate_directory(tmp_path):
    ok, reason = media.validate(tmp_path)
    assert ok is False and "目录" in reason


def test_validate_empty_file(tmp_path):
    p = tmp_path / "empty.jpg"
    p.write_bytes(b"")
    ok, reason = media.validate(p)
    assert ok is False and "空" in reason


def test_validate_never_raises():
    for bad in (None, "", 42, [], object()):
        ok, reason = media.validate(bad)                      # type: ignore[arg-type]
        assert ok is False and isinstance(reason, str) and reason


def test_validate_kind_override(tmp_path):
    p = make_file(tmp_path / "looks_like_video.mp4", 2048)
    # kind 显式传 photo 时不应去探时长
    ok, reason = media.validate(p, "photo")
    assert ok is True and "时长" not in reason


# ══════════════════════════════════════════════════════════
# 目录穿越攻击
# ══════════════════════════════════════════════════════════

TRAVERSAL = [
    "../../evil.jpg",
    "..\\evil.jpg",
    "../evil.jpg",
    "..%2Fevil.jpg",
    "/etc/passwd",
    "/absolute/home/user/evil.jpg",
    "C:\\Windows\\evil.jpg",
    "C:/Windows/evil.jpg",
    "\\\\server\\share\\evil.jpg",
    "....//....//evil.jpg",
    "sub/../../evil.jpg",
    "./../evil.jpg",
    "a\x00b.jpg",
]


@pytest.mark.parametrize("attack", TRAVERSAL)
def test_save_upload_blocks_traversal(tmp_path, attack):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    parent_before = {p.name for p in tmp_path.iterdir()}

    ok, reason, rel = media.save_upload(attack, b"PAYLOAD", media_dir)
    assert ok is True, (attack, reason)
    # 相对名必须裸名（无任何路径分隔符 / 盘符 / 上跳）
    assert rel and "/" not in rel and "\\" not in rel and ":" not in rel
    assert ".." not in rel
    # 解析后必须以 media_dir 为前缀
    landed = (media_dir / rel).resolve()
    assert landed.is_file()
    assert str(landed).startswith(str(media_dir.resolve()) + os.sep), (attack, landed)
    # 绝不能在 media_dir 之外落地
    assert str(landed.parent.resolve()) == str(media_dir.resolve())
    parent_after = {p.name for p in tmp_path.iterdir()}
    assert parent_after == parent_before, (attack, parent_after - parent_before)


def test_save_upload_traversal_keeps_basename_only(tmp_path):
    for i, (attack, expect_payload_inside) in enumerate(
            (("../../evil.jpg", "evil.jpg"),
             ("/etc/passwd", "passwd"),
             ("C:\\Windows\\evil.jpg", "evil.jpg"))):
        media_dir = tmp_path / f"media{i}"          # 每次用新目录，避免重名后缀干扰
        ok, _reason, rel = media.save_upload(attack, b"X", media_dir)
        assert ok is True
        assert rel == expect_payload_inside, (attack, rel)
        assert (media_dir / rel).resolve().parent == media_dir.resolve()


def test_save_upload_traversal_writes_nothing_outside(tmp_path):
    media_dir = tmp_path / "deep" / "media"
    media_dir.mkdir(parents=True)
    ok, _r, rel = media.save_upload("../../../../../../tmp/owned.jpg", b"X", media_dir)
    assert ok is True
    assert not (tmp_path / "owned.jpg").exists()
    assert not (tmp_path / "deep" / "owned.jpg").exists()
    assert (media_dir / rel).is_file()


def test_sanitize_filename_never_raises_and_is_flat():
    for bad in (None, "", ".", "..", "/", "\\", "C:", "\x00", "a" * 400,
                'a<b>c:d"e|f?g*h.jpg', "con.jpg", "NUL.txt", "....", "  ...  ",
                "中文 名字.jpg", b"bytes.jpg", 12345):
        out = media.sanitize_filename(bad)                    # type: ignore[arg-type]
        assert isinstance(out, str) and out
        assert "/" not in out and "\\" not in out and ".." not in out
        assert not any(c in '<>:"|?*' for c in out)
        assert not any(ord(c) < 32 or ord(c) == 127 for c in out)
        assert len(out) <= media._MAX_NAME_LEN


def test_sanitize_strips_control_and_illegal_chars(tmp_path):
    assert media.sanitize_filename("a\x00b\x01c\x1f.jpg") == "abc.jpg"
    assert media.sanitize_filename('a<b>c:d"e|f?g*h.jpg').endswith(".jpg")
    assert not any(c in media.sanitize_filename('a<b>c:d"e|f?g*h.jpg') for c in '<>:"|?*')
    assert media.sanitize_filename("CON.jpg") == "_CON.jpg"
    assert media.sanitize_filename("nul.txt") == "_nul.txt"
    assert media.sanitize_filename("trailing. ") == "trailing"


def test_sanitize_keeps_length_and_extension():
    out = media.sanitize_filename("x" * 300 + ".jpeg")
    assert len(out) <= media._MAX_NAME_LEN and out.endswith(".jpeg")


# ══════════════════════════════════════════════════════════
# save_upload：重名不覆盖 / 其它路径
# ══════════════════════════════════════════════════════════

def test_save_upload_does_not_overwrite_same_name(tmp_path):
    media_dir = tmp_path / "media"
    ok1, e1, rel1 = media.save_upload("dup.jpg", b"FIRST", media_dir)
    ok2, e2, rel2 = media.save_upload("DUP.jpg", b"SECOND", media_dir)
    ok3, e3, rel3 = media.save_upload("dup.jpg", b"THIRD", media_dir)
    assert (ok1, ok2, ok3) == (True, True, True), (e1, e2, e3)
    assert len({rel1, rel2, rel3}) == 3
    assert (media_dir / rel1).read_bytes() == b"FIRST"        # 第一份没被覆盖
    assert (media_dir / rel2).read_bytes() == b"SECOND"
    assert (media_dir / rel3).read_bytes() == b"THIRD"
    assert sorted(p.name for p in media_dir.iterdir()) == sorted([rel1, rel2, rel3])


def test_save_upload_creates_media_dir(tmp_path):
    target = tmp_path / "not" / "yet" / "media"
    ok, reason, rel = media.save_upload("a.jpg", b"X", target)
    assert ok is True, reason
    assert (target / rel).is_file()


def test_save_upload_accepts_bytes_like(tmp_path):
    media_dir = tmp_path / "media"
    ok, reason, rel = media.save_upload("a.jpg", bytearray(b"XY"), media_dir)
    assert ok is True, reason
    assert (media_dir / rel).read_bytes() == b"XY"
    ok2, _r2, rel2 = media.save_upload("b.jpg", memoryview(b"Z"), media_dir)
    assert ok2 is True and (media_dir / rel2).read_bytes() == b"Z"


def test_save_upload_rejects_empty_and_bad_data(tmp_path):
    media_dir = tmp_path / "media"
    ok, reason, rel = media.save_upload("a.jpg", b"", media_dir)
    assert ok is False and rel == "" and "空" in reason
    ok2, reason2, rel2 = media.save_upload("a.jpg", None, media_dir)   # type: ignore[arg-type]
    assert ok2 is False and rel2 == ""
    ok3, reason3, rel3 = media.save_upload("a.jpg", object(), media_dir)  # type: ignore[arg-type]
    assert ok3 is False and rel3 == ""
    assert not media_dir.exists() or not list(media_dir.iterdir())


def test_save_upload_enforces_byte_limit(tmp_path):
    media_dir = tmp_path / "media"
    make_file(tmp_path / "dummy", 0)
    ok, reason, rel = media.save_upload("big.jpg", b"\0" * (media.IMAGE_MAX_BYTES + 1), media_dir)
    assert ok is False and rel == "" and "过大" in reason
    # 刚好等于上限应通过
    ok2, reason2, rel2 = media.save_upload("edge.jpg", b"\0" * media.IMAGE_MAX_BYTES, media_dir)
    assert ok2 is True, reason2


def test_save_upload_never_raises_on_hostile_input(tmp_path):
    media_dir = tmp_path / "media"
    for name in (None, "", "..", "/", "\\", "C:", 123, [1], {"a": 1}, "a" * 999 + ".jpg"):
        ok, reason, rel = media.save_upload(name, b"X", media_dir)   # type: ignore[arg-type]
        assert isinstance(ok, bool) and isinstance(reason, str) and isinstance(rel, str)
        if ok:
            assert (media_dir / rel).resolve().parent == media_dir.resolve()
    # media_dir 非法（指向已有文件）也不抛
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"x")
    ok, reason, rel = media.save_upload("a.jpg", b"X", blocker / "sub")
    assert ok is False and rel == ""


def test_save_upload_default_dir_is_under_data():
    d = media.default_media_dir()
    assert isinstance(d, Path) and d.name == "media"


# ══════════════════════════════════════════════════════════
# probe_video：垃圾数据 / 空文件 / 缺失 / mvhd 降级
# ══════════════════════════════════════════════════════════

def test_probe_video_garbage_returns_zero(tmp_path):
    p = tmp_path / "garbage.mp4"
    p.write_bytes(b"this is definitely not a video" * 100)
    dur, err = media.probe_video(p)
    assert dur == 0.0
    assert isinstance(err, str) and err


def test_probe_video_random_binary_returns_zero(tmp_path):
    p = tmp_path / "rand.mov"
    p.write_bytes(bytes(range(256)) * 40)
    dur, err = media.probe_video(p)
    assert dur == 0.0 and isinstance(err, str) and err


def test_probe_video_empty_file_returns_zero(tmp_path):
    p = tmp_path / "empty.mp4"
    p.write_bytes(b"")
    dur, err = media.probe_video(p)
    assert dur == 0.0
    assert "空" in err


def test_probe_video_missing_and_dir(tmp_path):
    dur, err = media.probe_video(tmp_path / "nope.mp4")
    assert dur == 0.0 and "不存在" in err
    dur2, err2 = media.probe_video(tmp_path)
    assert dur2 == 0.0 and "目录" in err2


def test_probe_video_never_raises(tmp_path):
    for bad in (None, "", 12, [], {}, b"x", "\x00\x01\x02"):
        dur, err = media.probe_video(bad)                     # type: ignore[arg-type]
        assert dur == 0.0 and isinstance(err, str)


def test_probe_video_malformed_box_sizes(tmp_path):
    """畸形 box 长度（0xFFFFFFFF / 巨大 size / 截断）不能挂住或抛异常。"""
    cases = [
        struct.pack(">I", 0xFFFFFFFF) + b"mvhd" + b"\0" * 32,
        struct.pack(">I", 1) + b"moov" + struct.pack(">Q", 0xFFFFFFFFFFFFFFFF) + b"\0" * 16,
        struct.pack(">I", 8) + b"moov",                       # 声明有 moov 但没内容
        struct.pack(">I", 0) + b"moov",                       # size=0 延伸到结尾
        b"\x00\x00\x00\x08free" * 3,
    ]
    for i, blob in enumerate(cases):
        p = tmp_path / f"mal{i}.mp4"
        p.write_bytes(blob)
        dur, err = media.probe_video(p)
        assert dur == 0.0 and isinstance(err, str) and err


def test_probe_video_reads_mvhd_when_ffprobe_unavailable(tmp_path, monkeypatch):
    """ffprobe 不可用（本机默认）时，mvhd 降级必须能读出正确时长。"""
    monkeypatch.setattr(media, "_ffprobe_exe", lambda: None)
    p = fake_mp4(tmp_path / "v.mp4", 5.0)
    dur, err = media.probe_video(p)
    assert err == "", (dur, err)
    assert abs(dur - 5.0) < 0.001


def test_probe_video_reads_mvhd_v1_and_big_duration(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "_ffprobe_exe", lambda: None)
    p1 = fake_mp4(tmp_path / "v1.mp4", 3.5, version=1)
    dur1, err1 = media.probe_video(p1)
    assert err1 == "" and abs(dur1 - 3.5) < 0.001, (dur1, err1)

    p2 = fake_mp4(tmp_path / "long.mp4", 200.0)
    dur2, err2 = media.probe_video(p2)
    assert err2 == "" and abs(dur2 - 200.0) < 0.001
    # 200 秒的视频走 validate 必须被拒
    ok, reason = media.validate(p2)
    assert ok is False and "过长" in reason


def test_probe_video_ffprobe_failure_falls_back_to_mvhd(tmp_path, monkeypatch):
    """ffprobe 存在但失败/超时 → 必须降级，不得直接失败。"""
    def boom(*_a, **_k):
        raise media.subprocess.TimeoutExpired(cmd="ffprobe", timeout=1)
    monkeypatch.setattr(media, "_ffprobe_exe", lambda: "C:/fake/ffprobe.exe")
    monkeypatch.setattr(media.subprocess, "run", boom)
    p = fake_mp4(tmp_path / "v.mp4", 7.0)
    dur, err = media.probe_video(p)
    assert err == "" and abs(dur - 7.0) < 0.001, (dur, err)


def test_probe_video_mvhd_zero_duration_reports_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "_ffprobe_exe", lambda: None)
    p = fake_mp4(tmp_path / "zero.mp4", 0.0)
    dur, err = media.probe_video(p)
    assert dur == 0.0 and err


def test_ffprobe_is_not_a_hard_dependency():
    """ffprobe 缺失时模块与全部契约函数照常可用。"""
    assert media._ffprobe_exe() is None or isinstance(media._ffprobe_exe(), str)
    importlib_reload_still_importable = callable(media.probe_video)
    assert importlib_reload_still_importable is True
    # 本测试机没装 ffprobe：直接确认降级路径存在
    if media._ffprobe_exe() is None:
        assert callable(media._duration_from_mp4)


# ══════════════════════════════════════════════════════════
# summarize / human_size
# ══════════════════════════════════════════════════════════

def test_summarize_shape(tmp_path):
    p = make_file(tmp_path / "pic.jpg", 1024)
    s = media.summarize(p)
    assert set(s) == {"name", "kind", "bytes", "human_size", "duration", "ok", "reason"}
    assert s["name"] == "pic.jpg"
    assert s["kind"] == "photo"
    assert s["bytes"] == 1024
    assert s["human_size"] == "1.0 KB"
    assert s["duration"] == 0.0
    assert s["ok"] is True


def test_summarize_video_has_duration(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "_ffprobe_exe", lambda: None)
    p = fake_mp4(tmp_path / "clip.mp4", 12.5)
    s = media.summarize(p)
    assert s["kind"] == "video" and s["ok"] is True
    assert abs(s["duration"] - 12.5) < 0.001


def test_summarize_bad_paths_do_not_raise(tmp_path):
    for bad in (tmp_path / "missing.jpg", None, 123, ""):
        s = media.summarize(bad)                              # type: ignore[arg-type]
        assert s["ok"] is False and isinstance(s["reason"], str) and s["reason"]
        assert set(s) == {"name", "kind", "bytes", "human_size", "duration", "ok", "reason"}


def test_human_size():
    assert media.human_size(0) == "0 B"
    assert media.human_size(999) == "999 B"
    assert media.human_size(1024) == "1.0 KB"
    assert media.human_size(5 * 1024 * 1024) == "5.0 MB"
    assert media.human_size(512 * 1024 * 1024) == "512.0 MB"
    assert media.human_size(1024 ** 4) == "1.0 TB"
    for bad in (None, "abc", [], -5, float("nan")):
        assert isinstance(media.human_size(bad), str)


def test_public_api_complete():
    for name in ("IMAGE_MAX_BYTES", "GIF_MAX_BYTES", "VIDEO_MAX_BYTES", "VIDEO_MAX_SECONDS",
                 "MAX_IMAGES", "KIND_BY_EXT", "detect_kind", "limits_for", "probe_video",
                 "validate", "save_upload", "summarize"):
        assert hasattr(media, name), name
        assert name in media.__all__, name


def test_module_does_not_import_backends():
    """零耦合：全新解释器里 `import core.media` 不得把发布后端子包拖进来
    （web 与后端都要能独立引；用子进程测，避免受本进程其它测试的 import 污染）。"""
    import subprocess
    import sys
    root = Path(media.__file__).resolve().parent.parent
    code = (
        "import sys, core.media as m;"
        "bad=[k for k in sys.modules if k.startswith('core.backends')];"
        "print('BACKENDS_LOADED=' + repr(bad))"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(root),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    out = (proc.stdout or b"").decode("utf-8", "replace")
    assert proc.returncode == 0, out + (proc.stderr or b"").decode("utf-8", "replace")
    assert "BACKENDS_LOADED=[]" in out, out


# ══════════════════════════════════════════════════════════
# 多图：pack/parse（本次新增，支撑"一条推文带多张图"）
#   队列表 media_path 是单 TEXT 字段，单图存文件名、多图存 JSON 数组。
# ══════════════════════════════════════════════════════════

def test_pack_single_stays_plain_filename():
    """单图必须保持老格式（纯文件名），保证老库/老代码兼容。"""
    assert media.pack_media_paths(["a.jpg"]) == "a.jpg"
    assert media.pack_media_paths("a.jpg") == "a.jpg"


def test_pack_multiple_becomes_json():
    got = media.pack_media_paths(["a.jpg", "b.jpg"])
    assert got.startswith("[") and "a.jpg" in got and "b.jpg" in got


def test_pack_empty():
    for v in ([], None, "", ["", None]):
        assert media.pack_media_paths(v) == ""


def test_parse_roundtrip():
    names = ["a.jpg", "b.jpg", "c.jpg"]
    assert media.parse_media_paths(media.pack_media_paths(names)) == names


def test_parse_legacy_single():
    assert media.parse_media_paths("a.jpg") == ["a.jpg"]
    assert media.parse_media_paths("") == []
    assert media.parse_media_paths(None) == []


def test_parse_comma_separated():
    """手工改库写成逗号分隔也认（容错）。"""
    assert media.parse_media_paths("a.jpg,b.jpg") == ["a.jpg", "b.jpg"]


def test_parse_broken_json_does_not_lose_content():
    """JSON 坏了也不能把内容丢掉 —— 退化成单文件名。"""
    got = media.parse_media_paths("[坏JSON")
    assert got == ["[坏JSON"]


def test_parse_never_raises_on_weird_input():
    for v in (123, {}, object(), b"bytes"):
        media.parse_media_paths(v)   # 不抛即通过


def test_trim_to_limit_keeps_first_n_and_reports_dropped():
    """超过 4 张要保留前 4 张，并**明确告知**丢了哪些（不静默）。"""
    keep, drop = media.trim_to_limit(["1.jpg", "2.jpg", "3.jpg", "4.jpg", "5.jpg"])
    assert keep == ["1.jpg", "2.jpg", "3.jpg", "4.jpg"]
    assert drop == ["5.jpg"]


def test_trim_to_limit_no_drop_when_within():
    keep, drop = media.trim_to_limit(["a.jpg", "b.jpg"])
    assert keep == ["a.jpg", "b.jpg"] and drop == []


def test_resolve_media_paths_returns_only_existing(tmp_path):
    (tmp_path / "yes.jpg").write_bytes(b"x")
    got = media.resolve_media_paths('["yes.jpg","no.jpg"]', tmp_path)
    assert [p.name for p in got] == ["yes.jpg"]


def test_resolve_media_paths_legacy_single(tmp_path):
    (tmp_path / "one.jpg").write_bytes(b"x")
    got = media.resolve_media_paths("one.jpg", tmp_path)
    assert [p.name for p in got] == ["one.jpg"]


def test_max_images_is_four():
    """X 平台限制 —— 契约冻结值，不得擅改。"""
    assert media.MAX_IMAGES == 4
