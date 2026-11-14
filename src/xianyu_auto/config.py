from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


KEYCHAIN_SERVICE = "XianyuAuto"
DEFAULT_HOME = Path.home() / "Library/Application Support/XianyuAuto"


def keychain_get(account: str) -> str | None:
    env_name = f"XIANYU_AUTO_{account.upper()}"
    if env_name in os.environ:
        return os.environ[env_name]
    proc = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            account,
            "-w",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    value = proc.stdout.strip()
    if proc.returncode != 0 or not value:
        return None
    # The macOS security CLI can return UTF-8 path values as hex on some
    # systems. Normalize only our known path setting.
    if account == "baidu_remote_dir" and len(value) % 2 == 0:
        try:
            decoded = bytes.fromhex(value).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            pass
        else:
            if decoded.startswith("/apps/"):
                return decoded
    return value


def keychain_set(account: str, value: str) -> None:
    subprocess.run(
        [
            "security",
            "add-generic-password",
            "-U",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            account,
            "-w",
            value,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def keychain_delete(account: str) -> None:
    subprocess.run(
        ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account],
        check=False,
        capture_output=True,
        text=True,
    )


@dataclass(slots=True)
class Settings:
    home: Path
    database_path: Path
    assets_dir: Path
    owner_open_id: str | None
    yd_is_base_url: str
    yd_is_cookie_id: str | None
    yd_is_session_cookie: str | None
    yd_is_username: str
    yd_is_password: str | None
    netdisk_mode: str
    netdisk_manual_link: str | None
    netdisk_manual_code: str | None
    feishu_app_id: str | None
    feishu_app_secret: str | None
    feishu_verification_token: str | None
    feishu_encrypt_key: str | None
    vision_api_key: str | None
    vision_base_url: str | None
    vision_model: str | None
    vision_fallback_model: str | None
    admin_token: str | None
    publish_dry_run: bool
    auto_confirm_delivery: bool
    daily_publish_limit: int
    timezone: str
    scheduler_enabled: bool
    scheduler_tick_seconds: int
    raw: dict[str, Any]
    publish_location: dict[str, Any] | None = None
    image_api_key: str | None = None
    image_base_url: str = "http://127.0.0.1:3425/v1"
    image_model: str = "gptfree-pool/gpt-image-2.6-sunburst"
    image_generation_enabled: bool = False

    @property
    def approvals_dir(self) -> Path:
        return self.assets_dir / "approvals"

    @property
    def netdisk_manual_file(self) -> Path:
        return self.home / "manual-netdisk.json"

    @property
    def quark_browser_dir(self) -> Path:
        return self.home / "quark-browser"

    @classmethod
    def load(cls, home: Path | None = None) -> "Settings":
        resolved_home = Path(
            home or os.environ.get("XIANYU_AUTO_HOME") or DEFAULT_HOME
        ).expanduser()
        config_path = resolved_home / "config.json"
        raw: dict[str, Any] = {}
        if config_path.exists():
            with config_path.open(encoding="utf-8") as handle:
                raw = json.load(handle)

        def secret(name: str, config_key: str | None = None) -> str | None:
            return keychain_get(name) or raw.get(config_key or name)

        database_path = Path(
            raw.get("database_path", resolved_home / "data" / "xianyu_auto.db")
        ).expanduser()
        assets_dir = Path(
            raw.get("assets_dir", resolved_home / "assets")
        ).expanduser()
        manual_link = raw.get("netdisk_manual_link") or keychain_get(
            "netdisk_manual_link"
        )
        manual_code = raw.get("netdisk_manual_code") or keychain_get(
            "netdisk_manual_code"
        )
        return cls(
            home=resolved_home,
            database_path=database_path,
            assets_dir=assets_dir,
            owner_open_id=raw.get("owner_open_id") or keychain_get("owner_open_id"),
            yd_is_base_url=raw.get("yd_is_base_url", "http://127.0.0.1:59188"),
            yd_is_cookie_id=raw.get("yd_is_cookie_id") or keychain_get("yd_is_cookie_id"),
            yd_is_session_cookie=secret("yd_is_session_cookie"),
            yd_is_username=raw.get("yd_is_username", "admin"),
            yd_is_password=secret("yd_is_password"),
            netdisk_mode=raw.get("netdisk_mode", "quark_web"),
            netdisk_manual_link=manual_link,
            netdisk_manual_code=manual_code,
            feishu_app_id=raw.get("feishu_app_id") or keychain_get("feishu_app_id"),
            feishu_app_secret=secret("feishu_app_secret"),
            feishu_verification_token=secret("feishu_verification_token"),
            feishu_encrypt_key=secret("feishu_encrypt_key"),
            vision_api_key=secret("vision_api_key"),
            vision_base_url=raw.get("vision_base_url") or keychain_get("vision_base_url"),
            vision_model=raw.get("vision_model") or keychain_get("vision_model"),
            vision_fallback_model=raw.get(
                "vision_fallback_model", "glm-4.5v"
            )
            or "glm-4.5v",
            admin_token=secret("admin_token"),
            publish_dry_run=bool(raw.get("publish_dry_run", True)),
            auto_confirm_delivery=bool(raw.get("auto_confirm_delivery", False)),
            daily_publish_limit=int(raw.get("daily_publish_limit", 1)),
            timezone=raw.get("timezone", "Asia/Shanghai"),
            scheduler_enabled=bool(raw.get("scheduler_enabled", True)),
            scheduler_tick_seconds=int(raw.get("scheduler_tick_seconds", 30)),
            raw=raw,
            publish_location=raw.get("publish_location"),
            image_api_key=secret("image_api_key"),
            image_base_url=(
                raw.get("image_base_url")
                or keychain_get("image_base_url")
                or "http://127.0.0.1:3425/v1"
            ),
            image_model=(
                raw.get("image_model")
                or keychain_get("image_model")
                or "gptfree-pool/gpt-image-2.6-sunburst"
            ),
            image_generation_enabled=bool(
                raw.get("image_generation_enabled", False)
            ),
        )

