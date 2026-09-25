#!/usr/bin/env python3
"""
setup_tg.py — Telegram Bot 配置向导

作用：
  1) 校验 TG_TOKEN 是否有效（getMe）
  2) 告诉你自己的 user id 和当前会话 chat_id（用于填白名单）
  3) 写入 .env

用法：
    python setup_tg.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def read_env() -> dict[str, str]:
    data: dict[str, str] = {}
    if not ENV_PATH.is_file():
        return data
    for raw in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        k, _, v = line.partition("=")
        data[k.strip()] = v.strip()
    return data


def write_env(updates: dict[str, str]) -> None:
    lines: list[str] = []
    remaining = dict(updates)
    if ENV_PATH.is_file():
        for raw in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
            stripped = raw.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                body = stripped[7:].lstrip() if stripped.lower().startswith("export ") else stripped
                k = body.partition("=")[0].strip()
                if k in remaining:
                    lines.append(f"{k}={remaining.pop(k)}")
                    continue
            lines.append(raw)
    for k, v in remaining.items():
        lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ask(prompt: str, default: str = "") -> str:
    shown = f" [{default}]" if default else ""
    try:
        v = input(f"{prompt}{shown}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已中断")
        sys.exit(1)
    return v or default


def main() -> None:
    print("=" * 62)
    print(" Telegram Bot 配置向导")
    print("=" * 62)
    print("\n如果还没有 bot：找 @BotFather -> /newbot -> 拿 token\n")

    env = read_env()
    token = _ask("TG_TOKEN", env.get("TG_TOKEN", ""))
    if not token:
        print("[x] TG_TOKEN 必填")
        sys.exit(1)

    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
        j = r.json()
    except Exception as e:
        print(f"[x] 请求 Telegram 失败：{e}")
        sys.exit(1)

    if not j.get("ok"):
        print(f"[x] token 无效：{j.get('description')}")
        sys.exit(1)

    bot = j["result"]
    print(f"[✓] Bot 有效：@{bot['username']}（{bot.get('first_name')}）")

    # 拉最近的 update，反推 user id / chat id
    try:
        u = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15).json()
    except Exception:
        u = {"ok": False}

    found_user = found_chat = ""
    if u.get("ok") and u.get("result"):
        last = u["result"][-1]
        msg = last.get("message") or last.get("channel_post") or {}
        from_user = msg.get("from") or {}
        chat = msg.get("chat") or {}
        found_user = str(from_user.get("id", ""))
        found_chat = str(chat.get("id", ""))
        if found_user or found_chat:
            print(f"[i] 从最近消息探测到：user_id={found_user or '?'}  chat_id={found_chat or '?'}")
    else:
        print(
            "\n[i] 还没收到任何消息。请先给 bot 发一句话（或把 bot 拉进你的群/频道发一句），\n"
            "    然后重新运行本脚本，就能自动探测到你的 id。"
        )

    print(
        "\n白名单说明：\n"
        "  ALLOWED_USERS = 允许发料的人（你自己的 id）——填了更安全\n"
        "  ALLOWED_CHATS = 允许的会话（转发来源）——群/频道 id 是负数\n"
        "  两个都留空 = 任何人给 bot 发东西都会触发发推，不建议。"
    )

    au = _ask("ALLOWED_USERS (逗号分隔，可留空)", env.get("ALLOWED_USERS", "") or found_user)
    ac = _ask("ALLOWED_CHATS (逗号分隔，可留空)", env.get("ALLOWED_CHATS", "") or found_chat)
    mode = _ask("MODE (auto=收到就发 / confirm=先确认)", env.get("MODE", "confirm"))
    if mode not in ("auto", "confirm"):
        mode = "confirm"

    write_env({
        "TG_TOKEN": token,
        "ALLOWED_USERS": au,
        "ALLOWED_CHATS": ac,
        "MODE": mode,
    })
    print(f"\n[✓] 配置已写入 {ENV_PATH}")

    if not au:
        print("[!] ALLOWED_USERS 为空：任何知道这个 bot 的人都能让它替你发推。建议填上。")


if __name__ == "__main__":
    main()
