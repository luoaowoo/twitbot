"""从图片字节里读出宽高（新文件）。

**为什么不用 Pillow**：只为了拿个宽高就引一个图像库不划算，
而且服务器上没装。主流格式的头部都有尺寸，直接解析就行
（PNG/JPEG/GIF/WebP 覆盖了小红书、微博、贴吧、B站图床的全部格式）。

用途：**竖图优先** —— 参考号（@YongQuan 等）发的都是竖图/方图，
横图在 X 信息流里占屏小、吃亏。发布前用它挑最「竖」的那张。
"""
from __future__ import annotations

import struct


def _png(b: bytes):
    if len(b) >= 24 and b[:8] == b"\x89PNG\r\n\x1a\n" and b[12:16] == b"IHDR":
        w, h = struct.unpack(">II", b[16:24])
        return int(w), int(h)
    return None


def _gif(b: bytes):
    if len(b) >= 10 and b[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", b[6:10])
        return int(w), int(h)
    return None


def _jpeg(b: bytes):
    if len(b) < 4 or b[:2] != b"\xff\xd8":
        return None
    i, n = 2, len(b)
    while i < n - 9:
        if b[i] != 0xFF:
            i += 1
            continue
        m = b[i + 1]
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        seg = struct.unpack(">H", b[i + 2:i + 4])[0]
        # SOF0..SOF15（除 DHT=0xC4 / JPG=0xC8 / DAC=0xCC）里带尺寸
        if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
            if i + 9 < n:
                h, w = struct.unpack(">HH", b[i + 5:i + 9])
                return int(w), int(h)
            return None
        i += 2 + seg
    return None


def _webp(b: bytes):
    if len(b) < 30 or b[:4] != b"RIFF" or b[8:12] != b"WEBP":
        return None
    fmt = b[12:16]
    try:
        if fmt == b"VP8X":
            w = 1 + int.from_bytes(b[24:27], "little")
            h = 1 + int.from_bytes(b[27:30], "little")
            return w, h
        if fmt == b"VP8 ":
            # 关键帧头：.... 9d 01 2a <w:2> <h:2>
            j = b.find(b"\x9d\x01\x2a")
            if j > 0 and j + 7 <= len(b):
                w, h = struct.unpack("<HH", b[j + 3:j + 7])
                return w & 0x3FFF, h & 0x3FFF
        if fmt == b"VP8L":
            if b[20] == 0x2F and len(b) >= 25:
                bits = int.from_bytes(b[21:25], "little")
                w = (bits & 0x3FFF) + 1
                h = ((bits >> 14) & 0x3FFF) + 1
                return w, h
    except Exception:
        return None
    return None


def size_of(data: bytes) -> tuple[int, int] | None:
    """返回 (宽, 高)；认不出来返回 None。**绝不抛异常**。"""
    if not data or len(data) < 16:
        return None
    for fn in (_png, _gif, _jpeg, _webp):
        try:
            got = fn(data)
            if got and got[0] > 0 and got[1] > 0:
                return got
        except Exception:
            continue
    return None


def is_portrait(wh: tuple[int, int] | None) -> bool:
    """竖图或方图（高 >= 宽）。认不出来当横图处理（更保守）。"""
    if not wh:
        return False
    w, h = wh
    return h >= w


def ratio_score(wh: tuple[int, int] | None) -> float:
    """越「竖」分越高；认不出来给 0。"""
    if not wh:
        return 0.0
    w, h = wh
    return h / max(1, w)
