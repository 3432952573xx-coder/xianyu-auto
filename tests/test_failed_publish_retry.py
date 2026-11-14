from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from xianyu_auto.adapters.feishu import DryRunFeishuClient
from xianyu_auto.adapters.netdisk import DryRunNetdiskAdapter
from xianyu_auto.adapters.ydisks import DryRunYdisksAdapter, PublishResult
from xianyu_auto.models import ProductStatus
from xianyu_auto.service import Orchestrator

from scripts.prepare_failed_publish_retry import prepare_failed_publish_retry
from tests.conftest import create_test_product


class FailingPublishYdisks(DryRunYdisksAdapter):
    def publish_item(self, **kwargs: Any) -> PublishResult:
        raise RuntimeError("simulated business rejection")


def _failed_live_publish(settings, db) -> tuple[Orchestrator, int, dict]:
    service = Orchestrator(
        settings=settings,
        db=db,
        ydisks=FailingPublishYdisks(),
        netdisk=DryRunNetdiskAdapter(),
        feishu=DryRunFeishuClient("owner-open-id"),
    )
    product_id = create_test_product(service)
    approval = service.submit_product(product_id)
    service.decide_approval(
        approval["id"], approve=True, actor_open_id="owner-open-id"
    )
    assert service.worker_once() is True
    assert db.product(product_id)["status"] == ProductStatus.PUBLISH_FAILED.value
    attempt = db.one(
        "SELECT * FROM publish_attempts WHERE product_id=? AND state='FAILED'",
        (product_id,),
    )
    job = db.one(
        "SELECT * FROM jobs WHERE product_id=? AND kind='publish'", (product_id,)
    )
    assert attempt is not None and job is not None
    return service, product_id, {"approval": approval, "attempt": attempt, "job": job}


def test_failed_publish_retry_requeues_only_the_approved_version(
    settings, db, tmp_path: Path
) -> None:
    service, product_id, rows = _failed_live_publish(settings, db)
    report = prepare_failed_publish_retry(
        db,
        product_id=product_id,
        approval_id=rows["approval"]["id"],
        attempt_id=int(rows["attempt"]["id"]),
        job_id=int(rows["job"]["id"]),
        owner_open_id="owner-open-id",
        backup_dir=tmp_path / "backups",
        apply=True,
    )

    assert report["applied"] is True
    assert Path(report["database_backup"]).exists()
    assert db.product(product_id)["status"] == ProductStatus.APPROVED.value
    archived = db.one(
        "SELECT * FROM publish_attempts WHERE id=?", (rows["attempt"]["id"],)
    )
    assert archived is not None
    assert archived["state"] == "FAILED_ARCHIVED"
    assert archived["idempotency_key"].startswith("failed-archive:")
    job = db.one("SELECT * FROM jobs WHERE id=?", (rows["job"]["id"],))
    assert job is not None and job["status"] == "PENDING"
    approval = db.valid_approval_for_product(product_id)
    assert approval is not None and approval["id"] == rows["approval"]["id"]


def test_failed_publish_retry_rejects_wrong_approval(
    settings, db, tmp_path: Path
) -> None:
    _, product_id, rows = _failed_live_publish(settings, db)
    with pytest.raises(KeyError):
        prepare_failed_publish_retry(
            db,
            product_id=product_id,
            approval_id="not-the-approved-version",
            attempt_id=int(rows["attempt"]["id"]),
            job_id=int(rows["job"]["id"]),
            owner_open_id="owner-open-id",
            backup_dir=tmp_path / "backups",
            apply=True,
        )
    assert db.product(product_id)["status"] == ProductStatus.PUBLISH_FAILED.value
