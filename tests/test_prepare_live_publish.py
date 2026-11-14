from __future__ import annotations

from xianyu_auto.adapters.feishu import DryRunFeishuClient
from xianyu_auto.adapters.netdisk import DryRunNetdiskAdapter
from xianyu_auto.adapters.ydisks import DryRunYdisksAdapter
from xianyu_auto.models import ProductStatus
from xianyu_auto.service import Orchestrator

from scripts.prepare_live_publish import prepare_live_publish
from tests.conftest import create_test_product


def test_prepare_live_publish_archives_only_dry_run_state(settings, db) -> None:
    service = Orchestrator(
        settings=settings,
        db=db,
        ydisks=DryRunYdisksAdapter(),
        netdisk=DryRunNetdiskAdapter(),
        feishu=DryRunFeishuClient("owner-open-id"),
    )
    product_id = create_test_product(service)
    approval = service.submit_product(product_id)
    service.decide_approval(
        approval["id"], approve=True, actor_open_id="owner-open-id"
    )
    assert service.worker_once() is True

    before = service.db.valid_approval_for_product(product_id)
    assert before is not None
    attempt = service.db.one(
        "SELECT * FROM publish_attempts WHERE product_id=?", (product_id,)
    )
    assert attempt is not None
    binding = service.db.one(
        "SELECT * FROM delivery_bindings WHERE product_id=?", (product_id,)
    )
    assert binding is not None

    report = prepare_live_publish(
        db,
        product_id,
        timezone_name=settings.timezone,
        owner_open_id="owner-open-id",
        apply=True,
    )

    assert report["daily_counter_before"] == 1
    assert service.db.product(product_id)["status"] == ProductStatus.APPROVED.value
    after = service.db.valid_approval_for_product(product_id)
    assert after is not None and after["id"] == before["id"]
    assert service.db.one(
        "SELECT * FROM delivery_bindings WHERE product_id=?", (product_id,)
    ) is None
    archived = service.db.one(
        "SELECT * FROM publish_attempts WHERE id=?", (attempt["id"],)
    )
    assert archived is not None
    assert archived["state"] == "DRY_RUN_ARCHIVED"
    assert archived["idempotency_key"].startswith("dry-run-archive:")
    assert service.db.daily_publish_count(report["daily_counter_day"]) == 0
    job = service.db.one(
        "SELECT * FROM jobs WHERE id=?", (report["publish_job_id"],)
    )
    assert job is not None and job["status"] == "PENDING"
