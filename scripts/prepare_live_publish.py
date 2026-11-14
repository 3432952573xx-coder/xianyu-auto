#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if (ROOT / "src").exists():
    sys.path.insert(0, str(ROOT / "src"))

from xianyu_auto.config import Settings
from xianyu_auto.db import Database, iso_now
from xianyu_auto.models import ProductStatus


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def prepare_live_publish(
    db: Database,
    product_id: int,
    *,
    timezone_name: str,
    owner_open_id: str | None,
    apply: bool,
) -> dict[str, Any]:
    with db.transaction() as conn:
        product = conn.execute(
            "SELECT * FROM products WHERE id=?", (product_id,)
        ).fetchone()
        _require(product is not None, f"product {product_id} not found")
        assert product is not None
        _require(
            product["status"] == ProductStatus.DELIVERY_READY.value,
            f"expected DELIVERY_READY, got {product['status']}",
        )

        approval = conn.execute(
            """
            SELECT a.*
            FROM approvals a
            JOIN products p
              ON p.id=a.product_id
             AND p.version=a.product_version
             AND p.content_hash=a.content_hash
            WHERE a.product_id=? AND a.status='APPROVED'
            ORDER BY a.decided_at DESC
            LIMIT 1
            """,
            (product_id,),
        ).fetchone()
        _require(approval is not None, "no valid owner approval for current content")
        assert approval is not None
        if owner_open_id:
            _require(
                approval["owner_open_id"] == owner_open_id
                and approval["decided_by"] == owner_open_id,
                "valid approval was not decided by the configured owner",
            )

        attempts = conn.execute(
            """
            SELECT * FROM publish_attempts
            WHERE product_id=? AND content_hash=? AND state='SUCCEEDED'
            ORDER BY id
            """,
            (product_id, product["content_hash"]),
        ).fetchall()
        _require(len(attempts) == 1, "expected exactly one successful publish attempt")
        attempt = attempts[0]
        item_id = str(attempt["item_id"] or "")
        _require(item_id.startswith("DRYRUN-"), "successful attempt is not a dry run")

        bindings = conn.execute(
            "SELECT * FROM delivery_bindings WHERE product_id=?", (product_id,)
        ).fetchall()
        _require(len(bindings) == 1, "expected exactly one delivery binding")
        binding = bindings[0]
        _require(
            str(binding["item_id"] or "").startswith("DRYRUN-"),
            "delivery binding is not a dry run",
        )

        dedupe_key = (
            f"publish:{product_id}:{product['content_hash']}"
        )
        jobs = conn.execute(
            """
            SELECT * FROM jobs
            WHERE kind='publish' AND product_id=? AND dedupe_key=?
            ORDER BY id
            """,
            (product_id, dedupe_key),
        ).fetchall()
        _require(len(jobs) == 1, "expected one publish job for the approved content")
        job = jobs[0]

        day = datetime.fromisoformat(attempt["started_at"]).astimezone(
            ZoneInfo(timezone_name)
        ).date().isoformat()
        counter = conn.execute(
            "SELECT published_count FROM daily_counters WHERE day=?", (day,)
        ).fetchone()
        count = int(counter["published_count"]) if counter else 0
        if int(attempt["day_counted"]) == 1:
            _require(count > 0, f"daily counter for {day} is already zero")

        report = {
            "product_id": product_id,
            "title": product["title"],
            "current_status": product["status"],
            "target_status": ProductStatus.APPROVED.value,
            "approval_id": approval["id"],
            "content_hash": product["content_hash"],
            "dry_run_attempt_id": attempt["id"],
            "dry_run_item_id": item_id,
            "dry_run_binding_id": binding["id"],
            "publish_job_id": job["id"],
            "publish_job_status": job["status"],
            "daily_counter_day": day,
            "daily_counter_before": count,
            "applied": apply,
        }
        if not apply:
            return report

        now = iso_now()
        archived_key = f"dry-run-archive:{attempt['id']}:{attempt['idempotency_key']}"
        db.audit(
            "dryrun.publish_archived",
            "operator",
            product_id,
            {
                **report,
                "attempt": dict(attempt),
                "delivery_binding": dict(binding),
            },
            conn=conn,
        )
        conn.execute(
            """
            UPDATE publish_attempts
            SET state='DRY_RUN_ARCHIVED', idempotency_key=?, day_counted=0
            WHERE id=?
            """,
            (archived_key, attempt["id"]),
        )
        conn.execute("DELETE FROM delivery_bindings WHERE id=?", (binding["id"],))
        conn.execute(
            """
            UPDATE products
            SET status=?, updated_at=?
            WHERE id=?
            """,
            (ProductStatus.APPROVED.value, now, product_id),
        )
        conn.execute(
            """
            UPDATE jobs
            SET status='PENDING', attempts=0, last_error=NULL,
                available_at=?, updated_at=?
            WHERE id=?
            """,
            (now, now, job["id"]),
        )
        if int(attempt["day_counted"]) == 1:
            conn.execute(
                """
                UPDATE daily_counters
                SET published_count=published_count-1
                WHERE day=? AND published_count>0
                """,
                (day,),
            )
            conn.execute(
                "DELETE FROM daily_counters WHERE day=? AND published_count=0",
                (day,),
            )
        db.audit(
            "live_publish.prepared",
            "operator",
            product_id,
            {
                "approval_id": approval["id"],
                "content_hash": product["content_hash"],
                "publish_job_id": job["id"],
                "archived_attempt_id": attempt["id"],
            },
            conn=conn,
        )
        return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Archive the synthetic dry-run publish and requeue the approved item."
    )
    parser.add_argument("--product-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    settings = Settings.load()
    db = Database(settings.database_path)
    db.initialize()
    try:
        report = prepare_live_publish(
            db,
            args.product_id,
            timezone_name=settings.timezone,
            owner_open_id=settings.owner_open_id,
            apply=args.apply,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
