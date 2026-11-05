#!/usr/bin/env python3
"""登录态采集：打开一个真实浏览器，你手动登录，我只负责把会话存下来。

为什么要人机配合：研招网的专业目录接口要登录态（返回「请登录」），
但把账号密码交给脚本既不必要也不安全 —— 你在浏览器里正常登录，
脚本只保存 cookie / localStorage（storage_state），下次复用。

用法：
  python scripts/chsi_login.py            # 打开浏览器 → 你登录 → 回车 → 自动验证并保存
  python scripts/chsi_login.py --check    # 只用已保存的会话验证，不开浏览器

产物：.secrets/chsi_state.json（已在 .gitignore 里，不要提交、不要外传）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.abspath(os.path.join(HERE, "..", ".secrets", "chsi_state.json"))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
PROBE = "https://yz.chsi.com.cn/zsml/rs/zys.do"
PROBE_DATA = {"ssdm": "11", "dwmc": "北京大学", "start": "1"}


def cookies_from_state() -> dict[str, str]:
    with open(STATE, encoding="utf-8") as fh:
        store = json.load(fh)
    return {c["name"]: c["value"] for c in store.get("cookies", [])
            if "chsi" in c.get("domain", "")}


def check() -> int:
    if not os.path.exists(STATE):
        print(f"还没有登录态：{STATE} 不存在。先跑一次不带 --check 的。", file=sys.stderr)
        return 2
    ck = cookies_from_state()
    print(f"读到 {len(ck)} 个 chsi 域 cookie，逐个试接口…")
    for name, data in (
        ("专业目录(北大·北京)", PROBE_DATA),
        ("专业目录(085404 全国)", {"zydm": "085404", "start": "1"}),
    ):
        try:
            r = requests.post(PROBE, data=data, cookies=ck,
                              headers={"User-Agent": UA, "Referer": "https://yz.chsi.com.cn/zsml/",
                                       "X-Requested-With": "XMLHttpRequest"}, timeout=25)
            body = r.text
            flag = "请登录" not in body and r.status_code == 200
            print(f"  [{name}] {r.status_code} | 可用={flag} | {body[:220]}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [{name}] ERR {exc}")
    return 0


def _detect_login(ctx, page) -> bool:
    """登录成功的判据：出现学信网 CAS 会话 cookie，且页面上有「退出/个人中心」这类字样。

    只看 JSESSIONID 不够——未登录时也可能有；所以要同时看页面。
    """
    try:
        names = {c.get("name", "") for c in ctx.cookies()}
        has_cas = any("CASTGC" in n or "CHSI" in n.upper() for n in names)
        text = page.content() if page else ""
        has_ui = any(k in text for k in ("退出登录", "个人中心", "我的学信档案", "欢迎"))
        return has_cas and has_ui
    except Exception:                                     # noqa: BLE001
        return False


def login(wait_seconds: int = 300) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("需要 playwright：pip install playwright && playwright install chromium", file=sys.stderr)
        return 2

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--start-maximized",
                                                           "--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(no_viewport=True, user_agent=UA)
        page = ctx.new_page()
        page.goto("https://yz.chsi.com.cn/zsml/", timeout=60000)
        print(f"[1/3] 浏览器已打开。请在 {wait_seconds} 秒内完成学信网登录，"
              f"并顺手查一次专业目录（例如北京大学）。", flush=True)
        print("      脚本检测到登录成功会自动继续，不用在终端按任何键。", flush=True)

        deadline = time.time() + wait_seconds
        ok = False
        while time.time() < deadline:
            if _detect_login(ctx, page):
                ok = True
                break
            time.sleep(3)

        ctx.storage_state(path=STATE)
        try:
            print(f"[2/3] 当前页面：{page.title()} | {page.url}", flush=True)
        except Exception:                                 # noqa: BLE001
            pass
        print(f"[3/3] 会话已保存：{STATE}  （检测到登录={ok}）", flush=True)
        browser.close()

    print("\n立刻验证接口：", flush=True)
    return check()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只验证已保存的会话")
    args = ap.parse_args()
    return check() if args.check else login()


if __name__ == "__main__":
    raise SystemExit(main())
