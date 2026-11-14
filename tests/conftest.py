from __future__ import annotations

import json
from pathlib import Path

import pytest

from xianyu_auto.adapters.feishu import DryRunFeishuClient
from xianyu_auto.adapters.netdisk import DryRunNetdiskAdapter
from xianyu_auto.adapters.ydisks import DryRunYdisksAdapter
from xianyu_auto.checks import canonical_content_hash, sha256_file
from xianyu_auto.config import Settings
from xianyu_auto.db import Database
from xianyu_auto.service import Orchestrator

TEST_ADMIN_TOKEN = "fixture-api-key"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        home=tmp_path,
        database_path=tmp_path / "data" / "test.sqlite3",
        assets_dir=tmp_path / "assets",
        owner_open_id="owner-open-id",
        yd_is_base_url="http://127.0.0.1:59188",
        yd_is_cookie_id="cookie-id",
        yd_is_session_cookie=None,
        yd_is_username="admin",
        yd_is_password=None,
        netdisk_mode="manual",
        netdisk_manual_link=None,
        netdisk_manual_code=None,
        feishu_app_id=None,
        feishu_app_secret=None,
        feishu_verification_token=None,
        feishu_encrypt_key=None,
        vision_api_key=None,
        vision_base_url=None,
        vision_model=None,
        vision_fallback_model="glm-4.5v",
        admin_token=TEST_ADMIN_TOKEN,
        publish_dry_run=True,
        auto_confirm_delivery=False,
        daily_publish_limit=2,
        timezone="Asia/Shanghai",
        scheduler_enabled=False,
        scheduler_tick_seconds=30,
        raw={},
    )


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.database_path)
    database.initialize()
    return database


@pytest.fixture
def orchestrator(
    settings: Settings,
    db: Database,
    tmp_path: Path,
) -> Orchestrator:
    return Orchestrator(
        settings=settings,
        db=db,
        ydisks=DryRunYdisksAdapter(),
        netdisk=DryRunNetdiskAdapter(),
        feishu=DryRunFeishuClient("owner-open-id"),
    )


def create_test_product(orchestrator: Orchestrator, *, title: str = "测试字帖") -> int:
    root = orchestrator.settings.assets_dir / "test-product"
    root.mkdir(parents=True, exist_ok=True)
    pdf = root / "product.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% test fixture\n")
    images = []
    for index in range(3):
        path = root / f"{index}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        images.append(path)
    assets = [
        {
            "kind": "pdf",
            "path": str(pdf),
            "sha256": sha256_file(pdf),
            "metadata": {
                "pages": 20,
                "embedded_fonts": ["FixtureFont"],
                "size_bytes": pdf.stat().st_size,
            },
        }
    ]
    assets.extend(
        {
            "kind": "image",
            "path": str(path),
            "sha256": sha256_file(path),
            "metadata": {"width": 1080, "height": 1440},
        }
        for path in images
    )
    description = "测试电子版资料，拍下后自动发送网盘链接，不邮寄。"
    content_hash = canonical_content_hash(
        title=title,
        description=description,
        category="K12 可打印练习资料",
        price_cents=590,
        normal_price_cents=990,
        pages=20,
        asset_hashes=[asset["sha256"] for asset in assets],
    )
    return orchestrator.db.create_product(
        slug=f"test-{title}",
        title=title,
        description=description,
        category="K12 可打印练习资料",
        price_cents=590,
        normal_price_cents=990,
        pages=20,
        content_hash=content_hash,
        risk_flags=["测试风险"],
        metadata={},
        preview_path=str(images[0]),
        assets=assets,
    )

