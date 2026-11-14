from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .adapters.feishu import DryRunFeishuClient, FeishuClient
from .adapters.model import OpenAICompatibleImageGenerator, OpenAICompatibleVisionModel
from .adapters.netdisk import (
    BaiduNetdiskAdapter,
    DryRunNetdiskAdapter,
    ManualShareRequired,
    ManualNetdiskAdapter,
    NetdiskPort,
    QuarkWebShareAdapter,
)
from .adapters.ydisks import (
    DryRunYdisksAdapter,
    HttpYdisksAdapter,
    PublishUncertainError,
    YdisksPort,
)
from .catalog import PRODUCTS, product_by_slug
from .checks import sha256_file
from .config import Settings
from .db import Database, iso_now
from .models import ApprovalStatus, JobStatus, ProductStatus
from .products import build_all, build_product
from .selection import BrowserKeywordSearch, run_selection


REJECT_REASONS = {
    "content_error",
    "layout_issue",
    "pricing",
    "copyright_risk",
    "other",
}


def explicit_price_cents(text: str) -> int | None:
    matches = re.findall(r"[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*(?:元|块)", text)
    if not matches:
        return None
    try:
        return int(Decimal(matches[-1]) * 100)
    except (InvalidOperation, ValueError):
        return None


class DailyPublishLimitReached(RuntimeError):
    """The approved item must wait for the next local publishing day."""


def price_text(cents: int) -> str:
    return f"{cents / 100:.2f}"


class Orchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        ydisks: YdisksPort,
        netdisk: NetdiskPort,
        feishu: FeishuClient | DryRunFeishuClient,
        reviewer: OpenAICompatibleVisionModel | None = None,
        image_generator: OpenAICompatibleImageGenerator | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.ydisks = ydisks
        self.netdisk = netdisk
        self.feishu = feishu
        self.reviewer = reviewer
        self.image_generator = image_generator

    @classmethod
    def from_settings(cls, settings: Settings) -> "Orchestrator":
        settings.home.mkdir(parents=True, exist_ok=True)
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        settings.assets_dir.mkdir(parents=True, exist_ok=True)
        db = Database(settings.database_path)
        db.initialize()
        if settings.publish_dry_run:
            ydisks: YdisksPort = DryRunYdisksAdapter()
        else:
            if not settings.yd_is_cookie_id:
                raise RuntimeError("yd_is_cookie_id is required when dry-run is disabled")
            ydisks = HttpYdisksAdapter(
                settings.yd_is_base_url,
                cookie_id=settings.yd_is_cookie_id,
                session_cookie=settings.yd_is_session_cookie,
                username=settings.yd_is_username,
                password=settings.yd_is_password,
                publish_location=settings.publish_location,
            )
        if settings.publish_dry_run:
            netdisk: NetdiskPort = DryRunNetdiskAdapter()
        elif settings.netdisk_mode in {"quark", "quark_web"}:
            netdisk = QuarkWebShareAdapter(
                profile_dir=settings.quark_browser_dir,
                headless=True,
            )
        elif settings.netdisk_mode == "baidu":
            from .config import keychain_get

            netdisk = BaiduNetdiskAdapter(
                access_token=keychain_get("baidu_access_token"),
                refresh_token=keychain_get("baidu_refresh_token"),
                app_id=keychain_get("baidu_app_id"),
                app_key=keychain_get("baidu_app_key"),
                app_secret=keychain_get("baidu_app_secret"),
                remote_dir=keychain_get("baidu_remote_dir") or "/XianyuAuto",
                manual_store=settings.netdisk_manual_file,
            )
        elif settings.netdisk_mode == "manual":
            netdisk = ManualNetdiskAdapter(
                settings.netdisk_manual_link, settings.netdisk_manual_code
            )
        else:
            raise RuntimeError(
                f"unsupported netdisk_mode: {settings.netdisk_mode}; "
                "use quark_web, baidu, or manual"
            )
        if settings.feishu_app_id and settings.feishu_app_secret:
            feishu: FeishuClient | DryRunFeishuClient = FeishuClient(
                app_id=settings.feishu_app_id,
                app_secret=settings.feishu_app_secret,
                owner_open_id=settings.owner_open_id,
            )
        elif settings.publish_dry_run:
            feishu = DryRunFeishuClient(settings.owner_open_id or "dry-run-owner")
        else:
            raise RuntimeError("Feishu app credentials are required for real publishing")
        reviewer = None
        if (
            settings.vision_api_key
            and settings.vision_base_url
            and settings.vision_model
        ):
            reviewer = OpenAICompatibleVisionModel(
                base_url=settings.vision_base_url,
                api_key=settings.vision_api_key,
                model=settings.vision_model,
                fallback_model=settings.vision_fallback_model or "glm-4.5v",
            )
        image_generator = None
        if settings.image_generation_enabled:
            image_generator = OpenAICompatibleImageGenerator(
                base_url=settings.image_base_url,
                api_key=settings.image_api_key or "magpie",
                model=settings.image_model,
            )
        return cls(
            settings=settings,
            db=db,
            ydisks=ydisks,
            netdisk=netdisk,
            feishu=feishu,
            reviewer=reviewer,
            image_generator=image_generator,
        )

    def close(self) -> None:
        if hasattr(self.ydisks, "close"):
            self.ydisks.close()  # type: ignore[attr-defined]
        if hasattr(self.netdisk, "close"):
            self.netdisk.close()  # type: ignore[attr-defined]
        if hasattr(self.feishu, "close"):
            self.feishu.close()  # type: ignore[attr-defined]
        self.db.close()

    def recover_interrupted_jobs(self) -> int:
        recovered = self.db.recover_running_jobs()
        if recovered:
            self.db.audit(
                "worker.recovered_interrupted_jobs",
                "system",
                details={"count": recovered},
            )
        return recovered

    def _owner(self, override: str | None = None) -> str:
        owner = override or self.settings.owner_open_id
        if not owner and isinstance(self.feishu, DryRunFeishuClient):
            owner = self.feishu.owner_open_id
        if not owner:
            raise RuntimeError("owner_open_id is not configured")
        return owner

    def register_manifest(self, manifest: dict[str, Any]) -> int:
        existing = self.db.product_by_slug_and_hash(
            manifest["slug"], manifest["content_hash"]
        )
        if existing:
            return int(existing["id"])
        assets = [
            {
                "kind": asset["kind"],
                "path": asset["path"],
                "sha256": asset["sha256"],
                "metadata": asset.get("metadata", {}),
            }
            for asset in manifest["assets"]
        ]
        return self.db.create_product(
            slug=manifest["slug"],
            title=manifest["title"],
            description=manifest["description"],
            category=manifest["category"],
            price_cents=int(manifest["price_cents"]),
            normal_price_cents=int(manifest["normal_price_cents"]),
            pages=int(manifest["pages"]),
            content_hash=manifest["content_hash"],
            risk_flags=list(manifest.get("risk_flags", [])),
            metadata=manifest.get("metadata", {}),
            preview_path=manifest.get("preview_path"),
            assets=assets,
        )

    def generate_samples(self, *, register: bool = True) -> list[dict[str, Any]]:
        manifests = build_all(
            self.settings.assets_dir / "samples",
            image_generator=self.image_generator,
        )
        if register:
            for manifest in manifests:
                manifest["product_id"] = self.register_manifest(manifest)
        return manifests

    def register_existing_samples(self) -> list[int]:
        ids: list[int] = []
        for spec in PRODUCTS:
            manifest_path = (
                self.settings.assets_dir / "samples" / spec.slug / "product.json"
            )
            if not manifest_path.exists():
                manifest = build_product(
                    spec,
                    self.settings.assets_dir / "samples",
                    image_generator=self.image_generator,
                )
            else:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            ids.append(self.register_manifest(manifest))
        return ids

    def _product_assets(self, product_id: int) -> list[sqlite3.Row]:
        return self.db.all(
            "SELECT * FROM assets WHERE product_id=? ORDER BY id", (product_id,)
        )

    def _approval_checks(self, product_id: int) -> tuple[list[str], str]:
        assets = self._product_assets(product_id)
        pdf_assets = [asset for asset in assets if asset["kind"] == "pdf"]
        if len(pdf_assets) != 1:
            raise RuntimeError(f"expected exactly one PDF, got {len(pdf_assets)}")
        metadata = json.loads(pdf_assets[0]["metadata_json"])
        checks = [
            f"PDF 页数 {metadata.get('pages')}",
            f"嵌入字体 {len(metadata.get('embedded_fonts', []))} 种",
            "SHA-256 已记录",
            "标题、价格、描述、素材共同绑定审批版本",
        ]
        return checks, _human_size(int(metadata.get("size_bytes", 0)))

    def submit_product(
        self, product_id: int, *, owner_open_id: str | None = None
    ) -> dict[str, Any]:
        owner = self._owner(owner_open_id)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        approval = self.db.submit_for_approval(product_id, owner, expires_at)
        product = self.db.product(product_id)
        assets = self._product_assets(product_id)
        images = [Path(asset["path"]) for asset in assets if asset["kind"] == "image"][:3]
        checks, file_size = self._approval_checks(product_id)
        try:
            message_id, _ = self.feishu.send_approval_card(
                approval_id=approval["id"],
                title=product["title"],
                price=price_text(int(product["price_cents"])),
                normal_price=price_text(int(product["normal_price_cents"])),
                category=product["category"],
                pages=int(product["pages"]),
                file_size=file_size,
                content_hash=product["content_hash"],
                checks=checks,
                risks=json.loads(product["risk_flags_json"]),
                preview_images=images,
            )
        except Exception as exc:
            self.db.invalidate_pending_approvals(
                product_id,
                actor="system",
                reason=f"approval_card_delivery_failed:{type(exc).__name__}",
            )
            raise
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE approvals SET card_message_id=? WHERE id=?",
                (message_id, approval["id"]),
            )
            self.db.audit(
                "approval.card_sent",
                "system",
                product_id,
                {"approval_id": approval["id"], "message_id": message_id},
                conn=conn,
            )
        return {**approval, "message_id": message_id}

    def decide_approval(
        self,
        approval_id: str,
        *,
        approve: bool,
        actor_open_id: str,
        reject_reason: str | None = None,
    ) -> dict[str, Any]:
        if not approve and reject_reason not in REJECT_REASONS:
            raise ValueError("reject reason is required and must be a supported value")
        result = self.db.decide_approval(
            approval_id,
            actor_open_id=actor_open_id,
            approve=approve,
            reject_reason=reject_reason,
        )
        if result.get("changed"):
            text = (
                "审批通过，商品已进入发布队列。系统会先生成网盘链接，再执行发布。"
                if approve
                else f"商品已驳回，原因：{reject_reason}。未调用发布接口。"
            )
            try:
                self.feishu.send_text(text)
            except Exception:
                pass
        return result

    def handle_feishu_action(
        self,
        *,
        approval_id: str | None,
        proposal_id: str | None = None,
        action: str | None,
        actor_open_id: str | None,
        reason: str | None,
    ) -> str:
        if not action or not actor_open_id:
            raise ValueError("审批动作缺少必要字段")
        if action == "apply_revision":
            if not proposal_id:
                raise ValueError("修改方案动作缺少 proposal_id")
            return self.apply_revision_proposal(
                proposal_id, actor_open_id=actor_open_id
            )
        if action == "request_more_changes":
            if not proposal_id:
                raise ValueError("修改意见动作缺少 proposal_id")
            proposal = self.db.revision_proposal(proposal_id)
            approval = self.db.approval(str(proposal["approval_id"]))
            if actor_open_id != approval["owner_open_id"]:
                raise PermissionError("owner only")
            return "请直接在机器人单聊中回复新的修改意见"
        if not approval_id:
            raise ValueError("审批动作缺少 approval_id")
        approval = self.db.approval(approval_id)
        if actor_open_id != approval["owner_open_id"]:
            raise PermissionError("owner only")
        title = self.db.product(int(approval["product_id"]))["title"]
        if action == "preview":
            return "审批卡片中已展示 3 张预览图；文件哈希和检查结果见卡片正文"
        if action == "reject":
            self.feishu.send_reject_reason_card(approval_id, title)
            return "请选择驳回原因"
        if action == "request_changes":
            result = self.db.request_changes(
                approval_id,
                actor_open_id=actor_open_id,
            )
            if result.get("changed"):
                self.feishu.send_text(
                    f"《{title}》已暂停上架。请直接回复你想修改的内容，"
                    "系统收到后会保留为修改意见，商品不会发布。"
                )
                return "已暂停上架，请回复修改意见"
            return "该审批已处理，无需重复操作"
        if action == "approve":
            result = self.decide_approval(
                approval_id,
                approve=True,
                actor_open_id=actor_open_id,
            )
            return "已通过" if result.get("changed") else "该审批已处理，无需重复操作"
        if action == "reject_reason":
            if reason not in REJECT_REASONS:
                raise ValueError("无效驳回原因")
            result = self.decide_approval(
                approval_id,
                approve=False,
                actor_open_id=actor_open_id,
                reject_reason=reason,
            )
            return "已驳回" if result.get("changed") else "该审批已处理，无需重复操作"
        raise ValueError(f"unknown action: {action}")

    def handle_feishu_message(self, *, actor_open_id: str, text: str) -> str:
        feedback = text.strip()
        if actor_open_id != self.settings.owner_open_id:
            return "ignored"
        if not feedback:
            return "ignored"
        approval = self.db.latest_revision_request(actor_open_id)
        if approval is None:
            return "ignored"
        self.db.record_revision_feedback(
            approval["id"],
            actor_open_id=actor_open_id,
            feedback=feedback,
        )
        preview = feedback if len(feedback) <= 120 else f"{feedback[:120]}…"
        if self.reviewer is None:
            self.feishu.send_text(
                f"修改意见已记录给《{approval['title']}》：\n{preview}\n"
                "视觉模型尚未配置，商品仍为草稿，不会上架。"
            )
            return "recorded"
        feedback_hash = hashlib.sha256(
            f"{approval['id']}\0{feedback}".encode("utf-8")
        ).hexdigest()[:24]
        queued = self.db.enqueue_job(
            "revision_feedback",
            f"revision-feedback:{feedback_hash}",
            product_id=int(approval["product_id"]),
            payload={"approval_id": approval["id"], "feedback": feedback},
        )
        self.feishu.send_text(
            f"修改意见已收到给《{approval['title']}》：\n{preview}\n"
            "正在结合商品图分析可执行修改方案，完成后会发回飞书。"
            "商品仍为草稿，不会上架。"
        )
        return "queued" if queued else "recorded"

    def _normalize_revision_proposal(
        self,
        product: sqlite3.Row,
        feedback: str,
        raw: dict[str, Any],
    ) -> dict[str, Any]:
        current_title = str(product["title"])
        current_price = int(product["price_cents"])
        normal_price = int(product["normal_price_cents"])
        raw_notes = raw.get("notes", [])
        raw_risks = raw.get("risks", [])
        notes = [
            str(item)[:300]
            for item in (raw_notes if isinstance(raw_notes, list) else [raw_notes])
            if str(item).strip()
        ][:6]
        risks = [
            str(item)[:300]
            for item in (raw_risks if isinstance(raw_risks, list) else [raw_risks])
            if str(item).strip()
        ][:6]
        can_apply = True
        regenerate_images = bool(raw.get("regenerate_images"))
        image_prompt = str(raw.get("image_prompt") or "").strip()[:1200]
        if regenerate_images and not image_prompt:
            notes.append("模型未提供可执行的生图提示词，已拦截图片重绘。")
            can_apply = False
        if regenerate_images and self.image_generator is None:
            notes.append("Magpie 生图网关未配置，已拦截图片重绘。")
            can_apply = False
        proposed_title = str(raw.get("title") or current_title).strip()
        title_reason = str(raw.get("title_reason") or "").strip()[:500]
        if (
            not proposed_title
            or len(proposed_title) > 30
            or any(
                term in proposed_title
                for term in ("微信", "二维码", "扫码", "加V", "加v", "外链", "QQ")
            )
        ):
            notes.append("标题建议超过30字、为空或包含引流内容，已拦截并保留原标题。")
            proposed_title = current_title
            can_apply = False

        raw_price = raw.get("price_cents")
        try:
            proposed_price = int(raw_price)
        except (TypeError, ValueError):
            proposed_price = current_price
            can_apply = False
            notes.append("价格建议不是整数，已拦截并保留原价。")
        price_reason = str(raw.get("price_reason") or "").strip()[:500]
        requested_price = explicit_price_cents(feedback)
        if requested_price is not None:
            proposed_price = requested_price
            if requested_price < 100:
                proposed_price = 100
                price_reason = (
                    f"你明确要求 ¥{requested_price / 100:.2f}；"
                    "系统最低价为 ¥1.00，因此方案取 ¥1.00。"
                )
                notes.append("已按新的最低价格门禁将明确报价调整为 ¥1.00。")
            else:
                price_reason = (
                    f"按你的明确报价 ¥{requested_price / 100:.2f} 生成修改方案。"
                )
        if proposed_price < 100 or proposed_price > 1990 or proposed_price > normal_price:
            notes.append(
                f"价格建议 ¥{proposed_price / 100:.2f} 超出允许范围或高于正常价，"
                "已拦截并保留原价。"
            )
            proposed_price = current_price
            can_apply = False
        if (
            proposed_title == current_title
            and proposed_price == current_price
            and not regenerate_images
        ):
            can_apply = False
            if not notes:
                notes.append("没有可自动执行的标题或价格变化。")

        return {
            "understanding": str(raw.get("understanding") or "")[:800],
            "feedback": feedback,
            "current_title": current_title,
            "proposed_title": proposed_title,
            "title_reason": title_reason,
            "current_price_cents": current_price,
            "proposed_price_cents": proposed_price,
            "normal_price_cents": normal_price,
            "price_reason": price_reason,
            "regenerate_images": regenerate_images,
            "image_prompt": image_prompt if regenerate_images else "",
            "notes": notes,
            "risks": risks,
            "can_apply": can_apply,
        }

    def _analyze_revision_feedback(self, job: dict[str, Any]) -> None:
        if self.reviewer is None:
            raise RuntimeError("vision model is not configured")
        payload = json.loads(job["payload_json"])
        approval = self.db.approval(str(payload["approval_id"]))
        product = self.db.product(int(approval["product_id"]))
        if (
            approval["status"] != ApprovalStatus.CHANGES_REQUESTED.value
            or product["status"] != ProductStatus.DRAFT.value
            or product["version"] != approval["product_version"]
            or product["content_hash"] != approval["content_hash"]
        ):
            self.db.audit(
                "revision.analysis_skipped",
                "worker",
                int(product["id"]),
                {"approval_id": approval["id"], "reason": "stale_or_decided"},
            )
            return
        images = [
            Path(asset["path"])
            for asset in self._product_assets(int(product["id"]))
            if asset["kind"] == "image"
        ]
        raw, model = self.reviewer.review_revision(
            product={
                "title": product["title"],
                "description": product["description"],
                "category": product["category"],
                "price_cents": int(product["price_cents"]),
                "normal_price_cents": int(product["normal_price_cents"]),
                "pages": int(product["pages"]),
            },
            feedback=str(payload["feedback"]),
            image_paths=images,
        )
        proposal = self._normalize_revision_proposal(
            product, str(payload["feedback"]), raw
        )
        proposal_id = self.db.create_revision_proposal(
            approval_id=approval["id"],
            product_id=int(product["id"]),
            product_version=int(product["version"]),
            content_hash=product["content_hash"],
            feedback=str(payload["feedback"]),
            proposal=proposal,
            model=model,
        )
        message_id = self.feishu.send_revision_proposal_card(
            proposal_id=proposal_id,
            approval_id=approval["id"],
            title=product["title"],
            feedback=str(payload["feedback"]),
            current_title=proposal["current_title"],
            proposed_title=proposal["proposed_title"],
            current_price=price_text(proposal["current_price_cents"]),
            proposed_price=price_text(proposal["proposed_price_cents"]),
            understanding=proposal["understanding"],
            title_reason=proposal["title_reason"],
            price_reason=proposal["price_reason"],
            image_change=(
                "使用 GPT Image 生成一套统一详情图背景并重绘 6 张详情图，"
                "中文、价格和页面示意仍由程序叠加。"
                if proposal["regenerate_images"]
                else "不修改详情图。"
            ),
            notes=proposal["notes"],
            risks=proposal["risks"],
            can_apply=proposal["can_apply"],
        )
        self.db.set_revision_proposal_card(proposal_id, message_id)

    def apply_revision_proposal(
        self, proposal_id: str, *, actor_open_id: str
    ) -> str:
        proposal_row = self.db.revision_proposal(proposal_id)
        approval = self.db.approval(str(proposal_row["approval_id"]))
        if actor_open_id != approval["owner_open_id"]:
            raise PermissionError("owner only")
        if proposal_row["status"] == "APPLIED":
            return "该修改方案已应用，无需重复操作"
        if proposal_row["status"] != "PROPOSED":
            raise ValueError("该修改方案已失效，请重新提交修改意见")
        product = self.db.product(int(proposal_row["product_id"]))
        if (
            approval["status"] != ApprovalStatus.CHANGES_REQUESTED.value
            or product["status"] != ProductStatus.DRAFT.value
            or product["version"] != proposal_row["product_version"]
            or product["content_hash"] != proposal_row["content_hash"]
        ):
            self.db.mark_revision_proposal_stale(
                proposal_id, actor="system", reason="stale_or_decided"
            )
            raise ValueError("商品或审批已变化，请重新提交修改意见")
        proposal = json.loads(proposal_row["proposal_json"])
        if not proposal.get("can_apply"):
            raise ValueError("该方案包含被门禁拦截或不可自动执行的修改")
        try:
            spec = product_by_slug(str(product["slug"]))
        except KeyError as exc:
            raise ValueError("当前商品没有可用的自动重生成模板") from exc
        next_version = int(product["version"]) + 1
        revised_spec = replace(
            spec,
            title=str(proposal["proposed_title"]),
            price_cents=int(proposal["proposed_price_cents"]),
            normal_price_cents=max(
                int(product["normal_price_cents"]),
                int(proposal["proposed_price_cents"]),
            ),
        )
        output_root = self.settings.assets_dir / "revisions" / f"v{next_version}"
        regenerate_images = bool(proposal.get("regenerate_images"))
        if regenerate_images and self.image_generator is None:
            raise ValueError("Magpie 生图网关未配置，无法执行图片重绘")
        manifest = build_product(
            revised_spec,
            output_root,
            image_generator=self.image_generator,
            image_prompt=str(proposal.get("image_prompt") or "") or None,
            require_generated_image=regenerate_images,
        )
        self.db.update_product_content(
            int(product["id"]),
            title=manifest["title"],
            description=manifest["description"],
            category=manifest["category"],
            price_cents=int(manifest["price_cents"]),
            normal_price_cents=int(manifest["normal_price_cents"]),
            pages=int(manifest["pages"]),
            content_hash=manifest["content_hash"],
            assets=[
                {
                    "kind": asset["kind"],
                    "path": asset["path"],
                    "sha256": asset["sha256"],
                    "metadata": asset.get("metadata", {}),
                }
                for asset in manifest["assets"]
            ],
            actor=actor_open_id,
        )
        self.db.mark_revision_proposal_applied(
            proposal_id,
            actor_open_id=actor_open_id,
            new_product_version=next_version,
        )
        approval_result = self.submit_product(
            int(product["id"]), owner_open_id=actor_open_id
        )
        return (
            f"方案已应用，已生成 v{next_version} 并重新发起审批"
            f"（{approval_result['id']}），仍需你点击“通过并上架”。"
        )

    def send_pending_reminders(self) -> list[str]:
        sent: list[str] = []
        rows = self.db.all("SELECT * FROM approvals WHERE status='PENDING'")
        now = datetime.now(timezone.utc)
        for approval in rows:
            created_at = datetime.fromisoformat(approval["created_at"])
            age = now - created_at
            product = self.db.product(int(approval["product_id"]))
            if age >= timedelta(hours=24) and not approval["reminder_24h_sent_at"]:
                self.feishu.send_text(
                    f"审批提醒（24 小时）：《{product['title']}》仍在等待你处理。"
                    "7 天未处理会自动驳回，不会发布。"
                )
                self.db.audit(
                    "approval.reminder_24h",
                    "scheduler",
                    int(approval["product_id"]),
                    {"approval_id": approval["id"]},
                )
                with self.db.transaction() as conn:
                    conn.execute(
                        "UPDATE approvals SET reminder_24h_sent_at=? WHERE id=?",
                        (iso_now(), approval["id"]),
                    )
                sent.append(approval["id"])
            elif age >= timedelta(hours=2) and not approval["reminder_2h_sent_at"]:
                self.feishu.send_text(
                    f"审批提醒（2 小时）：《{product['title']}》等待你审批。"
                )
                self.db.audit(
                    "approval.reminder_2h",
                    "scheduler",
                    int(approval["product_id"]),
                    {"approval_id": approval["id"]},
                )
                with self.db.transaction() as conn:
                    conn.execute(
                        "UPDATE approvals SET reminder_2h_sent_at=? WHERE id=?",
                        (iso_now(), approval["id"]),
                    )
                sent.append(approval["id"])
        return sent

    def _start_publish_attempt(
        self, product: sqlite3.Row, approval: sqlite3.Row
    ) -> tuple[sqlite3.Row, bool]:
        key = f"xianyu:{product['slug']}:v{product['version']}:{product['content_hash'][:24]}"
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM publish_attempts WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing:
                return existing, False
            conn.execute(
                """
                INSERT INTO publish_attempts(
                  product_id, approval_id, content_hash, idempotency_key,
                  state, request_json, started_at
                ) VALUES (?, ?, ?, ?, 'PREPARED', ?, ?)
                """,
                (
                    product["id"],
                    approval["id"],
                    product["content_hash"],
                    key,
                    json.dumps(
                        {
                            "title": product["title"],
                            "description": product["description"],
                            "price_cents": product["price_cents"],
                            "normal_price_cents": product["normal_price_cents"],
                        },
                        ensure_ascii=False,
                    ),
                    iso_now(),
                ),
            )
            created = conn.execute(
                "SELECT * FROM publish_attempts WHERE idempotency_key=?", (key,)
            ).fetchone()
            assert created is not None
            self.db.audit(
                "publish.attempt_prepared",
                "worker",
                int(product["id"]),
                {
                    "approval_id": approval["id"],
                    "attempt_id": created["id"],
                    "idempotency_key": key,
                    "content_hash": product["content_hash"],
                },
                conn=conn,
            )
            return created, True

    def _mark_attempt(
        self,
        attempt_id: int,
        *,
        state: str,
        response: dict[str, Any] | None = None,
        item_id: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE publish_attempts
                SET state=?, response_json=?, item_id=?, error=?, finished_at=?
                WHERE id=?
                """,
                (
                    state,
                    json.dumps(response or {}, ensure_ascii=False),
                    item_id,
                    error,
                    iso_now(),
                    attempt_id,
                ),
            )

    def _reconcile_uncertain_publish(
        self, product: sqlite3.Row, attempt: sqlite3.Row
    ) -> str | None:
        matches = self.ydisks.find_items_by_title(product["title"])
        if len(matches) == 1:
            item_id = str(
                matches[0].get("item_id")
                or matches[0].get("id")
                or matches[0].get("itemId")
            )
            if item_id and item_id != "None":
                self._mark_attempt(
                    int(attempt["id"]),
                    state="SUCCEEDED",
                    response={"reconciled": True, "item": matches[0]},
                    item_id=item_id,
                )
                self.db.audit(
                    "publish.reconciled_existing_item",
                    "system",
                    int(product["id"]),
                    {"item_id": item_id, "attempt_id": attempt["id"]},
                )
                return item_id
        if len(matches) > 1:
            self.db.audit(
                "publish.reconcile_multiple_matches",
                "system",
                int(product["id"]),
                {"count": len(matches), "attempt_id": attempt["id"]},
            )
        return None

    def _ensure_card(
        self, product: sqlite3.Row, link: str, code: str
    ) -> int:
        name = f"{product['title']} · 网盘资料"
        existing = self.ydisks.find_cards_by_name(name)
        if len(existing) > 1:
            raise RuntimeError("multiple cards with the same product name exist")
        if len(existing) == 1:
            return int(existing[0]["id"])
        if code == "无需提取码":
            delivery = f"下载链接：{link}\n提取码：无需提取码\n"
        else:
            delivery = f"资料链接：{link}\n提取码：{code}\n"
        content = (
            delivery
            + "使用说明：请下载后打印。电子资料不退换，"
            "如有链接问题请在订单内联系卖家。"
        )
        return self.ydisks.create_text_card(
            name=name,
            content=content,
            description=f"{product['slug']} v{product['version']} / {product['content_hash'][:16]}",
        )

    def _ensure_rule(
        self, product: sqlite3.Row, item_id: str, card_id: int
    ) -> int:
        rules = self.ydisks.find_rules_for_item(item_id)
        enabled = [rule for rule in rules if bool(rule.get("enabled", True))]
        if len(enabled) > 1:
            raise RuntimeError("multiple enabled delivery rules for one item")
        if len(enabled) == 1:
            return int(enabled[0]["id"])
        return self.ydisks.create_paid_delivery_rule(
            item_id=item_id,
            title=product["title"],
            card_id=card_id,
        )

    def _finalize_delivery(
        self,
        product: sqlite3.Row,
        *,
        item_id: str,
        link: str,
        code: str,
    ) -> None:
        current = self.db.product(int(product["id"]))
        if ProductStatus(current["status"]) not in {
            ProductStatus.LIVE,
            ProductStatus.DELIVERY_FAILED,
        }:
            raise RuntimeError(
                f"cannot configure delivery from status {current['status']}"
            )
        try:
            card_id = self._ensure_card(current, link, code)
            self.db.save_delivery_binding(
                int(current["id"]),
                item_id=item_id,
                netdisk_link=link,
                extract_code=code,
                card_id=card_id,
                status="CARD_READY",
            )
            rule_id = self._ensure_rule(current, item_id, card_id)
            self.db.save_delivery_binding(
                int(current["id"]),
                item_id=item_id,
                netdisk_link=link,
                extract_code=code,
                card_id=card_id,
                automation_rule_id=rule_id,
                status="READY",
            )
            self.db.set_product_status(
                int(current["id"]),
                ProductStatus.DELIVERY_READY,
                actor="worker",
                details={"item_id": item_id, "card_id": card_id, "rule_id": rule_id},
            )
        except Exception as exc:
            if ProductStatus(current["status"]) == ProductStatus.LIVE:
                self.db.set_product_status(
                    int(current["id"]),
                    ProductStatus.DELIVERY_FAILED,
                    actor="worker",
                    details={"item_id": item_id, "error": str(exc)},
                )
            self.db.enqueue_job(
                "delivery_repair",
                f"delivery-repair:{current['id']}:{item_id}",
                product_id=int(current["id"]),
                payload={"item_id": item_id, "link": link, "code": code},
            )
            raise

    def _attempt_key(self, product: sqlite3.Row) -> str:
        return (
            f"xianyu:{product['slug']}:v{product['version']}:"
            f"{product['content_hash'][:24]}"
        )

    def _promote_live_and_count(
        self, product: sqlite3.Row, attempt: sqlite3.Row, item_id: str
    ) -> None:
        current = self.db.product(int(product["id"]))
        status = ProductStatus(current["status"])
        if status == ProductStatus.APPROVED:
            self.db.set_product_status(
                int(current["id"]),
                ProductStatus.PUBLISHING,
                actor="worker",
                details={"attempt_id": attempt["id"], "recovered": True},
            )
            current = self.db.product(int(product["id"]))
            status = ProductStatus(current["status"])
        if status == ProductStatus.PUBLISHING:
            self.db.set_product_status(
                int(current["id"]),
                ProductStatus.LIVE,
                actor="worker",
                details={"attempt_id": attempt["id"], "item_id": item_id},
            )
        elif status not in {
            ProductStatus.LIVE,
            ProductStatus.DELIVERY_FAILED,
            ProductStatus.DELIVERY_READY,
        }:
            raise RuntimeError(f"cannot promote publish from status {status.value}")

        day = datetime.now(ZoneInfo(self.settings.timezone)).date().isoformat()
        with self.db.transaction() as conn:
            counted = self.db.count_publish_once(
                int(attempt["id"]), day, conn=conn
            )
        if counted:
            self.db.audit(
                "publish.daily_count_recorded",
                "worker",
                int(product["id"]),
                {"attempt_id": attempt["id"], "day": day},
            )

    def _publish(self, job: dict[str, Any]) -> None:
        product_id = int(job["product_id"])
        product = self.db.product(product_id)
        payload = json.loads(job["payload_json"])
        attempt = self.db.one(
            "SELECT * FROM publish_attempts WHERE idempotency_key=?",
            (self._attempt_key(product),),
        )
        approval = self.db.valid_approval_for_product(product_id)
        if attempt is None and ProductStatus(product["status"]) != ProductStatus.APPROVED:
            raise RuntimeError(f"publish requires APPROVED, got {product['status']}")
        if approval is None:
            raise PermissionError("no valid owner approval for current content hash")
        expected_approval_id = attempt["approval_id"] if attempt else approval["id"]
        if payload.get("approval_id") != expected_approval_id:
            raise PermissionError("publish job approval does not match valid approval")

        day = datetime.now(ZoneInfo(self.settings.timezone)).date().isoformat()
        if (
            attempt is None
            and self.db.daily_publish_count(day) >= self.settings.daily_publish_limit
        ):
            raise DailyPublishLimitReached(
                f"daily publish limit reached for {day}; item remains approved"
            )

        pdf_assets = [
            asset
            for asset in self._product_assets(product_id)
            if asset["kind"] == "pdf"
        ]
        if len(pdf_assets) != 1:
            raise RuntimeError("exactly one PDF is required")
        pdf_path = Path(pdf_assets[0]["path"])
        if sha256_file(pdf_path) != pdf_assets[0]["sha256"]:
            raise RuntimeError("PDF changed after approval; approval must be regenerated")

        if attempt is None:
            attempt, _ = self._start_publish_attempt(product, approval)

        if attempt["state"] in {"UNCERTAIN", "SENT"}:
            item_id = self._reconcile_uncertain_publish(product, attempt)
            if not item_id:
                if ProductStatus(product["status"]) == ProductStatus.PUBLISHING:
                    self.db.set_product_status(
                        product_id,
                        ProductStatus.PUBLISH_FAILED,
                        actor="worker",
                        details={"error": "publish uncertain; manual reconciliation required"},
                    )
                raise RuntimeError(
                    "previous publish is uncertain; check Ydisks before manual intervention"
                )
            attempt = self.db.one(
                "SELECT * FROM publish_attempts WHERE id=?", (attempt["id"],)
            )
            assert attempt is not None
        elif attempt["state"] == "SUCCEEDED":
            item_id = attempt["item_id"]
            binding = self.db.one(
                "SELECT * FROM delivery_bindings WHERE product_id=?", (product_id,)
            )
            link = binding["netdisk_link"] if binding else None
            code = binding["extract_code"] if binding else None
            if not item_id or not link or not code:
                raise RuntimeError("succeeded attempt lacks delivery details")
            self._promote_live_and_count(product, attempt, str(item_id))
            self._finalize_delivery(
                product, item_id=str(item_id), link=str(link), code=str(code)
            )
            return
        elif attempt["state"] == "PREPARED":
            pass
        else:
            raise RuntimeError(f"publish attempt is {attempt['state']}, not retryable")

        binding = self.db.one(
            "SELECT * FROM delivery_bindings WHERE product_id=?", (product_id,)
        )
        link = binding["netdisk_link"] if binding else None
        code = binding["extract_code"] if binding else None
        if not link or not code:
            share = self.netdisk.share_file(
                pdf_path,
                f"{product['slug']}-v{product['version']}.pdf",
                share_key=str(product["slug"]),
            )
            link = share.link
            code = share.extract_code
            self.db.save_delivery_binding(
                product_id,
                netdisk_link=link,
                extract_code=code,
                status="LINK_READY",
            )
        current_status = ProductStatus(self.db.product(product_id)["status"])
        if current_status == ProductStatus.APPROVED:
            self.db.set_product_status(
                product_id,
                ProductStatus.PUBLISHING,
                actor="worker",
                details={"approval_id": approval["id"], "attempt_id": attempt["id"]},
            )
        elif current_status != ProductStatus.PUBLISHING:
            raise RuntimeError(
                f"publish preparation requires APPROVED or PUBLISHING, got {current_status.value}"
            )
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE publish_attempts SET state='SENT' WHERE id=?",
                (attempt["id"],),
            )
        self.db.audit(
            "publish.dispatch_started",
            "worker",
            product_id,
            {"approval_id": approval["id"], "attempt_id": attempt["id"]},
        )
        images = [
            Path(asset["path"])
            for asset in self._product_assets(product_id)
            if asset["kind"] == "image"
        ]
        try:
            result = self.ydisks.publish_item(
                title=product["title"],
                description=product["description"],
                price_cents=int(product["price_cents"]),
                normal_price_cents=int(product["normal_price_cents"]),
                images=images,
            )
        except PublishUncertainError as exc:
            self._mark_attempt(
                int(attempt["id"]), state="UNCERTAIN", error=str(exc)
            )
            refreshed = self.db.one(
                "SELECT * FROM publish_attempts WHERE id=?", (attempt["id"],)
            )
            assert refreshed is not None
            item_id = self._reconcile_uncertain_publish(product, refreshed)
            if not item_id:
                self.db.set_product_status(
                    product_id,
                    ProductStatus.PUBLISH_FAILED,
                    actor="worker",
                    details={"error": "publish timeout; manual reconciliation required"},
                )
                raise RuntimeError(
                    "publish timed out; no retry attempted. Check Ydisks items manually."
                ) from exc
            result = None
        except Exception as exc:
            self._mark_attempt(
                int(attempt["id"]), state="FAILED", error=str(exc)
            )
            self.db.set_product_status(
                product_id,
                ProductStatus.PUBLISH_FAILED,
                actor="worker",
                details={"error": str(exc)},
            )
            raise

        if result is not None:
            item_id = result.item_id
            self._mark_attempt(
                int(attempt["id"]),
                state="SUCCEEDED",
                response=result.raw,
                item_id=item_id,
            )
            refreshed_attempt = self.db.one(
                "SELECT * FROM publish_attempts WHERE id=?", (attempt["id"],)
            )
            assert refreshed_attempt is not None
            attempt = refreshed_attempt
        self._promote_live_and_count(product, attempt, str(item_id))
        self._finalize_delivery(
            product,
            item_id=str(item_id),
            link=str(link),
            code=str(code),
        )

    def _repair_delivery(self, job: dict[str, Any]) -> None:
        product = self.db.product(int(job["product_id"]))
        binding = self.db.one(
            "SELECT * FROM delivery_bindings WHERE product_id=?", (product["id"],)
        )
        payload = json.loads(job["payload_json"])
        item_id = payload.get("item_id") or (binding["item_id"] if binding else None)
        link = payload.get("link") or (binding["netdisk_link"] if binding else None)
        code = payload.get("code") or (binding["extract_code"] if binding else None)
        if not all((item_id, link, code)):
            raise RuntimeError("delivery repair lacks item_id, link or code")
        self._finalize_delivery(
            product, item_id=str(item_id), link=str(link), code=str(code)
        )

    def worker_once(self, *, kind: str | None = None) -> bool:
        job = self.db.claim_next_job(kind)
        if job is None:
            return False
        try:
            if job["kind"] == "publish":
                self._publish(job)
            elif job["kind"] == "delivery_repair":
                self._repair_delivery(job)
            elif job["kind"] == "reconcile":
                report = self.reconcile()
                self.db.finish_job(
                    int(job["id"]), JobStatus.SUCCEEDED, payload_patch=report
                )
                return True
            elif job["kind"] == "selection":
                search = (
                    BrowserKeywordSearch(
                        profile_dir=self.settings.home / "selection-browser",
                        headless=True,
                    )
                    if self.settings.raw.get("selection_live", False)
                    else None
                )
                report = run_selection(
                    cache_path=self.settings.home / "cache" / "selection-cache.json",
                    search=search,
                )
                self.db.finish_job(
                    int(job["id"]), JobStatus.SUCCEEDED, payload_patch=report
                )
                return True
            elif job["kind"] == "revision_feedback":
                self._analyze_revision_feedback(job)
            else:
                raise RuntimeError(f"unknown job kind: {job['kind']}")
        except Exception as exc:
            if isinstance(exc, ManualShareRequired):
                next_check = (
                    datetime.now(timezone.utc) + timedelta(minutes=10)
                ).isoformat()
                job_payload = json.loads(job["payload_json"])
                notified_for = job_payload.get("manual_share_notified_for")
                self.db.defer_job(
                    int(job["id"]),
                    available_at=next_check,
                    reason=str(exc),
                    payload_patch={
                        "manual_share": {
                            "share_key": exc.share_key,
                            "remote_name": exc.remote_name,
                            "remote_path": exc.remote_path,
                        },
                        "manual_share_notified_for": exc.share_key,
                    },
                )
                if notified_for != exc.share_key:
                    try:
                        self.feishu.send_text(
                            "百度网盘商品目录已准备好，但当前应用没有自动分享权限，"
                            "商品尚未发布。请在百度网盘 App 中一次性分享这个目录：\n"
                            f"{exc.remote_path}\n"
                            "以后同一商品的新版本会自动上传到该目录，沿用这个分享链接。\n"
                            "然后在 Mac 上运行：\n"
                            "uv run python scripts/set_baidu_share.py "
                            f"--share-key {exc.share_key}\n"
                            "录入后系统会自动继续；无需在飞书重新审批。"
                        )
                    except Exception:
                        pass
                return True
            if isinstance(exc, DailyPublishLimitReached):
                next_day = (
                    datetime.now(ZoneInfo(self.settings.timezone))
                    .replace(hour=0, minute=5, second=0, microsecond=0)
                    + timedelta(days=1)
                )
                self.db.defer_job(
                    int(job["id"]),
                    available_at=next_day.isoformat(),
                    reason=str(exc),
                )
                return True
            if job["kind"] == "revision_feedback":
                try:
                    self.feishu.send_text(
                        "修改意见分析失败，商品仍为草稿且不会上架。"
                        f"错误类型：{type(exc).__name__}。请稍后重新回复一条修改意见。"
                    )
                except Exception:
                    pass
            self.db.finish_job(
                int(job["id"]), JobStatus.FAILED, error=f"{type(exc).__name__}: {exc}"
            )
            return True
        self.db.finish_job(int(job["id"]), JobStatus.SUCCEEDED)
        return True

    def process_jobs(self, max_jobs: int = 10) -> int:
        processed = 0
        while processed < max_jobs and self.worker_once():
            processed += 1
        return processed

    def reconcile(self) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        bindings = self.db.delivery_bindings()
        for binding in bindings:
            if binding["status"] == "DELISTED":
                continue
            if binding["status"] != "READY":
                issues.append(
                    {"product_id": binding["product_id"], "issue": "binding_not_ready"}
                )
                continue
            if not binding["item_id"] or not binding["automation_rule_id"]:
                issues.append(
                    {"product_id": binding["product_id"], "issue": "missing_ids"}
                )
                continue
            if not self.settings.publish_dry_run and binding["netdisk_link"]:
                try:
                    response = httpx.head(
                        binding["netdisk_link"], follow_redirects=True, timeout=15
                    )
                    if response.status_code >= 400:
                        issues.append(
                            {
                                "product_id": binding["product_id"],
                                "issue": "netdisk_link_unreachable",
                                "status_code": response.status_code,
                            }
                        )
                except httpx.HTTPError:
                    issues.append(
                        {
                            "product_id": binding["product_id"],
                            "issue": "netdisk_link_check_failed",
                        }
                    )
            rules = self.ydisks.find_rules_for_item(str(binding["item_id"]))
            enabled = [rule for rule in rules if bool(rule.get("enabled", True))]
            if len(enabled) != 1:
                issues.append(
                    {
                        "product_id": binding["product_id"],
                        "issue": "delivery_rule_count",
                        "enabled_count": len(enabled),
                    }
                )
            if binding["card_id"]:
                details = self.ydisks.get_card_details(int(binding["card_id"]))
                stock = details.get(
                    "stock",
                    details.get("remaining", details.get("available", None)),
                )
                if stock is not None and int(stock) <= 0:
                    issues.append(
                        {
                            "product_id": binding["product_id"],
                            "issue": "card_stock_empty",
                        }
                    )
        for issue in issues:
            self.db.audit("reconcile.issue", "scheduler", issue["product_id"], issue)
            if issue["product_id"] and issue["issue"] not in {
                "delivery_link_missing",
                "delivery_link_invalid",
            }:
                self.db.enqueue_job(
                    "delivery_repair",
                    f"reconcile-repair:{issue['product_id']}:{iso_now()[:10]}",
                    product_id=int(issue["product_id"]),
                    payload={},
                )
        report = {"checked": len(bindings), "issues": issues, "checked_at": iso_now()}
        self.db.audit("reconcile.completed", "scheduler", None, report)
        return report

    def enqueue_daily_tasks(self) -> list[str]:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        day = now.date().isoformat()
        created: list[str] = []
        if now.hour >= 9:
            if self.db.enqueue_job("selection", f"selection:{day}"):
                created.append("selection")
        if now.hour >= 20:
            if self.db.enqueue_job("reconcile", f"reconcile:{day}"):
                created.append("reconcile")
        return created


def _human_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def build_demo_orchestrator(home: Path) -> Orchestrator:
    settings = Settings(
        home=home,
        database_path=home / "data" / "demo.sqlite3",
        assets_dir=home / "assets",
        owner_open_id="demo-owner",
        yd_is_base_url="http://127.0.0.1:59188",
        yd_is_cookie_id=None,
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
        admin_token=None,
        publish_dry_run=True,
        auto_confirm_delivery=False,
        daily_publish_limit=2,
        timezone="Asia/Shanghai",
        scheduler_enabled=False,
        scheduler_tick_seconds=30,
        raw={},
    )
    db = Database(settings.database_path)
    db.initialize()
    return Orchestrator(
        settings=settings,
        db=db,
        ydisks=DryRunYdisksAdapter(),
        netdisk=DryRunNetdiskAdapter(),
        feishu=DryRunFeishuClient("demo-owner"),
    )

