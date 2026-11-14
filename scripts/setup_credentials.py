#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xianyu_auto.config import DEFAULT_HOME, KEYCHAIN_SERVICE, keychain_get, keychain_set


SECRET_ACCOUNTS = (
    ("owner_open_id", "飞书审批人 open_id（不是密码）", False),
    ("feishu_app_id", "飞书自建应用 App ID", False),
    ("feishu_app_secret", "飞书自建应用 App Secret", True),
    ("yd_is_cookie_id", "Ydisks 中闲鱼账号的 cookie_id", False),
    ("yd_is_password", "Ydisks 管理后台管理员密码", True),
    ("yd_is_session_cookie", "Ydisks session Cookie（可选，留空则用管理员密码登录）", True),
    ("baidu_app_id", "百度网盘开放平台 AppID", False),
    ("baidu_app_key", "百度网盘开放平台 AppKey（可选）", False),
    ("baidu_app_secret", "百度网盘开放平台 Secret（可选）", True),
    ("baidu_refresh_token", "百度网盘 OAuth refresh token（可选）", True),
    ("baidu_access_token", "百度网盘 access token（可选）", True),
    ("baidu_remote_dir", "百度网盘上传目录（/apps/<应用名称>/deliveries）", False),
    ("netdisk_manual_link", "手动网盘分享链接（仅 manual 后备模式）", False),
    ("netdisk_manual_code", "手动网盘提取码（仅 manual 后备模式）", True),
    ("backup_passphrase", "加密备份口令（至少 12 位）", True),
    ("vision_api_key", "视觉模型 API Key（修改意见分析，可选）", True),
    ("vision_base_url", "视觉模型 Base URL（OpenAI 兼容）", False),
    ("vision_model", "视觉模型名（默认 glm-4.6v-flash）", False),
    ("image_api_key", "Magpie 网关 API Key（本地默认 magpie）", True),
    ("image_base_url", "Magpie 网关 Base URL（默认 http://127.0.0.1:3425/v1）", False),
    ("image_model", "生图模型名（默认 gptfree-pool/gpt-image-2.6-sunburst）", False),
    ("deepseek_api_key", "DeepSeek API Key（可选，当前不参与 PDF/答案生成）", True),
    ("deepseek_base_url", "DeepSeek base URL（可选）", False),
    ("deepseek_model", "DeepSeek 模型名（可选）", False),
)


def _configured(account: str) -> bool:
    return bool(keychain_get(account))


def _read_config(home: Path) -> dict:
    path = home / "config.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_config(home: Path, payload: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    path.chmod(0o600)


def print_status() -> None:
    for account, label, _ in SECRET_ACCOUNTS:
        state = "已配置" if _configured(account) else "未配置"
        print(f"{state:4}  {account:<24} {label}")


def prompt_value(label: str, *, secret: bool, current: str | None) -> str | None:
    suffix = "，直接回车保留现值" if current else "，留空跳过"
    prompt = f"{label}{suffix}: "
    value = getpass.getpass(prompt) if secret else input(prompt)
    value = value.strip()
    if not value:
        return current
    return value


def save_secrets(interactive: bool) -> None:
    if not interactive:
        raise SystemExit("interactive input is required unless --check is used")
    print("只在本机终端输入，内容不会回显；输入直接写入 macOS Keychain。")
    for account, label, secret in SECRET_ACCOUNTS:
        current = keychain_get(account)
        value = prompt_value(label, secret=secret, current=current)
        if value and value != current:
            keychain_set(account, value)
    print("凭据已写入 Keychain。未显示的旧值保持不变。")


def configure_runtime(home: Path) -> None:
    config = _read_config(home)
    print("\n非敏感运行配置（直接回车保留默认值）")
    defaults = {
        "yd_is_base_url": config.get("yd_is_base_url", "http://127.0.0.1:59188"),
        "yd_is_username": config.get("yd_is_username", "admin"),
        "netdisk_mode": config.get("netdisk_mode", "quark_web"),
        "daily_publish_limit": str(config.get("daily_publish_limit", 1)),
    }
    for key, default in defaults.items():
        value = input(f"{key} [{default}]: ").strip() or str(default)
        if key == "daily_publish_limit":
            parsed = int(value)
            if parsed not in (1, 2):
                raise SystemExit("daily_publish_limit 只能是 1 或 2")
            config[key] = parsed
        else:
            config[key] = value
    if config["netdisk_mode"] not in {"quark_web", "baidu", "manual"}:
        raise SystemExit("netdisk_mode 只能是 quark_web、baidu 或 manual")
    if "publish_dry_run" not in config:
        config["publish_dry_run"] = True
    _write_config(home, config)
    print(f"运行配置已更新：{home / 'config.json'}")


def validate() -> list[str]:
    issues: list[str] = []
    if not _configured("owner_open_id"):
        issues.append("缺少 owner_open_id")
    if not _configured("feishu_app_id") or not _configured("feishu_app_secret"):
        issues.append("缺少飞书自建应用凭据")
    if not _configured("yd_is_cookie_id"):
        issues.append("缺少 Ydisks cookie_id")
    if not _configured("yd_is_password") and not _configured("yd_is_session_cookie"):
        issues.append("缺少 Ydisks 管理员密码或 session Cookie")
    if not _configured("backup_passphrase"):
        issues.append("缺少备份口令")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default=str(DEFAULT_HOME))
    parser.add_argument("--check", action="store_true", help="只显示是否已配置，不读取值")
    parser.add_argument(
        "--configure-runtime",
        action="store_true",
        help="同时录入非敏感运行配置",
    )
    args = parser.parse_args()
    home = Path(args.home).expanduser()
    if args.check:
        print_status()
        issues = validate()
        if issues:
            print("\n待完成：")
            for issue in issues:
                print(f"- {issue}")
            return 1
        print("\n关键凭据已配置。")
        return 0

    save_secrets(interactive=True)
    if args.configure_runtime:
        configure_runtime(home)
    print("\n当前配置状态：")
    print_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
