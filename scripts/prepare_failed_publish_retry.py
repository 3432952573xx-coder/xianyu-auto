#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if (ROOT / "src").exists():
    sys.path.insert(0, str(ROOT / "src"))

from xianyu_auto.adapters.ydisks import HttpYdisksAdapter
from xianyu_auto.config import Settings
from xianyu_auto.db import Database, iso_now
from xianyu_auto.models import ApprovalStatus, ProductStatus


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _backup_database(db: Database, target_dir: Path) -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    target_dir = target_dir / f"pre-retry-{stamp}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "xianyu_auto.db"
    source = sqlite3.connect(db.path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def prepare_failed_publish_retry(
    db: Database,
    *,
    product_id: int,
    approval_id: str,
    attempt_id: int,
    job_id: int,
    owner_open_id: str | None,
    backup_dir: Path,
    apply: bool,
) -> dict[str, Any]:
    product = db.product(product_id)
    approval = db.approval(approval_id)
    attempt = db.one("SELECT * FROM publish_attempts WHERE id=?", (attempt_id,))
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))

    _require(
        product["status"] == ProductStatus.PUBLISH_FAILED.value,
        f"expected PUBLISH_FAILED, got {product['status']}",
    )
    _require(
        approval["status"] == ApprovalStatus.APPROVED.value,
        f"approval is {approval['status']}, not APPROVED",
    )
    _require(
        approval["product_id"] == product_id
        and approval["product_version"] == product["version"]
        and approval["content_hash"] == product["content_hash"],
        "approval does not match the current product version and content hash",
    )
    if owner_open_id:
        _require(
            approval["owner_open_id"] == owner_open_id
            and approval["decided_by"] == owner_open_id,
            "approval was not decided by the configured owner",
        )
    _require(attempt is not None, f"publish attempt {attempt_id} not found")
    assert attempt is not None
    _require(
        attempt["product_id"] == product_id
        and attempt["approval_id"] == approval_id
        and attempt["content_hash"] == product["content_hash"],
        "publish attempt does not match the product and approval",
    )
    _require(attempt["state"] == "FAILED", "publish attempt is not FAILED")
    _require(not attempt["item_id"], "failed publish attempt unexpectedly has an item_id")
    _require(
        int(attempt["day_counted"]) == 0,
        "failed publish attempt was counted as a successful daily publish",
    )
    successful = db.one(
        """
        SELECT id FROM publish_attempts
        WHERE content_hash=? AND state='SUCCEEDED'
        """,
        (product["content_hash"],),
    )
    _require(successful is None, "a successful publish already exists for this content")
    _require(job is not None, f"publish job {job_id} not found")
    assert job is not None
    expected_dedupe = f"publish:{product_id}:{product['content_hash']}"
    _require(
        job["kind"] == "publish"
        and job["product_id"] == product_id
        and job["dedupe_key"] == expected_dedupe,
        "job does not match the failed product publish",
    )
    _require(job["status"] == "FAILED", "publish job is not FAILED")

    report = {
        "product_id": product_id,
        "title": product["title"],
        "current_status": product["status"],
        "target_status": ProductStatus.APPROVED.value,
        "approval_id": approval_id,
        "content_hash": product["content_hash"],
        "failed_attempt_id": attempt_id,
        "publish_job_id": job_id,
        "remote_duplicate_count": 0,
        "applied": apply,
    }
    if not apply:
        return report

    backup = _backup_database(db, backup_dir)
    report["database_backup"] = str(backup)
    now = iso_now()
    archived_key = f"failed-archive:{attempt_id}:{attempt['idempotency_key']}"
    with db.transaction() as conn:
        db.audit(
            "publish.failed_attempt_archived",
            "operator",
            product_id,
            {
                "attempt_id": attempt_id,
                "approval_id": approval_id,
                "content_hash": product["content_hash"],
                "previous_error": attempt["error"],
            },
            conn=conn,
        )
        conn.execute(
            """
            UPDATE publish_attempts
            SET state='FAILED_ARCHIVED', idempotency_key=?
            WHERE id=?
            """,
            (archived_key, attempt_id),
        )
        conn.execute(
            "UPDATE products SET status=?, updated_at=? WHERE id=?",
            (ProductStatus.APPROVED.value, now, product_id),
        )
        db.audit(
            "product.status_changed",
            "operator",
            product_id,
            {
                "from": ProductStatus.PUBLISH_FAILED.value,
                "to": ProductStatus.APPROVED.value,
                "reason": "retry_failed_publish_after_remote_count_zero",
                "approval_id": approval_id,
            },
            conn=conn,
        )
        conn.execute(
            """
            UPDATE jobs
            SET status='PENDING', attempts=0, last_error=NULL,
                available_at=?, updated_at=?
            WHERE id=?
            """,
            (now, now, job_id),
        )
        conn.execute(
            """
            UPDATE delivery_bindings
            SET manual_confirm=1, updated_at=?
            WHERE product_id=?
            """,
            (now, product_id),
        )
        db.audit(
            "publish.retry_queued",
            "operator",
            product_id,
            {
                "approval_id": approval_id,
                "archived_attempt_id": attempt_id,
                "publish_job_id": job_id,
                "content_hash": product["content_hash"],
            },
            conn=conn,
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Archive one failed live publish and requeue its approved version."
    )
    parser.add_argument("--product-id", type=int, required=True)
    parser.add_argument("--approval-id", required=True)
    parser.add_argument("--attempt-id", type=int, required=True)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    settings = Settings.load()
    db = Database(settings.database_path)
    db.initialize()
    ydisks = None
    try:
        product = db.product(args.product_id)
        if not settings.yd_is_cookie_id:
            raise RuntimeError("yd_is_cookie_id is not configured")
        ydisks = HttpYdisksAdapter(
            settings.yd_is_base_url,
            cookie_id=settings.yd_is_cookie_id,
            session_cookie=settings.yd_is_session_cookie,
            username=settings.yd_is_username,
            password=settings.yd_is_password,
        )
        matches = ydisks.find_items_by_title(product["title"])
        if matches:
            raise RuntimeError(
                f"remote already has {len(matches)} item(s) with this title; recovery stopped"
            )
        report = prepare_failed_publish_retry(
            db,
            product_id=args.product_id,
            approval_id=args.approval_id,
            attempt_id=args.attempt_id,
            job_id=args.job_id,
            owner_open_id=settings.owner_open_id,
            backup_dir=settings.home / "backups",
            apply=args.apply,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        if ydisks is not None:
            ydisks.close()
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
