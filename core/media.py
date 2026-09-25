"""媒体校验与存储（CONTRACT_V2 §4，Agent G 归属文件）。

设计约束（全部硬性）：
  * **纯标准库**，零耦合 —— 不引入任何发布后端子包，不 import 任何第三方包。
    唯一可选的外部程序是 `ffprobe`（可选增强；缺失/失败时降级为手工解析 mp4 的
    `mvhd` box）。**ffmpeg/ffprobe 不是依赖**。
  * **对外函数绝不抛异常**：任何输入（None / 垃圾 bytes / 不存在的路径 / 目录 /
    畸形视频）都必须返回可读结果。
  * **防目录穿越**：`save_upload` 只取 basename，剔除 `..`、路径分隔符、Windows
    非法字符与控制字符，落盘前再断言解析后的路径仍在 `media_dir` 之内，
    并且用 `open(..., "xb")` 独占创建 —— 重名**绝不覆盖**。

X 平台真实限制（契约 §4.1 冻结值）：
    单图 5MB / GIF 15MB / 视频 512MB 且不超过 140 秒 / 单条最多 4 张图。
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import struct
import subprocess
from pathlib import Path

# ══════════════════════════════════════════════════════════
# 常量（X 平台真实限制 —— 契约冻结，不得改动取值）
# ══════════════════════════════════════════════════════════

IMAGE_MAX_BYTES = 5 * 1024 * 1024        # 单图 5MB
GIF_MAX_BYTES = 15 * 1024 * 1024         # GIF 15MB
VIDEO_MAX_BYTES = 512 * 1024 * 1024      # 视频 512MB
VIDEO_MAX_SECONDS = 140                  # 2 分 20 秒
MAX_IMAGES = 4                           # 单条最多 4 张图

#: 扩展名 → kind（扩展名判定优先级最高；key 一律小写含点）
KIND_BY_EXT: dict[str, str] = {
    # 图片（含动图 —— GIF 仍归 photo，但字节上限走 GIF_MAX_BYTES）
    ".jpg": "photo", ".jpeg": "photo", ".jpe": "photo", ".jfif": "photo",
    ".png": "photo", ".gif": "photo", ".webp": "photo", ".bmp": "photo",
    ".tif": "photo", ".tiff": "photo", ".heic": "photo", ".heif": "photo",
    ".avif": "photo",
    # 视频
    ".mp4": "video", ".m4v": "video", ".mov": "video", ".mkv": "video",
    ".webm": "video", ".avi": "video", ".wmv": "video", ".flv": "video",
    ".mpeg": "video", ".mpg": "video", ".3gp": "video", ".ts": "video",
    # 文档（X 普通推文并不原生支持，保留 kind 供队列/控制台复用）
    ".pdf": "document", ".doc": "document", ".docx": "document",
    ".xls": "document", ".xlsx": "document", ".ppt": "document",
    ".pptx": "document", ".txt": "document", ".md": "document",
    ".csv": "document", ".json": "document", ".xml": "document",
    ".rtf": "document", ".odt": "document", ".ods": "document",
    ".epub": "document", ".zip": "document", ".rar": "document",
    ".7z": "document", ".gz": "document", ".tar": "document",
    ".psd": "document", ".ai": "document", ".srt": "document",
}

#: mime 主类型 → kind（扩展名无法判定时用）
_KIND_BY_MIME_PREFIX = (
    ("image/", "photo"),
    ("video/", "video"),
    ("text/", "text"),
)
_KIND_BY_MIME_EXACT = {
    "application/json": "text",
    "application/xml": "text",
    "application/javascript": "text",
    "application/x-javascript": "text",
    "application/pdf": "document",
    "application/msword": "document",
    "application/zip": "document",
    "application/x-zip-compressed": "document",
    "application/octet-stream": "document",
    "application/x-msdownload": "document",
}

#: 全 kind 的别名归一表（limits_for / validate 用）
_KIND_ALIASES = {
    "photo": "photo", "image": "photo", "images": "photo", "picture": "photo",
    "pic": "photo", "gif": "gif",
    "video": "video", "movie": "video", "videos": "video",
    "text": "text", "txt": "text",
    "document": "document", "doc": "document", "file": "document",
}

#: ffprobe 子进程硬超时（秒）—— 防止畸形文件把调用方挂死
FFPROBE_TIMEOUT = 8.0

#: mp4 暴力扫描兜底的上限（字节）：正常路径走 box 跳转，不读这么多
_MP4_SCAN_BYTES = 2 * 1024 * 1024
#: box 遍历迭代硬上限（防构造出的畸形 box 链死循环）
_MAX_BOX_ITER = 4096
#: 文件名总长上限（兼容 Windows MAX_PATH 余量）
_MAX_NAME_LEN = 120
#: 重名时尝试的随机后缀次数
_MAX_NAME_TRIES = 32

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ══════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════

def default_media_dir() -> Path:
    """默认媒体目录：`core.config.MEDIA_DIR`，取不到就落 cwd/data/media。

    惰性 import —— 让本模块在无 config 的环境下也能单独引用/测试。
    """
    try:
        from core.config import MEDIA_DIR  # 局部 import：避免 import 期副作用
        return Path(MEDIA_DIR)
    except Exception:
        return Path.cwd() / "data" / "media"


def human_size(num: object) -> str:
    """字节数 → 人话（如 1310720 → '1.2 MB'）。任何输入都不抛异常。"""
    try:
        n = float(num)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "0 B"
    if n != n or n < 0:  # NaN / 负数
        n = 0.0
    units = ("B", "KB", "MB", "GB", "TB")
    i = 0
    while n >= 1024.0 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    return f"{int(n)} {units[i]}" if i == 0 else f"{n:.1f} {units[i]}"


def _suffix(filename: object) -> str:
    """安全取小写扩展名（含点）；失败返回 ''。"""
    try:
        return Path(str(filename)).suffix.lower()
    except Exception:
        return ""


def _size_of(path: object) -> int:
    """文件字节数；取不到返回 0（不抛）。测试可 monkeypatch 本函数。"""
    try:
        return int(os.path.getsize(str(path)))
    except Exception:
        return 0


def _normalize_kind(kind: object) -> str:
    """kind 归一；未知一律 document。"""
    try:
        return _KIND_ALIASES.get(str(kind or "").strip().lower(), "document")
    except Exception:
        return "document"


# ══════════════════════════════════════════════════════════
# 判定与上限
# ══════════════════════════════════════════════════════════

def detect_kind(filename: str, mime: str = "") -> str:
    """返回 text|photo|video|document。**扩展名优先，其次 mime，兜底 document。**

    - 扩展名大小写不敏感（`.JPG` / `.Mp4` 均识别）。
    - 扩展名未知时才看 mime；mime 大小写不敏感、自动去掉 `;charset=...` 参数。
    - `text/*` → `text`（契约允许的第四种 kind）；其余无法判定 → `document`。
    - 任何异常输入返回 document，绝不抛异常。
    """
    try:
        ext = _suffix(filename)
        hit = KIND_BY_EXT.get(ext)
        if hit:
            return hit
    except Exception:
        pass
    try:
        m = str(mime or "").split(";", 1)[0].strip().lower()
        if m:
            for prefix, kind in _KIND_BY_MIME_PREFIX:
                if m.startswith(prefix):
                    return kind
            hit = _KIND_BY_MIME_EXACT.get(m)
            if hit:
                return hit
            # 兜底前缀：application/* 里带 image / video 字样的（少见）
            if "image" in m:
                return "photo"
            if "video" in m:
                return "video"
    except Exception:
        pass
    return "document"


def limits_for(kind: str) -> tuple[int, int | None]:
    """返回 (字节上限, 时长上限秒或 None)。

    - `photo` → (5MB, None)
    - `gif`   → (15MB, None) —— 非 detect_kind 的返回值，供调用方显式按 GIF 校验
    - `video` → (512MB, 140)
    - `document` / `text` / 未知 → (5MB, None)（保守取图片上限）

    注意：GIF 也可以走 `photo` —— `validate()` / `byte_limit_for()` 会按 `.gif`
    扩展名自动换成 15MB，不需要调用方关心。
    """
    k = _normalize_kind(kind)
    if k == "gif":
        return GIF_MAX_BYTES, None
    if k == "video":
        return VIDEO_MAX_BYTES, VIDEO_MAX_SECONDS
    return IMAGE_MAX_BYTES, None


def byte_limit_for(filename: str, kind: str = "") -> int:
    """按「文件名扩展名 + kind」决定字节上限（GIF 单独放宽到 15MB）。"""
    try:
        if _suffix(filename) == ".gif":
            return GIF_MAX_BYTES
        return limits_for(kind or detect_kind(filename))[0]
    except Exception:
        return IMAGE_MAX_BYTES


# ══════════════════════════════════════════════════════════
# 视频时长：ffprobe 优先，mp4 mvhd 降级
# ══════════════════════════════════════════════════════════

def _ffprobe_exe() -> str | None:
    """ffprobe 可执行文件路径；不存在返回 None（**不是错误**）。"""
    try:
        return shutil.which("ffprobe")
    except Exception:
        return None


def _ffprobe_duration(path: Path, timeout: float) -> tuple[float | None, str]:
    """调 ffprobe 取时长。返回 (秒|None, 原因)。绝不抛异常、绝不无限等待。"""
    exe = _ffprobe_exe()
    if not exe:
        return None, "未找到 ffprobe（降级手工解析）"
    cmd = [
        exe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        proc = subprocess.run(  # noqa: S603 - 固定参数列表，无 shell
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            timeout=max(0.5, float(timeout)),
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return None, f"ffprobe 超时（>{timeout:g}s）"
    except Exception as e:
        return None, f"ffprobe 调用失败：{type(e).__name__}"
    if proc.returncode != 0:
        err = (proc.stderr or b"")[:200].decode("utf-8", "replace").strip()
        return None, f"ffprobe 退出码 {proc.returncode}{('：' + err) if err else ''}"
    try:
        info = json.loads((proc.stdout or b"").decode("utf-8", "replace") or "{}")
    except Exception:
        return None, "ffprobe 输出不是合法 JSON"
    dur = _pick_duration(info)
    if dur is None:
        return None, "ffprobe 未给出 duration"
    return dur, ""


def _pick_duration(info: object) -> float | None:
    """从 ffprobe json 里挑时长：format.duration 优先，其次各 stream 最大值。"""
    best: float | None = None
    try:
        if isinstance(info, dict):
            fmt = info.get("format")
            if isinstance(fmt, dict):
                best = _to_float(fmt.get("duration"))
            streams = info.get("streams")
            if isinstance(streams, list):
                for st in streams:
                    if not isinstance(st, dict):
                        continue
                    d = _to_float(st.get("duration"))
                    if d is not None and (best is None or d > best):
                        best = d
    except Exception:
        return None
    if best is not None and best > 0:
        return best
    return None


def _to_float(v: object) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
        return f if f == f else None  # 过滤 NaN
    except (TypeError, ValueError):
        return None


def _parse_mvhd(payload: bytes) -> float:
    """解析 `mvhd` box 负载 → 秒。失败返回 0.0。"""
    try:
        if len(payload) < 20:
            return 0.0
        version = payload[0]
        if version == 1:
            if len(payload) < 32:
                return 0.0
            timescale = struct.unpack(">I", payload[20:24])[0]
            duration = struct.unpack(">Q", payload[24:32])[0]
        else:
            timescale = struct.unpack(">I", payload[12:16])[0]
            duration = struct.unpack(">I", payload[16:20])[0]
        if timescale <= 0 or duration <= 0:
            return 0.0
        return duration / float(timescale)
    except Exception:
        return 0.0


def _walk_boxes(fh, start: int, end: int, depth: int = 0) -> float:
    """在 [start, end) 区间内按 box 跳转找 mvhd/moov（深度受限、迭代受限）。"""
    if depth > 6:
        return 0.0
    offset = start
    for _ in range(_MAX_BOX_ITER):
        if offset + 8 > end:
            return 0.0
        try:
            fh.seek(offset)
            hdr = fh.read(8)
        except Exception:
            return 0.0
        if len(hdr) < 8:
            return 0.0
        try:
            size = struct.unpack(">I", hdr[0:4])[0]
        except Exception:
            return 0.0
        btype = hdr[4:8]
        hlen = 8
        if size == 1:                        # 64 位长度
            try:
                ext = fh.read(8)
            except Exception:
                return 0.0
            if len(ext) < 8:
                return 0.0
            try:
                size = struct.unpack(">Q", ext)[0]
            except Exception:
                return 0.0
            hlen = 16
        elif size == 0:                      # 延伸到区间末尾
            size = end - offset
        if size < hlen or offset + size > end:
            return 0.0                       # 畸形 box：直接放弃
        if btype == b"mvhd":
            try:
                fh.seek(offset + hlen)
                payload = fh.read(min(64, size - hlen))
            except Exception:
                return 0.0
            d = _parse_mvhd(payload)
            if d > 0:
                return d
        elif btype == b"moov":
            d = _walk_boxes(fh, offset + hlen, offset + size, depth + 1)
            if d > 0:
                return d
        offset += size
    return 0.0


def _duration_from_mp4(path: Path) -> tuple[float, str]:
    """手工解析 mp4：box 跳转找 moov/mvhd；失败再在头部做有界暴力扫描。"""
    try:
        total = _size_of(path)
    except Exception:
        total = 0
    if total <= 0:
        return 0.0, "无法读取文件大小"
    try:
        with open(path, "rb") as fh:
            d = _walk_boxes(fh, 0, total)
            if d > 0:
                return d, ""
            # 兜底：有些文件 box 头被破坏/moov 不在常规位置 —— 有界扫描 mvhd
            fh.seek(0)
            head = fh.read(min(total, _MP4_SCAN_BYTES))
    except OSError as e:
        return 0.0, f"打开失败：{type(e).__name__}"
    except Exception as e:
        return 0.0, f"解析失败：{type(e).__name__}"
    idx = head.find(b"mvhd")
    if idx >= 0:
        d = _parse_mvhd(head[idx + 4: idx + 4 + 64])
        if d > 0:
            return d, ""
    return 0.0, "未找到可用的 mvhd 时长（非 mp4 或时长字段为 0）"


def probe_video(path: str | Path) -> tuple[float, str]:
    """读视频时长(秒)。返回 `(duration, err)`；测不出时 `(0.0, 原因)`。

    策略（**绝不抛异常**）：
      1. 文件存在性/类型检查；
      2. `ffprobe -print_format json`（带硬超时，畸形文件也不会挂住）；
      3. ffprobe 不存在 / 失败 / 超时 → 手工解析 mp4 的 `moov/mvhd` box；
      4. 都不行 → `(0.0, 原因)`。

    限制说明：ffprobe 缺失时只认 mp4（含 m4v/mov 的 box 结构）；mkv/webm 的时长
    解析未实现 —— 那种情况返回 0.0，`validate()` 会放行但附带"时长未知"提示。
    """
    try:
        p = Path(str(path))
    except Exception as e:
        return 0.0, f"非法路径：{type(e).__name__}"
    try:
        if not p.exists():
            return 0.0, f"文件不存在：{p}"
        if p.is_dir():
            return 0.0, f"路径是目录，不是文件：{p}"
        if _size_of(p) <= 0:
            return 0.0, "文件为空（0 字节）"
    except Exception as e:
        return 0.0, f"路径检查失败：{type(e).__name__}"

    why: list[str] = []
    try:
        dur, err = _ffprobe_duration(p, FFPROBE_TIMEOUT)
        if dur is not None and dur > 0:
            return float(dur), ""
        why.append(err or "ffprobe 未给出时长")
    except Exception as e:                    # 最后一道防线
        why.append(f"ffprobe 异常：{type(e).__name__}")

    try:
        dur, err = _duration_from_mp4(p)
        if dur > 0:
            return float(dur), ""
        why.append(err or "mvhd 解析失败")
    except Exception as e:
        why.append(f"mvhd 解析异常：{type(e).__name__}")

    return 0.0, "；".join(x for x in why if x) or "无法测出时长"


# ══════════════════════════════════════════════════════════
# 校验
# ══════════════════════════════════════════════════════════

def validate(path: str | Path, kind: str = "") -> tuple[bool, str]:
    """校验单个媒体是否符合 X 限制。返回 `(ok, 可读原因)`。

    - `kind` 为空则自动 `detect_kind(path)`；未知 kind 归一为 document。
    - 不存在 / 不是文件 / 0 字节 / 超字节上限 / 视频超时长 → `(False, 中文原因)`。
    - **边界取闭区间**：字节数 `刚好等于` 上限通过，超 1 字节失败。
    - 视频时长测不出（无 ffprobe 且非 mp4）→ 放行，但原因里带"时长未知"提示
      （X 侧会做最终判定）。
    """
    try:
        try:
            p = Path(str(path))
        except Exception as e:
            return False, f"非法路径：{type(e).__name__}"
        if not p.exists():
            return False, f"文件不存在：{p}"
        if p.is_dir():
            return False, f"路径是目录，不是文件：{p}"

        k = _normalize_kind(kind) if str(kind or "").strip() else _normalize_kind(detect_kind(p.name))
        _limit, max_sec = limits_for(k)
        limit = byte_limit_for(p.name, k)     # .gif → 15MB，video → 512MB，其余 5MB

        size = _size_of(p)
        if size <= 0:
            return False, "文件为空（0 字节）"
        if size > limit:
            return False, (
                f"文件过大：{human_size(size)}（{size} 字节）超过 {k} 上限 "
                f"{human_size(limit)}（{limit} 字节）"
            )

        if k == "video" and max_sec:
            dur, err = probe_video(p)
            if dur > max_sec:
                return False, (
                    f"视频过长：{dur:.1f} 秒 超过上限 {max_sec} 秒（{max_sec // 60} 分 "
                    f"{max_sec % 60} 秒）"
                )
            if dur <= 0:
                return True, (
                    f"校验通过：{k} {human_size(size)} / 上限 {human_size(limit)}"
                    f"（时长未知：{err or '未能测出'} —— 发布时由 X 侧最终判定）"
                )
            return True, (
                f"校验通过：{k} {human_size(size)} / 上限 {human_size(limit)}，"
                f"时长 {dur:.1f}s / 上限 {max_sec}s"
            )

        return True, f"校验通过：{k} {human_size(size)} / 上限 {human_size(limit)}"
    except Exception as e:                    # 契约：绝不抛异常
        return False, f"校验失败（内部错误）：{type(e).__name__}: {e}"


# ══════════════════════════════════════════════════════════
# 上传落盘（防目录穿越 / 防覆盖）
# ══════════════════════════════════════════════════════════

#: Windows 非法字符（不含路径分隔符 —— 它们先被 basename 切掉了）
_ILLEGAL_CHARS = set('<>:"|?*')
#: Windows 保留设备名（不区分大小写，带扩展名也算）
_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def sanitize_filename(filename: object, fallback: str = "upload.bin") -> str:
    """把任意用户文件名洗成安全 basename（**绝不抛异常**）。

    做法（顺序不能换）：
      1. 统一分隔符 → 只取最后一段（等价 basename），顺手砍掉 `C:` 盘符；
      2. 逐字符剔除控制字符（<0x20、0x7F）与 Windows 非法字符 `<>:"|?*`；
      3. 删除一切 `..` 序列、去掉首尾空白与结尾的点/空格；
      4. 长度截断（保留扩展名）；空/`.`/`..`/保留设备名 → 走 fallback/加前缀。
    """
    try:
        raw = filename if isinstance(filename, str) else str(filename if filename is not None else "")
    except Exception:
        raw = ""
    try:
        name = raw.strip().strip('"').strip("'").replace("\\", "/")
        name = name.split("/")[-1]                 # basename（两种分隔符都吃）
        if ":" in name:                            # 去掉盘符 C:/./ ADS 之类
            name = name.split(":")[-1]
        cleaned = []
        for ch in name:
            o = ord(ch)
            if o < 32 or o == 127:                 # 控制字符
                continue
            if ch in _ILLEGAL_CHARS:
                continue
            cleaned.append(ch)
        name = "".join(cleaned)
        while ".." in name:                        # 剔除穿越序列
            name = name.replace("..", "")
        name = name.replace("\x00", "").strip()
        name = name.rstrip(". ")                   # Windows 结尾点/空格非法
        if name in ("", ".", "..") or set(name) <= {"."}:
            name = fallback
        # 长度截断（保留扩展名）
        if len(name) > _MAX_NAME_LEN:
            stem, dot, ext = name.rpartition(".")
            if dot and len(ext) <= 12:
                name = stem[: _MAX_NAME_LEN - len(ext) - 1] + "." + ext
            else:
                name = name[:_MAX_NAME_LEN]
        # Windows 保留设备名
        if name.split(".")[0].lower() in _RESERVED_NAMES:
            name = "_" + name
        return name or fallback
    except Exception:
        return fallback


def _is_within(child: Path, parent: Path) -> bool:
    """child 解析后是否仍在 parent 之内（跨盘/异常一律 False）。"""
    try:
        c = os.path.normcase(os.path.abspath(str(child)))
        p = os.path.normcase(os.path.abspath(str(parent)))
        return os.path.commonpath([c, p]) == p
    except Exception:
        return False


def save_upload(filename: str, data: bytes, media_dir: str | Path | None = None) -> tuple[bool, str, str]:
    """把上传的字节存进 `media_dir`。返回 `(ok, 错误原因, 相对文件名)`。

    相对文件名 = 落盘 basename，可直接写进 `Job.media_path`（相对 MEDIA_DIR）。

    安全保证（契约 §4.2）：
      * 只取 basename，剔除 `..`、`/`、`\\`、`<>:"|?*` 与控制字符；
      * 落盘前断言 `resolve()` 后仍在 `media_dir` 之内，越界直接拒绝；
      * `open(..., "xb")` 独占创建 —— 重名加短随机后缀，**绝不覆盖**已有文件；
      * 超字节上限 / 空数据 / 目录不可写 → `(False, 中文原因, "")`，绝不抛异常。
    """
    try:
        if data is None:
            return False, "没有收到文件内容", ""
        if isinstance(data, (bytearray, memoryview)):
            try:
                data = bytes(data)
            except Exception:
                return False, "文件内容类型不支持", ""
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        if not isinstance(data, (bytes, bytearray)):
            return False, f"文件内容类型不支持：{type(data).__name__}", ""
        payload = bytes(data)
        if not payload:
            return False, "文件内容为空（0 字节）", ""

        safe = sanitize_filename(filename)

        # 字节上限（GIF 15MB / 图片 5MB / 视频 512MB 等）
        limit = byte_limit_for(safe)
        if len(payload) > limit:
            return False, (
                f"文件过大：{human_size(len(payload))}（{len(payload)} 字节）超过上限 "
                f"{human_size(limit)}（{limit} 字节）"
            ), ""

        try:
            d = Path(media_dir) if media_dir else default_media_dir()
            d = d.expanduser()
            d.mkdir(parents=True, exist_ok=True)
            root = d.resolve()
        except Exception as e:
            return False, f"媒体目录不可用：{type(e).__name__}: {e}", ""

        cand = root / safe
        if not _is_within(cand, root):
            return False, f"非法文件名（疑似目录穿越）：{filename!r}", ""

        stem, ext = os.path.splitext(safe)
        stem = stem or "upload"
        for attempt in range(_MAX_NAME_TRIES):
            if attempt == 0:
                target = cand
            else:
                token = secrets.token_hex(3)        # 6 位十六进制短随机后缀
                target = root / f"{stem}_{token}{ext}"
            if not _is_within(target, root):        # 双保险
                return False, "非法文件名（疑似目录穿越）", ""
            try:
                with open(target, "xb") as fh:      # 独占创建：不覆盖任何已有文件
                    fh.write(payload)
            except FileExistsError:
                continue                            # 重名 → 换随机后缀重试
            except OSError as e:
                return False, f"写入失败：{type(e).__name__}: {e}", ""
            except Exception as e:
                return False, f"写入失败：{type(e).__name__}: {e}", ""
            return True, "", target.name
        return False, f"文件名冲突次数过多（>{_MAX_NAME_TRIES}），请改名后重试", ""
    except Exception as e:                          # 契约：绝不抛异常
        return False, f"保存失败（内部错误）：{type(e).__name__}: {e}", ""


# ══════════════════════════════════════════════════════════
# 控制台摘要
# ══════════════════════════════════════════════════════════

def summarize(path: str | Path) -> dict:
    """给控制台展示用：`{name, kind, bytes, human_size, duration, ok, reason}`。

    任何输入都返回同构 dict（不存在/异常时 ok=False + reason 说明），绝不抛异常。
    """
    out = {
        "name": "",
        "kind": "document",
        "bytes": 0,
        "human_size": "0 B",
        "duration": 0.0,
        "ok": False,
        "reason": "",
    }
    try:
        try:
            p = Path(str(path))
        except Exception as e:
            out["reason"] = f"非法路径：{type(e).__name__}"
            return out
        out["name"] = p.name or str(path)
        out["kind"] = _normalize_kind(detect_kind(p.name))
        size = _size_of(p)
        out["bytes"] = size
        out["human_size"] = human_size(size)
        if out["kind"] == "video":
            try:
                dur, _ = probe_video(p)
                out["duration"] = float(dur or 0.0)
            except Exception:
                out["duration"] = 0.0
        ok, reason = validate(p, out["kind"])
        out["ok"] = bool(ok)
        out["reason"] = str(reason)
    except Exception as e:
        out["ok"] = False
        out["reason"] = f"汇总失败（内部错误）：{type(e).__name__}: {e}"
    return out


# ══════════════════════════════════════════════════════════
# 多图支持（一条推文带多张图）
# ══════════════════════════════════════════════════════════
#
# 背景：队列表的 `media_path` 是**单个 TEXT 字段**（core/queue.py 属冻结文件，
# 不改结构）。老数据存单个文件名；多图时存 JSON 数组字符串：
#
#     "Capture001.png"                       <- 单图（老格式，保持兼容）
#     '["a.jpg","b.jpg","c.jpg"]'            <- 多图（新格式）
#
# 解析一律走 `parse_media_paths()`，写入一律走 `pack_media_paths()`，
# 这样老库、新库、两种格式混用都不会出错。

def pack_media_paths(paths: object) -> str:
    """把文件名列表打包成 `media_path` 字段的值。

    * 空 -> `""`
    * 单个 -> 直接存文件名（**与老格式完全一致**，便于降级和肉眼排查）
    * 多个 -> JSON 数组字符串
    """
    try:
        if paths is None:
            return ""
        if isinstance(paths, (str, Path)):
            items = [str(paths)]
        else:
            # ⚠ 不能直接 str(x)：None 会变成字符串 "None" 混进去
            items = [str(x) for x in paths if x is not None]
        items = [x for x in items if x and x != "None"]
        if not items:
            return ""
        if len(items) == 1:
            return items[0]
        return json.dumps(items, ensure_ascii=False)
    except Exception:
        return ""


def parse_media_paths(stored: object) -> list[str]:
    """把 `media_path` 字段解析成文件名列表。**任何输入都不抛异常。**

    兼容三种历史形态：
      * `""` / None            -> []
      * `"a.jpg"`              -> ["a.jpg"]（单图老格式）
      * `'["a.jpg","b.jpg"]'`  -> ["a.jpg", "b.jpg"]（多图新格式）
      * `"a.jpg,b.jpg"`        -> 逗号分隔也认（手工改库容错）
    """
    try:
        if stored is None:
            return []
        if isinstance(stored, (list, tuple)):
            return [str(x) for x in stored if x]
        text = str(stored).strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                got = json.loads(text)
                if isinstance(got, list):
                    return [str(x) for x in got if x]
                return [str(got)] if got else []
            except Exception:
                # JSON 坏了也别丢内容：退化成单文件名
                return [text]
        if "," in text and not Path(text).exists():
            parts = [x.strip() for x in text.split(",") if x.strip()]
            if len(parts) > 1:
                return parts
        return [text]
    except Exception:
        return []


def resolve_media_paths(stored: object, media_dir: str | Path | None = None) -> list[Path]:
    """解析成**真实存在**的绝对路径列表（不存在/越界的丢掉）。"""
    out: list[Path] = []
    try:
        base = Path(media_dir) if media_dir is not None else default_media_dir()
        for name in parse_media_paths(stored):
            try:
                cand = Path(name)
                if not cand.is_absolute():
                    cand = base / name
                if cand.exists() and cand.is_file():
                    out.append(cand)
            except Exception:
                continue
    except Exception:
        pass
    return out


def trim_to_limit(paths: object, limit: int = MAX_IMAGES) -> tuple[list[str], list[str]]:
    """把文件名列表裁到上限。返回 (保留, 被丢弃)。

    超出 X 的 4 张上限时，**保留前 N 张并明确告知调用方丢了哪些**，
    由上层决定是提示用户还是拒绝 —— 绝不静默丢。
    """
    items = parse_media_paths(paths)
    try:
        n = max(int(limit), 1)
    except Exception:
        n = MAX_IMAGES
    if len(items) <= n:
        return items, []
    return items[:n], items[n:]


__all__ = [
    # 常量
    "IMAGE_MAX_BYTES", "GIF_MAX_BYTES", "VIDEO_MAX_BYTES", "VIDEO_MAX_SECONDS",
    "MAX_IMAGES", "KIND_BY_EXT", "FFPROBE_TIMEOUT",
    # 契约函数
    "detect_kind", "limits_for", "probe_video", "validate", "save_upload", "summarize",
    # 辅助
    "human_size", "sanitize_filename", "byte_limit_for", "default_media_dir",
    # 多图
    "pack_media_paths", "parse_media_paths", "resolve_media_paths", "trim_to_limit",
]
