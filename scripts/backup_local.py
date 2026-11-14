#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if (ROOT / "src").exists():
    sys.path.insert(0, str(ROOT / "src"))

from xianyu_auto.config import DEFAULT_HOME, Settings, keychain_get


def sqlite_backup(source: Path, target: Path) -> None:
    if not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def copy_tree(source: Path, target: Path) -> None:
    if source.exists():
        shutil.copytree(source, target, dirs_exist_ok=True)


def main() -> int:
    passphrase = keychain_get("backup_passphrase")
    if not passphrase:
        raise SystemExit("backup_passphrase is not configured in Keychain")
    settings = Settings.load()
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    requested_dir = os.environ.get("XIANYU_AUTO_BACKUP_DIR")
    output_dir = (
        Path(requested_dir).expanduser()
        if requested_dir
        else settings.home / "backups"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"xianyu-auto-{stamp}.tar.gz.enc"

    with tempfile.TemporaryDirectory(prefix="xianyu-auto-backup-") as temp_name:
        stage = Path(temp_name) / "snapshot"
        stage.mkdir()
        sqlite_backup(settings.database_path, stage / "orchestrator.sqlite3")
        config_path = settings.home / "config.json"
        if config_path.exists():
            shutil.copy2(config_path, stage / "config.json")
        copy_tree(settings.assets_dir, stage / "assets")

        ydisks_home = Path.home() / "Library/Application Support/YdisksXianyuHelper"
        if ydisks_home.exists():
            sqlite_backup(ydisks_home / "data/xianyu_data.db", stage / "ydisks/xianyu_data.db")
            data_key = ydisks_home / "data-key"
            if data_key.exists():
                shutil.copy2(data_key, stage / "ydisks/data-key")

        plain_tar = Path(temp_name) / "snapshot.tar.gz"
        with tarfile.open(plain_tar, "w:gz") as archive:
            archive.add(stage, arcname=f"xianyu-auto-{stamp}")

        env = os.environ.copy()
        env["XIANYU_BACKUP_PASSPHRASE"] = passphrase
        with plain_tar.open("rb") as source, output.open("wb") as target:
            subprocess.run(
                [
                    "openssl",
                    "enc",
                    "-aes-256-cbc",
                    "-pbkdf2",
                    "-salt",
                    "-pass",
                    "env:XIANYU_BACKUP_PASSPHRASE",
                ],
                stdin=source,
                stdout=target,
                check=True,
            )
    output.chmod(0o600)

    backups = sorted(output_dir.glob("xianyu-auto-*.tar.gz.enc"), reverse=True)
    for old in backups[14:]:
        old.unlink(missing_ok=True)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
