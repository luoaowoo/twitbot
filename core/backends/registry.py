"""后端注册表（冻结）—— 惰性导入，任何后端故障不得影响其它后端可用性。"""
from __future__ import annotations

import importlib
import logging

log = logging.getLogger("twitbot.backends")

# name -> (模块路径, 类名, 中文名)
REGISTRY: dict[str, tuple[str, str, str]] = {
    "x_api":   ("core.backends.x_api",   "XApiBackend",   "X 官方 API"),
    "browser": ("core.backends.browser", "BrowserBackend", "无头浏览器"),
}

_cache: dict[str, object] = {}


def available_names() -> list[str]:
    return list(REGISTRY)


def get(name: str):
    """取后端实例（进程内单例）。未知名抛 KeyError。"""
    name = (name or "").strip().lower()
    if name not in REGISTRY:
        raise KeyError(f"未知后端: {name!r}，可选: {list(REGISTRY)}")
    if name in _cache:
        return _cache[name]
    mod_path, cls_name, _ = REGISTRY[name]
    mod = importlib.import_module(mod_path)
    inst = getattr(mod, cls_name)()
    _cache[name] = inst
    return inst


def describe() -> list[dict]:
    """给 Web 控制台用：每个后端的可用性与说明。单个后端出错不影响其它。"""
    out: list[dict] = []
    for name, (mod_path, cls_name, label) in REGISTRY.items():
        item = {"name": name, "label": label, "available": False, "reason": "", "loaded": False}
        try:
            be = get(name)
            item["loaded"] = True
            ok, reason = be.available()
            item["available"] = bool(ok)
            item["reason"] = reason
        except Exception as e:
            item["reason"] = f"加载失败: {type(e).__name__}: {e}"
            log.warning("后端 %s 加载失败: %s", name, e)
        out.append(item)
    return out
