from __future__ import annotations

import json
from pathlib import Path

import pytest

from xianyu_auto.checks import canonical_content_hash
from xianyu_auto.models import ApprovalStatus, JobStatus, ProductStatus
from xianyu_auto.service import Orchestrator

from .conftest import create_test_product


def test_unapproved_product_never_calls_publish(orchestrator: Orchestrator) -> None:
    product_id = create_test_product(orchestrator)
    orchestrator.db.enqueue_job(
        "publish",
        "forged-publish",
        product_id=product_id,
        payload={"approval_id": "missing"},
    )
    assert orchestrator.worker_once() is True
    assert orchestrator.ydisks.publish_calls == 0  # type: ignore[attr-defined]
    job = orchestrator.db.one("SELECT * FROM jobs WHERE dedupe_key='forged-publish'")
    assert job is not None and job["status"] == JobStatus.FAILED.value
    assert orchestrator.db.product(product_id)["status"] == ProductStatus.DRAFT.value


def test_non_owner_cannot_approve(orchestrator: Orchestrator) -> None:
    product_id = create_test_product(orchestrator)
    approval = orchestrator.submit_product(product_id)
    with pytest.raises(PermissionError):
        orchestrator.decide_approval(
            approval["id"],
            approve=True,
            actor_open_id="someone-else",
        )
    assert orchestrator.db.product(product_id)["status"] == ProductStatus.PENDING_APPROVAL.value
    assert orchestrator.ydisks.publish_calls == 0  # type: ignore[attr-defined]


def test_duplicate_approval_is_idempotent(orchestrator: Orchestrator) -> None:
    product_id = create_test_product(orchestrator)
    approval = orchestrator.submit_product(product_id)
    first = orchestrator.decide_approval(
        approval["id"], approve=True, actor_open_id="owner-open-id"
    )
    second = orchestrator.decide_approval(
        approval["id"], approve=True, actor_open_id="owner-open-id"
    )
    assert first["changed"] is True
    assert second == {"changed": False, "status": "APPROVED", "reason": "already_decided"}
    jobs = orchestrator.db.all(
        "SELECT * FROM jobs WHERE kind='publish' AND product_id=?", (product_id,)
    )
    assert len(jobs) == 1


def test_changes_requested_returns_to_draft_without_publish(
    orchestrator: Orchestrator,
) -> None:
    product_id = create_test_product(orchestrator)
    approval = orchestrator.submit_product(product_id)
    result = orchestrator.handle_feishu_action(
        approval_id=approval["id"],
        action="request_changes",
        actor_open_id="owner-open-id",
        reason=None,
    )
    assert result == "已暂停上架，请回复修改意见"
    assert (
        orchestrator.db.approval(approval["id"])["status"]
        == ApprovalStatus.CHANGES_REQUESTED.value
    )
    assert orchestrator.db.product(product_id)["status"] == ProductStatus.DRAFT.value
    assert orchestrator.worker_once() is False
    assert orchestrator.ydisks.publish_calls == 0  # type: ignore[attr-defined]


def test_owner_revision_feedback_is_recorded(orchestrator: Orchestrator) -> None:
    product_id = create_test_product(orchestrator)
    approval = orchestrator.submit_product(product_id)
    orchestrator.db.request_changes(approval["id"], actor_open_id="owner-open-id")
    result = orchestrator.handle_feishu_message(
        actor_open_id="owner-open-id",
        text="标题改得更简洁，价格改为 4.9 元",
    )
    assert result == "recorded"
    event = orchestrator.db.one(
        "SELECT * FROM audit_events WHERE event_type='approval.revision_feedback'"
    )
    assert event is not None
    details = json.loads(event["details_json"])
    assert details["feedback"] == "标题改得更简洁，价格改为 4.9 元"
    assert orchestrator.db.product(product_id)["status"] == ProductStatus.DRAFT.value
    assert orchestrator.ydisks.publish_calls == 0  # type: ignore[attr-defined]


