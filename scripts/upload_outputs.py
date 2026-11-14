#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xianyu_auto.adapters.netdisk import (
    BaiduNetdiskAdapter,
    DryRunNetdiskAdapter,
    ManualShareRequired,
    QuarkWebShareAdapter,
)
from xianyu_auto.config import Settings, keychain_get, keychain_set


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_archive(source_dir: Path, archive_dir: Path) -> tuple[Path, dict]:
    if not source_dir.is_dir():
        raise SystemExit(f"source directory does not exist: {source_dir}")

    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    archive = archive_dir / f"闲鱼自动化交付_{stamp}.zip"
    files = sorted(
        path
        for path in source_dir.rglob("*")
        if path.is_file()
        and not any(part.startswith(".") for part in path.parts)
        and archive_dir not in path.parents
    )
    manifest = {
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "source_dir": str(source_dir),
        "files": [],
    }

    with zipfile.ZipFile(
        archive, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as handle:
        for path in files:
            arcname = path.relative_to(source_dir).as_posix()
            handle.write(path, arcname)
            manifest["files"].append(
                {
                    "path": arcname,
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
        handle.writestr(
            "MANIFEST.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )

    archive_sha256 = sha256_file(archive)
    manifest["archive"] = {
        "path": archive.name,
        "size": archive.stat().st_size,
        "sha256": archive_sha256,
    }
    manifest_path = archive.with_suffix(".manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    archive.with_suffix(".sha256").write_text(
        f"{archive_sha256}  {archive.name}\n",
        encoding="utf-8",
    )
    return archive, manifest


def resolve_remote_dir(value: str | None) -> str:
    remote_dir = (
        value
        or keychain_get("baidu_remote_dir")
        or "/apps/XianyuAuto/deliveries"
    ).strip()
    if not remote_dir.startswith("/apps/"):
        raise SystemExit(
            "百度网盘开放平台上传目录必须以 /apps/<应用名称> 开头，"
            "例如 /apps/xianyu-auto/deliveries"
        )
    return remote_dir.rstrip("/")


def upload_archive(
    archive: Path,
    remote_dir: str | None,
    *,
    netdisk: str,
    dry_run: bool,
    period_days: int,
    share_key: str,
) -> dict:
    if dry_run:
        adapter = DryRunNetdiskAdapter()
    elif netdisk == "quark-web":
        adapter = QuarkWebShareAdapter(
            profile_dir=Settings.load().quark_browser_dir,
            expire_days=period_days,
        )
    else:
        if not remote_dir:
            raise SystemExit("百度网盘上传必须指定 --remote-dir")
        access_token = keychain_get("baidu_access_token")
        refresh_token = keychain_get("baidu_refresh_token")
        app_id = keychain_get("baidu_app_id")
        app_key = keychain_get("baidu_app_key")
        app_secret = keychain_get("baidu_app_secret")
        missing: list[str] = []
        if not access_token and not refresh_token:
            missing.append("baidu_access_token 或 baidu_refresh_token")
        if missing:
            raise SystemExit(
                "百度网盘授权未完成，缺少："
                + "、".join(sorted(set(missing)))
                + "。请先运行 ./scripts/setup_credentials.py，"
                "并完成百度网盘开放平台 OAuth。"
            )
        adapter = BaiduNetdiskAdapter(
            access_token=access_token,
            refresh_token=refresh_token,
            app_id=app_id,
            app_key=app_key,
            app_secret=app_secret,
            remote_dir=remote_dir,
            period_days=period_days,
            manual_store=Settings.load().netdisk_manual_file,
        )

    try:
        result = adapter.share_file(
            archive,
            archive.name,
            share_key=share_key if netdisk == "baidu" else None,
        )
    finally:
        try:
            if not dry_run and netdisk == "baidu":
                if getattr(adapter, "access_token", None):
                    keychain_set("baidu_access_token", adapter.access_token)
                if getattr(adapter, "refresh_token", None):
                    keychain_set("baidu_refresh_token", adapter.refresh_token)
        finally:
            close = getattr(adapter, "close", None)
            if close:
                close()

    return {
        "link": result.link,
        "extract_code": result.extract_code,
        "netdisk": netdisk,
        **({"remote_dir": remote_dir} if remote_dir else {}),
        "raw": result.raw,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="打包 outputs 并可选上传到百度网盘，生成分享链接和提取码"
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=ROOT / "outputs",
        help="要打包的目录，默认 outputs",
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=None,
        help="压缩包目录，默认 <source-dir>/archives",
    )
    parser.add_argument(
        "--netdisk",
        choices=("quark-web", "baidu"),
        default="baidu",
        help="上传到百度网盘开放平台（默认）或夸克官方网页会话",
    )
    parser.add_argument(
        "--remote-dir",
        default=None,
        help="百度网盘目录，必须以 /apps/<应用名称> 开头；夸克模式不需要",
    )
    parser.add_argument(
        "--period-days",
        type=int,
        default=365,
        help="分享有效期，默认 365 天",
    )
    parser.add_argument(
        "--share-key",
        default="outputs",
        help="百度网盘稳定目录名；以后打包文件复用同一分享，默认 outputs",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="打包后上传并创建分享链接；不加时只在本机打包",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="使用模拟网盘结果，不访问百度或夸克网盘",
    )
    args = parser.parse_args()

    source_dir = args.source_dir.expanduser().resolve()
    archive_dir = (
        args.archive_dir.expanduser().resolve()
        if args.archive_dir
        else source_dir / "archives"
    )
    archive, manifest = build_archive(source_dir, archive_dir)
    print(f"archive={archive}")
    print(f"sha256={manifest['archive']['sha256']}")
    print(f"size={manifest['archive']['size']}")

    if not args.upload:
        return 0

    remote_dir = resolve_remote_dir(args.remote_dir) if args.netdisk == "baidu" else None
    try:
        result = upload_archive(
            archive,
            remote_dir,
            netdisk=args.netdisk,
            dry_run=args.dry_run,
            period_days=args.period_days,
            share_key=args.share_key,
        )
    except ManualShareRequired as exc:
        pending = {
            "uploaded": True,
            "share_status": "MANUAL_REQUIRED",
            "netdisk": args.netdisk,
            "remote_name": exc.remote_name,
            "remote_path": exc.remote_path,
            "fs_id": exc.fs_id,
            "share_key": exc.share_key,
            "archive": archive.name,
            "sha256": manifest["archive"]["sha256"],
        }
        pending_path = archive.with_suffix(".pending-share.json")
        pending_path.write_text(
            json.dumps(pending, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"uploaded=true")
        print(f"remote_path={exc.remote_path}")
        print("share_status=MANUAL_REQUIRED")
        print(f"pending_share={pending_path}")
        print(
            "请在百度网盘 App 中分享该目录，然后运行：\n"
            "uv run python scripts/set_baidu_share.py "
            f"--share-key {exc.share_key}"
        )
        return 2
    except Exception as exc:
        print(f"upload_failed={type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    share_path = archive.with_suffix(".share.json")
    share_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"link={result['link']}")
    print(f"extract_code={result['extract_code']}")
    print(f"share_result={share_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
