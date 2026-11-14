from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from xianyu_auto.adapters.model import VisionModelError
from xianyu_auto.catalog import product_by_slug
from xianyu_auto.models import ApprovalStatus, ProductStatus
from xianyu_auto.products import build_product
from xianyu_auto.service import Orchestrator


class FakeReviewer:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or {
            "understanding": "希望降低首周价格。",
            "title": "小学常用300字描红字帖",
            "title_reason": "标题保持不变。",
            "price_cents": 490,
            "price_reason": "降低首周决策成本，同时符合最低价格门禁。",
            "notes": [],
            "risks": [],
        }
        self.calls: list[dict[str, Any]] = []

    def review_revision(
        self,
        *,
        product: dict[str, Any],
        feedback: str,
        image_paths: list[Path],
    ) -> tuple[dict[str, Any], str]:
        self.calls.append(
            {
                "product": product,
                "feedback": feedback,
                "image_count": len(image_paths),
            }
        )
        return self.payload, "fake-vision-model"


class FailingReviewer:
    def review_revision(self, **_: Any) -> tuple[dict[str, Any], str]:
        raise VisionModelError("upstream unavailable")


class FakeImageGenerator:
    model = "fake-image-model"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str, **_: object) -> bytes:
        self.prompts.append(prompt)
        buffer = io.BytesIO()
        Image.new("RGB", (1024, 1536), "#1557b0").save(buffer, "PNG")
        return buffer.getvalue()


def _create_generated_product(orchestrator: Orchestrator, tmp_path: Path) -> int:
    manifest = build_product(
        product_by_slug("hanzi-tracing-300"), tmp_path / "generated"
    )
    return orchestrator.register_manifest(manifest)


def _prepare_revision(
    orchestrator: Orchestrator, tmp_path: Path, feedback: str
) -> tuple[int, dict[str, Any]]:
    product_id = _create_generated_product(orchestrator, tmp_path)
    approval = orchestrator.submit_product(product_id)
    orchestrator.handle_feishu_action(
        approval_id=approval["id"],
        action="request_changes",
        actor_open_id="owner-open-id",
        reason=None,
    )
    assert (
        orchestrator.handle_feishu_message(
            actor_open_id="owner-open-id", text=feedback
        )
        == "queued"
    )
    assert orchestrator.worker_once(kind="revision_feedback") is True
    proposal = orchestrator.db.one(
        "SELECT * FROM revision_proposals ORDER BY id DESC LIMIT 1"
    )
    assert proposal is not None
    return product_id, dict(proposal)


def test_feedback_generates_proposal_without_publishing(
    orchestrator: Orchestrator, tmp_path: Path
) -> None:
    reviewer = FakeReviewer()
    orchestrator.reviewer = reviewer
    product_id, proposal = _prepare_revision(
        orchestrator, tmp_path, "价格降到4.9元"
    )

    assert reviewer.calls[0]["image_count"] == 6
    assert proposal["status"] == "PROPOSED"
    details = json.loads(proposal["proposal_json"])
    assert details["proposed_price_cents"] == 490
    assert details["can_apply"] is True
    assert orchestrator.db.product(product_id)["status"] == ProductStatus.DRAFT.value
    assert orchestrator.ydisks.publish_calls == 0  # type: ignore[attr-defined]
    assert orchestrator.feishu.messages[-1]["type"] == "revision_proposal"  # type: ignore[attr-defined]


def test_price_below_one_yuan_is_clamped(
    orchestrator: Orchestrator, tmp_path: Path
) -> None:
    orchestrator.reviewer = FakeReviewer(
        {
            "understanding": "用户希望改为0.1元。",
            "title": "小学常用300字描红字帖",
            "title_reason": "保持不变。",
            "price_cents": 10,
            "price_reason": "用户指定价格。",
            "notes": [],
            "risks": [],
        }
    )
    product_id, proposal = _prepare_revision(
        orchestrator, tmp_path, "价格调整到0.1元"
    )

    details = json.loads(proposal["proposal_json"])
    assert details["proposed_price_cents"] == 100
    assert details["can_apply"] is True
    assert "¥1.00" in details["price_reason"]
    assert any("最低价格门禁" in note for note in details["notes"])
    assert orchestrator.db.product(product_id)["status"] == ProductStatus.DRAFT.value
    assert orchestrator.ydisks.publish_calls == 0  # type: ignore[attr-defined]


def test_one_yuan_minimum_price_is_allowed(
    orchestrator: Orchestrator, tmp_path: Path
) -> None:
    orchestrator.reviewer = FakeReviewer(
        {
            "understanding": "用户希望改为1元。",
            "title": "小学常用300字描红字帖",
            "title_reason": "保持不变。",
            "price_cents": 100,
            "price_reason": "按新的最低价格门禁调整为1元。",
            "notes": [],
            "risks": [],
        }
    )
    _, proposal = _prepare_revision(
        orchestrator, tmp_path, "价格调整到1元"
    )

    details = json.loads(proposal["proposal_json"])
    assert details["proposed_price_cents"] == 100
    assert details["can_apply"] is True


