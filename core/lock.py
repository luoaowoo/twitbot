"""单实例锁（冻结）—— 防止两份进程同时出队导致重复发推。

Windows: msvcrt.locking 锁的是「当前文件位置」起的 N 字节。若用 'a+b' 打开，
位置在文件末尾，两个进程各自锁到**不同偏移**，锁形同虚设 —— 必须先 seek(0)。
Linux/macOS: fcntl.flock 不受此影响，但同样 seek(0) 保持一致语义。
"""
from __future__ import annotations

import os

from . import config


def single_instance_lock() -> object | None:
    """拿到锁返回文件句柄（须保持引用），已被占用返回 None。"""
    lock_path = config.DATA_DIR / "bot.lock"
    fh = open(lock_path, "a+b")
    try:
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    try:
        host = os.uname().nodename if hasattr(os, "uname") else os.getenv("COMPUTERNAME", "?")
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} host={host}\n".encode())
        fh.flush()
    except Exception:
        pass
    return fh


def lock_holder() -> str:
    """读锁文件里的持有者信息（排障用）。"""
    p = config.DATA_DIR / "bot.lock"
    try:
        return p.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""
