#!/usr/bin/env python3
"""
setup_x.py — X (Twitter) 凭据向导 + 连通性自检

作用：
  1) 用 App 的 Consumer Key/Secret 走 OAuth 1.0a 授权，自动拿到 Access Token/Secret
  2) 写入 .env（保留其它已有配置）
  3) 立刻验证：读自己账号 + 发一条自检推（可选）+ 试上传媒体（探测免费层有没有媒体权限）

用法：
    python setup_x.py            # 授权 + 验证
    python setup_x.py --no-post  # 只授权和读账号，不发自检推
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
import webbrowser
from pathlib import Path

import tweepy

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


# ── .env 读写（无第三方依赖） ────────────────────────────────

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
    """更新 .env 中指定键，保留注释与其它行；不存在的键追加到末尾。"""
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
    shown = f" [{default[:8]}…]" if default else ""
    try:
        v = input(f"{prompt}{shown}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已中断")
        sys.exit(1)
    return v or default


# ── 主流程 ───────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-post", action="store_true", help="不发自检推文")
    args = ap.parse_args()

    print("=" * 62)
    print(" X (Twitter) 凭据向导")
    print("=" * 62)
    print(
        "\n先确认两件事（顺序错了 Token 只有只读权限）：\n"
        "  1. developer.x.com -> 你的 App -> User authentication settings\n"
        "     App permissions 设为 Read and Write，Type of App 选 Web App\n"
        "     Callback URI 填: http://127.0.0.1:8080/callback\n"
        "  2. 保存后再到 Keys and tokens 页生成/重生成 Access Token\n"
    )

    env = read_env()
    ck = _ask("Consumer Key (API Key)", env.get("X_CONSUMER_KEY", ""))
    cs = _ask("Consumer Secret (API Secret)", env.get("X_CONSUMER_SECRET", ""))
    if not ck or not cs:
        print("[x] Consumer Key/Secret 必填")
        sys.exit(1)

    # OAuth 1.0a 三步：request token -> 用户授权 -> access token
    auth = tweepy.OAuth1UserHandler(ck, cs, callback="http://127.0.0.1:8080/callback")
    try:
        redirect_url = auth.get_authorization_url()
    except tweepy.TweepyException as e:
        print(f"\n[x] 获取授权链接失败：{e}")
        print("    常见原因：Consumer Key/Secret 填错，或该 App 未开启 OAuth 1.0a")
        sys.exit(1)

    print("\n在浏览器打开下面链接，用你的 X 账号授权：\n")
    print(f"  {redirect_url}\n")
    try:
        webbrowser.open(redirect_url)
    except Exception:
        pass

    print("授权后浏览器会跳到一个 127.0.0.1 的地址（页面打不开是正常的，属于回调地址）。")
    print("把浏览器地址栏里 URL 的完整内容（或只贴 oauth_verifier= 后面那串）粘进来。\n")
    pasted = _ask("回调 URL 或 verifier")
    if not pasted:
        print("[x] 未提供 verifier")
        sys.exit(1)
    if "oauth_verifier=" in pasted:
        verifier = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query).get("oauth_verifier", [""])[0]
    else:
        verifier = pasted.strip()
    if not verifier:
        print("[x] 解析 verifier 失败")
        sys.exit(1)

    try:
        auth.get_access_token(verifier)
    except tweepy.TweepyException as e:
        print(f"\n[x] 换取 Access Token 失败：{e}")
        print("    verifier 是一次性的，且有效期很短。请重新运行脚本。")
        sys.exit(1)

    at, asec = auth.access_token, auth.access_token_secret
    write_env({
        "X_CONSUMER_KEY": ck,
        "X_CONSUMER_SECRET": cs,
        "X_ACCESS_TOKEN": at,
        "X_ACCESS_SECRET": asec,
    })
    print(f"\n[✓] 凭据已写入 {ENV_PATH}")

    # ── 验证 1：读自己账号 ───────────────────────────────
    v2 = tweepy.Client(ck, cs, at, asec)
    try:
        me = v2.get_me(user_auth=True)
        uname = me.data.username
        print(f"[✓] 鉴权有效，账号 = @{uname}")
    except tweepy.TweepyException as e:
        print(f"[x] 读取账号失败：{e}")
        sys.exit(1)

    # ── 验证 2：发推权限 ─────────────────────────────────
    if not args.no_post:
        try:
            r = v2.create_tweet(text="自检：TwitBot 通道已打通。可忽略。")
            tid = r.data["id"]
            print(f"[✓] 发推权限正常 -> https://x.com/i/status/{tid}")
            try:
                v2.delete_tweet(tid)
                print("[✓] 已删除该自检推文")
            except Exception:
                print("[!] 自检推文删除失败，请手动删掉")
        except tweepy.TweepyException as e:
            print(f"[x] 发推失败：{e}")
            body = str(e).lower()
            if "403" in body:
                print("    → App 权限不是 Read and Write，或 Access Token 是在改权限之前生成的。")
                print("      去 developer.x.com 重设权限后重新生成 Access Token，再跑一遍本脚本。")
            elif "429" in body:
                print("    → 触达免费层额度上限，等下个月或升级套餐。")
            sys.exit(1)

    # ── 验证 3：媒体上传权限（免费层常无） ────────────────
    try:
        v1 = tweepy.API(tweepy.OAuth1UserHandler(ck, cs, at, asec))
        png_1x1 = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d4944415478da63f8ffff3f0005fe02fea6d5b1e20000000049454e44ae426082"
        )
        tmp = BASE_DIR / "_probe.png"
        tmp.write_bytes(png_1x1)
        media = v1.media_upload(str(tmp))
        tmp.unlink(missing_ok=True)
        print(f"[✓] 媒体上传可用（media_id={media.media_id}）——可以发图文/视频")
    except tweepy.TweepyException as e:
        print(f"[!] 媒体上传不可用：{e}")
        print("    → 这层没有 media/upload 权限，机器人会自动降级为纯文本发推（图片发不上去）。")
    except FileNotFoundError:
        pass

    print("\n完成。接下来：")
    print("  python bot.py --dry-run   # 干跑")
    print("  python bot.py             # 正式运行")


if __name__ == "__main__":
    main()