def test_confirmed_proposal_creates_new_version_and_new_approval(
    orchestrator: Orchestrator, tmp_path: Path
) -> None:
    orchestrator.reviewer = FakeReviewer()
    product_id, proposal = _prepare_revision(
        orchestrator, tmp_path, "价格降到4.9元"
    )
    old_approval_id = proposal["approval_id"]

    result = orchestrator.handle_feishu_action(
        approval_id=old_approval_id,
        proposal_id=proposal["id"],
        action="apply_revision",
        actor_open_id="owner-open-id",
        reason=None,
    )

    product = orchestrator.db.product(product_id)
    assert "已生成 v2" in result
    assert product["version"] == 2
    assert product["price_cents"] == 490
    assert product["status"] == ProductStatus.PENDING_APPROVAL.value
    assert (
        orchestrator.db.approval(old_approval_id)["status"]
        == ApprovalStatus.INVALIDATED.value
    )
    new_approval = orchestrator.db.one(
        "SELECT * FROM approvals WHERE product_id=? AND product_version=2",
        (product_id,),
    )
    assert new_approval is not None
    assert new_approval["status"] == ApprovalStatus.PENDING.value
    assert orchestrator.db.revision_proposal(proposal["id"])["status"] == "APPLIED"
    assert orchestrator.ydisks.publish_calls == 0  # type: ignore[attr-defined]


def test_non_owner_cannot_apply_revision(
    orchestrator: Orchestrator, tmp_path: Path
) -> None:
    orchestrator.reviewer = FakeReviewer()
    _, proposal = _prepare_revision(orchestrator, tmp_path, "价格降到4.9元")

    with pytest.raises(PermissionError):
        orchestrator.handle_feishu_action(
            approval_id=proposal["approval_id"],
            proposal_id=proposal["id"],
            action="apply_revision",
            actor_open_id="someone-else",
            reason=None,
        )


def test_image_revision_regenerates_assets_and_requires_new_approval(
    orchestrator: Orchestrator, tmp_path: Path
) -> None:
    generator = FakeImageGenerator()
    orchestrator.image_generator = generator  # type: ignore[assignment]
    orchestrator.reviewer = FakeReviewer(
        {
            "understanding": "希望封面改用更清爽的蓝色插画背景。",
            "title": "小学常用300字描红字帖",
            "title_reason": "标题保持不变。",
            "price_cents": 590,
            "price_reason": "价格保持不变。",
            "regenerate_images": True,
            "image_prompt": (
                "Airy cobalt-blue paper-cut botanical cover, subtle ivory texture."
            ),
            "notes": [],
            "risks": [],
        }
    )
    product_id, proposal = _prepare_revision(
        orchestrator, tmp_path, "封面换成更清爽的蓝色插画"
    )
    details = json.loads(proposal["proposal_json"])
    assert details["regenerate_images"] is True
    assert details["can_apply"] is True

    orchestrator.handle_feishu_action(
        approval_id=proposal["approval_id"],
        proposal_id=proposal["id"],
        action="apply_revision",
        actor_open_id="owner-open-id",
        reason=None,
    )

    product = orchestrator.db.product(product_id)
    assert product["version"] == 2
    assert product["status"] == ProductStatus.PENDING_APPROVAL.value
    assert len(generator.prompts) == 1
    assert "Airy cobalt-blue paper-cut botanical cover" in generator.prompts[0]
    assert orchestrator.ydisks.publish_calls == 0  # type: ignore[attr-defined]


def test_model_failure_never_changes_or_publishes(
    orchestrator: Orchestrator, tmp_path: Path
) -> None:
    orchestrator.reviewer = FailingReviewer()  # type: ignore[assignment]
    product_id = _create_generated_product(orchestrator, tmp_path)
    approval = orchestrator.submit_product(product_id)
    orchestrator.handle_feishu_action(
        approval_id=approval["id"],
        action="request_changes",
        actor_open_id="owner-open-id",
        reason=None,
    )
    orchestrator.handle_feishu_message(
        actor_open_id="owner-open-id", text="价格降到4.9元"
    )
    assert orchestrator.worker_once(kind="revision_feedback") is True

    job = orchestrator.db.one(
        "SELECT * FROM jobs WHERE kind='revision_feedback'"
    )
    assert job is not None and job["status"] == "FAILED"
    assert orchestrator.db.one("SELECT * FROM revision_proposals") is None
    assert orchestrator.db.product(product_id)["status"] == ProductStatus.DRAFT.value
    assert orchestrator.ydisks.publish_calls == 0  # type: ignore[attr-defined]
    assert "分析失败" in orchestrator.feishu.messages[-1]["content"]  # type: ignore[attr-defined]
