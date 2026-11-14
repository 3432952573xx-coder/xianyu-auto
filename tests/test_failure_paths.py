from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from xianyu_auto.adapters.netdisk import ManualShareRequired, ShareResult
from xianyu_auto.adapters.ydisks import DryRunYdisksAdapter, PublishUncertainError, PublishResult
from xianyu_auto.config import Settings
from xianyu_auto.db import Database
from xianyu_auto.models import ProductStatus
from xianyu_auto.service import Orchestrator

from .conftest import create_test_product


class CountingYdisks(DryRunYdisksAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.publish_calls = 0
        self.timeout_reached_platform = False

    def publish_item(self, **kwargs: Any) -> PublishResult:
        self.publish_calls += 1
        if self.timeout_reached_platform:
            calls_before_super = self.publish_calls
            result = super().publish_item(**kwargs)
            self.publish_calls = calls_before_super
            raise PublishUncertainError(f"timeout after creating {result.item_id}")
        raise PublishUncertainError("timeout before confirmation")


class FailingNetdisk:
    def share_file(
        self,
        file_path: Path,
        remote_name: str,
        *,
        share_key: str | None = None,
    ) -> ShareResult:
        raise RuntimeError("netdisk unavailable")


class ManualSharePendingNetdisk:
    def share_file(
        self,
        file_path: Path,
        remote_name: str,
        *,
        share_key: str | None = None,
    ) -> ShareResult:
        raise ManualShareRequired(
            share_key=share_key or remote_name,
            remote_name=remote_name,
            remote_path=f"/apps/test/{share_key or remote_name}",
            fs_id="123",
        )


def _service(
    settings: Settings,
    db,
    *,
    ydisks,
    netdisk,
) -> Orchestrator:
    from xianyu_auto.adapters.feishu import DryRunFeishuClient

    return Orchestrator(
        settings=settings,
        db=db,
        ydisks=ydisks,
        netdisk=netdisk,
        feishu=DryRunFeishuClient("owner-open-id"),
    )


def test_timeout_does_not_republish(settings: Settings, db) -> None:
    ydisks = CountingYdisks()
    service = _service(settings, db, ydisks=ydisks, netdisk=_ok_netdisk())
    product_id = create_test_product(service)
    approval = service.submit_product(product_id)
    service.decide_approval(
        approval["id"], approve=True, actor_open_id="owner-open-id"
    )
    assert service.worker_once() is True
    product = service.db.product(product_id)
    assert product["status"] == ProductStatus.PUBLISH_FAILED.value
    assert ydisks.publish_calls == 1
    assert service.worker_once() is False
    assert ydisks.publish_calls == 1


def test_timeout_with_unique_item_is_adopted(settings: Settings, db) -> None:
    ydisks = CountingYdisks()
    ydisks.timeout_reached_platform = True
    service = _service(settings, db, ydisks=ydisks, netdisk=_ok_netdisk())
    product_id = create_test_product(service)
    approval = service.submit_product(product_id)
    service.decide_approval(
        approval["id"], approve=True, actor_open_id="owner-open-id"
    )
    assert service.worker_once() is True
    assert service.db.product(product_id)["status"] == ProductStatus.DELIVERY_READY.value
    assert ydisks.publish_calls == 1


def test_netdisk_failure_blocks_publish(settings: Settings, db) -> None:
    ydisks = CountingYdisks()
    service = _service(
        settings, db, ydisks=ydisks, netdisk=FailingNetdisk()
    )
    product_id = create_test_product(service)
    approval = service.submit_product(product_id)
    service.decide_approval(
        approval["id"], approve=True, actor_open_id="owner-open-id"
    )
    assert service.worker_once() is True
    assert service.db.product(product_id)["status"] == ProductStatus.APPROVED.value
    assert ydisks.publish_calls == 0


def test_manual_share_requirement_defers_without_publish(settings: Settings, db) -> None:
    ydisks = CountingYdisks()
    service = _service(
        settings, db, ydisks=ydisks, netdisk=ManualSharePendingNetdisk()
    )
    product_id = create_test_product(service)
    approval = service.submit_product(product_id)
    service.decide_approval(
        approval["id"], approve=True, actor_open_id="owner-open-id"
    )

    assert service.worker_once() is True
    assert service.db.product(product_id)["status"] == ProductStatus.APPROVED.value
    assert ydisks.publish_calls == 0
    job = service.db.one(
        "SELECT * FROM jobs WHERE kind='publish' AND product_id=?", (product_id,)
    )
    assert job is not None
    assert job["status"] == "PENDING"
    assert "百度网盘商品目录已准备好" in job["last_error"]
    payload = json.loads(job["payload_json"])
    assert payload["manual_share"]["share_key"].startswith("test-")
    assert payload["manual_share"]["remote_path"].startswith("/apps/test/test-")
    assert any(
        "百度网盘 App" in str(message.get("content", ""))
        for message in service.feishu.messages
    )
    assert service.worker_once() is False
    assert ydisks.publish_calls == 0


def test_reconcile_flags_empty_stock(settings: Settings, db) -> None:
    class EmptyStockYdisks(DryRunYdisksAdapter):
        def get_card_details(self, card_id: int) -> dict[str, Any]:
            return {"id": card_id, "stock": 0}

    ydisks = EmptyStockYdisks()
    service = _service(settings, db, ydisks=ydisks, netdisk=_ok_netdisk())
    product_id = create_test_product(service)
    approval = service.submit_product(product_id)
    service.decide_approval(
        approval["id"], approve=True, actor_open_id="owner-open-id"
    )
    service.worker_once()
    report = service.reconcile()
    assert any(issue["issue"] == "card_stock_empty" for issue in report["issues"])


def test_restart_after_remote_publish_does_not_publish_again(
    settings: Settings, db, tmp_path: Path
) -> None:
    ydisks = DryRunYdisksAdapter()
    service = _service(settings, db, ydisks=ydisks, netdisk=_ok_netdisk())
    product_id = create_test_product(service)
    approval = service.submit_product(product_id)
    service.decide_approval(
        approval["id"], approve=True, actor_open_id="owner-open-id"
    )

    def crash_before_delivery(*args: Any, **kwargs: Any) -> None:
        raise SystemExit("simulated restart")

    service._finalize_delivery = crash_before_delivery  # type: ignore[method-assign]
    with pytest.raises(SystemExit):
        service.worker_once()

    job = service.db.one("SELECT * FROM jobs WHERE kind='publish'")
    assert job is not None and job["status"] == "RUNNING"
    assert service.db.product(product_id)["status"] == ProductStatus.LIVE.value
    assert ydisks.publish_calls == 1
    service.close()

    resumed_db = Database(settings.database_path)
    resumed_db.initialize()
    resumed = _service(
        settings, resumed_db, ydisks=ydisks, netdisk=_ok_netdisk()
    )
    try:
        assert resumed.recover_interrupted_jobs() == 1
        assert resumed.worker_once() is True
        assert ydisks.publish_calls == 1
        assert resumed.db.product(product_id)["status"] == (
            ProductStatus.DELIVERY_READY.value
        )
        bindings = resumed.db.delivery_bindings()
        assert len(bindings) == 1 and bindings[0]["status"] == "READY"
        attempts = resumed.db.all("SELECT * FROM publish_attempts")
        assert len(attempts) == 1
        assert attempts[0]["state"] == "SUCCEEDED"
        assert attempts[0]["day_counted"] == 1
        counters = resumed.db.all("SELECT * FROM daily_counters")
        assert len(counters) == 1
        assert counters[0]["published_count"] == 1
    finally:
        resumed.close()


def test_daily_limit_defers_the_next_approved_item(settings: Settings, db) -> None:
    settings.daily_publish_limit = 1
    ydisks = DryRunYdisksAdapter()
    service = _service(settings, db, ydisks=ydisks, netdisk=_ok_netdisk())

    first_id = create_test_product(service, title="第一件测试商品")
    first_approval = service.submit_product(first_id)
    service.decide_approval(
        first_approval["id"], approve=True, actor_open_id="owner-open-id"
    )
    assert service.worker_once() is True
    assert ydisks.publish_calls == 1

    second_id = create_test_product(service, title="第二件测试商品")
    second_approval = service.submit_product(second_id)
    service.decide_approval(
        second_approval["id"], approve=True, actor_open_id="owner-open-id"
    )
    assert service.worker_once() is True
    assert ydisks.publish_calls == 1
    assert service.db.product(second_id)["status"] == ProductStatus.APPROVED.value
    deferred = service.db.one(
        "SELECT * FROM jobs WHERE product_id=? AND kind='publish'", (second_id,)
    )
    assert deferred is not None
    assert deferred["status"] == "PENDING"
    assert "daily publish limit" in deferred["last_error"]
    assert deferred["available_at"] > deferred["updated_at"]


def _ok_netdisk():
    from xianyu_auto.adapters.netdisk import DryRunNetdiskAdapter

    return DryRunNetdiskAdapter()