def test_non_owner_cannot_request_changes(orchestrator: Orchestrator) -> None:
    product_id = create_test_product(orchestrator)
    approval = orchestrator.submit_product(product_id)
    with pytest.raises(PermissionError):
        orchestrator.db.request_changes(approval["id"], actor_open_id="someone-else")
    assert orchestrator.db.product(product_id)["status"] == ProductStatus.PENDING_APPROVAL.value


def test_content_change_invalidates_approval(
    orchestrator: Orchestrator, tmp_path: Path
) -> None:
    product_id = create_test_product(orchestrator)
    approval = orchestrator.submit_product(product_id)
    product = orchestrator.db.product(product_id)
    assets = [
        {
            "kind": row["kind"],
            "path": row["path"],
            "sha256": row["sha256"],
            "metadata": json.loads(row["metadata_json"]),
        }
        for row in orchestrator.db.all(
            "SELECT * FROM assets WHERE product_id=?", (product_id,)
        )
    ]
    new_hash = canonical_content_hash(
        title="修改后的标题",
        description=product["description"],
        category=product["category"],
        price_cents=product["price_cents"],
        normal_price_cents=product["normal_price_cents"],
        pages=product["pages"],
        asset_hashes=[asset["sha256"] for asset in assets],
    )
    version = orchestrator.db.update_product_content(
        product_id,
        title="修改后的标题",
        description=product["description"],
        category=product["category"],
        price_cents=int(product["price_cents"]),
        normal_price_cents=int(product["normal_price_cents"]),
        pages=int(product["pages"]),
        content_hash=new_hash,
        assets=assets,
    )
    assert version == 2
    stale = orchestrator.db.approval(approval["id"])
    assert stale["status"] == "INVALIDATED"
    assert orchestrator.db.product(product_id)["status"] == ProductStatus.DRAFT.value
    with pytest.raises(ValueError):
        orchestrator.decide_approval(
            approval["id"], approve=True, actor_open_id="owner-open-id"
        )
    assert orchestrator.ydisks.publish_calls == 0  # type: ignore[attr-defined]


def test_approved_flow_binds_item_card_and_rule(orchestrator: Orchestrator) -> None:
    product_id = create_test_product(orchestrator)
    approval = orchestrator.submit_product(product_id)
    orchestrator.decide_approval(
        approval["id"], approve=True, actor_open_id="owner-open-id"
    )
    assert orchestrator.worker_once() is True
    product = orchestrator.db.product(product_id)
    binding = orchestrator.db.one(
        "SELECT * FROM delivery_bindings WHERE product_id=?", (product_id,)
    )
    assert product["status"] == ProductStatus.DELIVERY_READY.value
    assert binding is not None
    assert binding["item_id"]
    assert binding["card_id"]
    assert binding["automation_rule_id"]
    assert binding["status"] == "READY"
    assert orchestrator.ydisks.publish_calls == 1  # type: ignore[attr-defined]
    assert len(orchestrator.ydisks._items) == 1  # type: ignore[attr-defined]
    assert len(orchestrator.ydisks._cards) == 1  # type: ignore[attr-defined]
    assert len(orchestrator.ydisks._rules) == 1  # type: ignore[attr-defined]


def test_audit_proves_owner_approval_before_publish_dispatch(
    orchestrator: Orchestrator,
) -> None:
    product_id = create_test_product(orchestrator)
    approval = orchestrator.submit_product(product_id)
    orchestrator.decide_approval(
        approval["id"], approve=True, actor_open_id="owner-open-id"
    )
    orchestrator.worker_once()
    events = orchestrator.db.all(
        """
        SELECT event_type, actor, details_json, id
        FROM audit_events
        WHERE product_id=?
        ORDER BY id
        """,
        (product_id,),
    )
    approval_index = next(
        index
        for index, event in enumerate(events)
        if event["event_type"] == "approval.decided"
    )
    dispatch_index = next(
        index
        for index, event in enumerate(events)
        if event["event_type"] == "publish.dispatch_started"
    )
    assert approval_index < dispatch_index
    decision = json.loads(events[approval_index]["details_json"])
    dispatch = json.loads(events[dispatch_index]["details_json"])
    assert events[approval_index]["actor"] == "owner-open-id"
    assert decision["decision"] == "APPROVED"
    assert dispatch["approval_id"] == approval["id"]

