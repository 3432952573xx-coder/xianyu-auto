#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xianyu_auto.adapters.netdisk import QuarkWebShareAdapter
from xianyu_auto.config import DEFAULT_HOME


def main() -> int:
    parser = argparse.ArgumentParser(
        description="打开夸克网盘官方网页，扫码登录后保存本机专用会话"
    )
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=DEFAULT_HOME / "quark-browser",
    )
    args = parser.parse_args()
    profile_dir = args.profile_dir.expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.chmod(0o700)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            locale="zh-CN",
            viewport={"width": 1440, "height": 1000},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(
            "https://pan.quark.cn/",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        print("请在弹出的夸克官方页面中扫码登录；程序不会读取或显示你的凭据。")
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            response = context.request.get(
                f"{QuarkWebShareAdapter.API_BASE}/member",
                params=QuarkWebShareAdapter._params(),
                headers=QuarkWebShareAdapter.HEADERS,
                timeout=30_000,
            )
            if response.status == 200:
                try:
                    payload = response.json()
                except Exception:
                    payload = {}
                if payload.get("status") == 200:
                    print("夸克网盘登录成功，专用会话已保存在本机。")
                    time.sleep(3)
                    context.close()
                    return 0
            time.sleep(2)
        context.close()
        print("登录等待超时，请重新运行本脚本。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
