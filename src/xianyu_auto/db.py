from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import (
    ApprovalStatus,
    JobStatus,
    ProductStatus,
    StateTransitionError,
    require_transition,
)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  category TEXT NOT NULL,
  price_cents INTEGER NOT NULL,
  normal_price_cents INTEGER NOT NULL,
  pages INTEGER NOT NULL,
  status TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  risk_flags_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  preview_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(slug, version)
);

CREATE TABLE IF NOT EXISTS assets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id),
  product_version INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  owner_open_id TEXT NOT NULL,
  card_message_id TEXT,
  preview_token TEXT NOT NULL UNIQUE,
  decision_token TEXT NOT NULL UNIQUE,
  reject_reason TEXT,
  decided_by TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  reminder_2h_sent_at TEXT,
  reminder_24h_sent_at TEXT,
  expires_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS approvals_one_open_version
ON approvals(product_id, product_version)
WHERE status = 'PENDING';

CREATE TABLE IF NOT EXISTS revision_proposals (
  id TEXT PRIMARY KEY,
  approval_id TEXT NOT NULL REFERENCES approvals(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  product_version INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  feedback TEXT NOT NULL,
  proposal_json TEXT NOT NULL,
  model TEXT NOT NULL,
  status TEXT NOT NULL,
  card_message_id TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT
);

CREATE TABLE IF NOT EXISTS publish_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER NOT NULL REFERENCES products(id),
  approval_id TEXT NOT NULL REFERENCES approvals(id),
  content_hash TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL,
  request_json TEXT NOT NULL,
  response_json TEXT NOT NULL DEFAULT '{}',
  item_id TEXT,
  error TEXT,
  day_counted INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS publish_attempts_one_success
ON publish_attempts(content_hash)
WHERE state = 'SUCCEEDED';

CREATE TABLE IF NOT EXISTS delivery_bindings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER NOT NULL UNIQUE REFERENCES products(id),
  item_id TEXT UNIQUE,
  netdisk_link TEXT,
  extract_code TEXT,
  card_id INTEGER,
  automation_rule_id INTEGER,
  status TEXT NOT NULL,
  manual_confirm INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  dedupe_key TEXT NOT NULL UNIQUE,
  product_id INTEGER,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  available_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_counters (
  day TEXT PRIMARY KEY,
  published_count INTEGER NOT NULL DEFAULT 0
);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def initialize(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in self._conn.execute(
                    "PRAGMA table_info(publish_attempts)"
                ).fetchall()
            }
            if "day_counted" not in columns:
                self._conn.execute(
                    """
                    ALTER TABLE publish_attempts
                    ADD COLUMN day_counted INTEGER NOT NULL DEFAULT 0
                    """
                )
            self._conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def audit(
        self,
        event_type: str,
        actor: str,
        product_id: int | None = None,
        details: dict[str, Any] | None = None,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        target = conn or self._conn
        target.execute(
            """
            INSERT INTO audit_events(product_id, event_type, actor, details_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                product_id,
                event_type,
                actor,
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                iso_now(),
            ),
        )
        if conn is None:
            self._conn.commit()

    def create_product(
        self,
        *,
        slug: str,
        title: str,
        description: str,
        category: str,
        price_cents: int,
        normal_price_cents: int,
        pages: int,
        content_hash: str,
        risk_flags: list[str],
        metadata: dict[str, Any],
        preview_path: str | None,
        assets: list[dict[str, str]],
    ) -> int:
        now = iso_now()
        with self.transaction() as conn:
            latest = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM products WHERE slug=?",
                (slug,),
            ).fetchone()
            version = int(latest["version"]) + 1 if latest else 1
            cursor = conn.execute(
                """
                INSERT INTO products(
                  slug, version, title, description, category, price_cents,
                  normal_price_cents, pages, status, content_hash, risk_flags_json,
                  metadata_json, preview_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    slug,
                    version,
                    title,
                    description,
                    category,
                    price_cents,
                    normal_price_cents,
                    pages,
                    ProductStatus.DRAFT.value,
                    content_hash,
                    json.dumps(risk_flags, ensure_ascii=False),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    preview_path,
                    now,
                    now,
                ),
            )
            product_id = int(cursor.lastrowid)
            for asset in assets:
                conn.execute(
                    """
                    INSERT INTO assets(product_id, kind, path, sha256, metadata_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        product_id,
                        asset["kind"],
                        asset["path"],
                        asset["sha256"],
                        json.dumps(asset.get("metadata", {}), ensure_ascii=False),
                        now,
                    ),
                )
            self.audit(
                "product.created",
                "system",
                product_id,
                {"slug": slug, "version": version, "content_hash": content_hash},
                conn=conn,
            )
        return product_id

    def product(self, product_id: int) -> sqlite3.Row:
        row = self.one("SELECT * FROM products WHERE id=?", (product_id,))
        if row is None:
            raise KeyError(f"product {product_id} not found")
        return row

    def product_by_slug_and_hash(
        self, slug: str, content_hash: str
    ) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM products WHERE slug=? AND content_hash=? ORDER BY id DESC LIMIT 1",
            (slug, content_hash),
        )

    def set_product_status(
        self,
        product_id: int,
        target: ProductStatus,
        *,
        actor: str,
        details: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        def apply(c: sqlite3.Connection) -> None:
            row = c.execute(
                "SELECT status FROM products WHERE id=?", (product_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"product {product_id} not found")
            current = ProductStatus(row["status"])
            require_transition(current, target)
            c.execute(
                "UPDATE products SET status=?, updated_at=? WHERE id=?",
                (target.value, iso_now(), product_id),
            )
            self.audit(
                "product.status_changed",
                actor,
                product_id,
                {"from": current.value, "to": target.value, **(details or {})},
                conn=c,
            )

        if conn is not None:
            apply(conn)
        else:
            with self.transaction() as local:
                apply(local)

    def submit_for_approval(
        self, product_id: int, owner_open_id: str, expires_at: str
    ) -> dict[str, Any]:
        approval_id = str(uuid.uuid4())
        preview_token = uuid.uuid4().hex
        decision_token = uuid.uuid4().hex
        now = iso_now()
        with self.transaction() as conn:
            product = conn.execute(
                "SELECT * FROM products WHERE id=?", (product_id,)
            ).fetchone()
            if product is None:
                raise KeyError(f"product {product_id} not found")
            if ProductStatus(product["status"]) != ProductStatus.DRAFT:
                raise StateTransitionError("only DRAFT products can be submitted")
            existing = conn.execute(
                """
                SELECT id FROM approvals
                WHERE product_id=? AND product_version=? AND status='PENDING'
                """,
                (product_id, product["version"]),
            ).fetchone()
            if existing:
                raise StateTransitionError("approval already pending")
            conn.execute(
                """
                INSERT INTO approvals(
                  id, product_id, product_version, content_hash, status, owner_open_id,
                  preview_token, decision_token, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    product_id,
                    product["version"],
                    product["content_hash"],
                    ApprovalStatus.PENDING.value,
                    owner_open_id,
                    preview_token,
                    decision_token,
                    now,
                    expires_at,
                ),
            )
            self.set_product_status(
                product_id,
                ProductStatus.PENDING_APPROVAL,
                actor="system",
                details={"approval_id": approval_id},
                conn=conn,
            )
        return {
            "id": approval_id,
            "preview_token": preview_token,
            "decision_token": decision_token,
            "expires_at": expires_at,
        }

    def approval(self, approval_id: str) -> sqlite3.Row:
        row = self.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if row is None:
            raise KeyError(f"approval {approval_id} not found")
        return row

    def request_changes(
        self, approval_id: str, *, actor_open_id: str
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            approval = conn.execute(
                "SELECT * FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
            if approval is None:
                raise KeyError(f"approval {approval_id} not found")
            if actor_open_id != approval["owner_open_id"]:
                self.audit(
                    "approval.unauthorized",
                    actor_open_id,
                    approval["product_id"],
                    {"approval_id": approval_id, "action": "request_changes"},
                    conn=conn,
                )
                raise PermissionError("approval is restricted to configured owner")
            if approval["status"] != ApprovalStatus.PENDING.value:
                if approval["status"] in {
                    ApprovalStatus.EXPIRED.value,
                    ApprovalStatus.INVALIDATED.value,
                }:
                    raise StateTransitionError(
                        f"approval is {approval['status'].lower()}"
                    )
                return {
                    "changed": False,
                    "status": approval["status"],
                    "reason": "already_decided",
                }
            product = conn.execute(
                "SELECT * FROM products WHERE id=?", (approval["product_id"],)
            ).fetchone()
            if product is None:
                raise KeyError("approval product not found")
            if (
                product["content_hash"] != approval["content_hash"]
                or product["version"] != approval["product_version"]
            ):
                conn.execute(
                    "UPDATE approvals SET status=? WHERE id=?",
                    (ApprovalStatus.INVALIDATED.value, approval_id),
                )
                raise StateTransitionError("approval content hash is stale")
            now = iso_now()
            conn.execute(
                """
                UPDATE approvals
                SET status=?, decided_by=?, reject_reason=?, decided_at=?
                WHERE id=? AND status='PENDING'
                """,
                (
                    ApprovalStatus.CHANGES_REQUESTED.value,
                    actor_open_id,
                    "changes_requested",
                    now,
                    approval_id,
                ),
            )
            self.set_product_status(
                approval["product_id"],
                ProductStatus.DRAFT,
                actor=actor_open_id,
                details={"approval_id": approval_id, "reason": "changes_requested"},
                conn=conn,
            )
            self.audit(
                "approval.changes_requested",
                actor_open_id,
                approval["product_id"],
                {
                    "approval_id": approval_id,
                    "content_hash": approval["content_hash"],
                },
                conn=conn,
            )
        return {
            "changed": True,
            "status": ApprovalStatus.CHANGES_REQUESTED.value,
        }

    def latest_revision_request(self, owner_open_id: str) -> sqlite3.Row | None:
        return self.one(
            """
            SELECT a.*, p.title
            FROM approvals a
            JOIN products p ON p.id=a.product_id
            WHERE a.owner_open_id=?
              AND a.status=?
              AND p.status=?
            ORDER BY a.decided_at DESC
            LIMIT 1
            """,
            (
                owner_open_id,
                ApprovalStatus.CHANGES_REQUESTED.value,
                ProductStatus.DRAFT.value,
            ),
        )

    def record_revision_feedback(
        self,
        approval_id: str,
        *,
        actor_open_id: str,
        feedback: str,
    ) -> sqlite3.Row:
        with self.transaction() as conn:
            approval = conn.execute(
                """
                SELECT a.*, p.title
                FROM approvals a
                JOIN products p ON p.id=a.product_id
                WHERE a.id=?
                """,
                (approval_id,),
            ).fetchone()
            if approval is None:
                raise KeyError(f"approval {approval_id} not found")
            if actor_open_id != approval["owner_open_id"]:
                raise PermissionError("approval is restricted to configured owner")
            if approval["status"] != ApprovalStatus.CHANGES_REQUESTED.value:
                raise StateTransitionError("approval is not awaiting revision feedback")
            self.audit(
                "approval.revision_feedback",
                actor_open_id,
                approval["product_id"],
                {
                    "approval_id": approval_id,
                    "feedback": feedback,
                    "content_hash": approval["content_hash"],
                },
                conn=conn,
            )
            conn.execute(
                "UPDATE approvals SET reject_reason=? WHERE id=?",
                (feedback[:2000], approval_id),
            )
        return approval

    def create_revision_proposal(
        self,
        *,
        approval_id: str,
        product_id: int,
        product_version: int,
        content_hash: str,
        feedback: str,
        proposal: dict[str, Any],
        model: str,
    ) -> str:
        proposal_id = str(uuid.uuid4())
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO revision_proposals(
                  id, approval_id, product_id, product_version, content_hash,
                  feedback, proposal_json, model, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PROPOSED', ?)
                """,
                (
                    proposal_id,
                    approval_id,
                    product_id,
                    product_version,
                    content_hash,
                    feedback,
                    json.dumps(proposal, ensure_ascii=False, sort_keys=True),
                    model,
                    iso_now(),
                ),
            )
            self.audit(
                "revision.proposal_generated",
                "model",
                product_id,
                {
                    "approval_id": approval_id,
                    "proposal_id": proposal_id,
                    "content_hash": content_hash,
                    "model": model,
                    "proposal": proposal,
                },
                conn=conn,
            )
        return proposal_id

    def revision_proposal(self, proposal_id: str) -> sqlite3.Row:
        row = self.one("SELECT * FROM revision_proposals WHERE id=?", (proposal_id,))
        if row is None:
            raise KeyError(f"revision proposal {proposal_id} not found")
        return row

    def set_revision_proposal_card(
        self, proposal_id: str, message_id: str
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE revision_proposals SET card_message_id=? WHERE id=?",
                (message_id, proposal_id),
            )

    def mark_revision_proposal_applied(
        self,
        proposal_id: str,
        *,
        actor_open_id: str,
        new_product_version: int,
    ) -> None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM revision_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"revision proposal {proposal_id} not found")
            conn.execute(
                """
                UPDATE revision_proposals
                SET status='APPLIED', decided_at=?
                WHERE id=? AND status='PROPOSED'
                """,
                (iso_now(), proposal_id),
            )
            self.audit(
                "revision.proposal_applied",
                actor_open_id,
                int(row["product_id"]),
                {
                    "approval_id": row["approval_id"],
                    "proposal_id": proposal_id,
                    "new_product_version": new_product_version,
                },
                conn=conn,
            )

    def mark_revision_proposal_stale(
        self, proposal_id: str, *, actor: str, reason: str
    ) -> None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM revision_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"revision proposal {proposal_id} not found")
            conn.execute(
                """
                UPDATE revision_proposals
                SET status='STALE', decided_at=?
                WHERE id=? AND status='PROPOSED'
                """,
                (iso_now(), proposal_id),
            )
            self.audit(
                "revision.proposal_stale",
                actor,
                int(row["product_id"]),
                {"proposal_id": proposal_id, "reason": reason},
                conn=conn,
            )

    def decide_approval(
        self,
        approval_id: str,
        *,
        actor_open_id: str,
        approve: bool,
        reject_reason: str | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            approval = conn.execute(
                "SELECT * FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
            if approval is None:
                raise KeyError(f"approval {approval_id} not found")
            if actor_open_id != approval["owner_open_id"]:
                self.audit(
                    "approval.unauthorized",
                    actor_open_id,
                    approval["product_id"],
                    {"approval_id": approval_id},
                    conn=conn,
                )
                raise PermissionError("approval is restricted to configured owner")
            if approval["status"] != ApprovalStatus.PENDING.value:
                if approval["status"] in {
                    ApprovalStatus.EXPIRED.value,
                    ApprovalStatus.INVALIDATED.value,
                }:
                    raise StateTransitionError(
                        f"approval is {approval['status'].lower()}"
                    )
                return {
                    "changed": False,
                    "status": approval["status"],
                    "reason": "already_decided",
                }
            product = conn.execute(
                "SELECT * FROM products WHERE id=?", (approval["product_id"],)
            ).fetchone()
            if product is None:
                raise KeyError("approval product not found")
            if (
                product["content_hash"] != approval["content_hash"]
                or product["version"] != approval["product_version"]
            ):
                conn.execute(
                    "UPDATE approvals SET status=? WHERE id=?",
                    (ApprovalStatus.INVALIDATED.value, approval_id),
                )
                raise StateTransitionError("approval content hash is stale")
            target_status = (
                ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
            )
            conn.execute(
                """
                UPDATE approvals
                SET status=?, decided_by=?, reject_reason=?, decided_at=?
                WHERE id=? AND status='PENDING'
                """,
                (
                    target_status.value,
                    actor_open_id,
                    reject_reason,
                    iso_now(),
                    approval_id,
                ),
            )
            self.audit(
                "approval.decided",
                actor_open_id,
                approval["product_id"],
                {
                    "approval_id": approval_id,
                    "decision": target_status.value,
                    "reject_reason": reject_reason,
                    "content_hash": approval["content_hash"],
                },
                conn=conn,
            )
            product_target = (
                ProductStatus.APPROVED if approve else ProductStatus.REJECTED
            )
            self.set_product_status(
                approval["product_id"],
                product_target,
                actor=actor_open_id,
                details={"approval_id": approval_id, "reason": reject_reason},
                conn=conn,
            )
            if approve:
                self.enqueue_job(
                    "publish",
                    f"publish:{approval['product_id']}:{approval['content_hash']}",
                    product_id=approval["product_id"],
                    payload={"approval_id": approval_id},
                    conn=conn,
                )
        return {"changed": True, "status": target_status.value}

    def expire_stale_approvals(self) -> list[str]:
        expired: list[str] = []
        with self.transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM approvals
                WHERE status='PENDING' AND expires_at <= ?
                """,
                (iso_now(),),
            ).fetchall()
            for approval in rows:
                conn.execute(
                    """
                    UPDATE approvals SET status=?, decided_at=?, reject_reason=?
                    WHERE id=? AND status='PENDING'
                    """,
                    (
                        ApprovalStatus.EXPIRED.value,
                        iso_now(),
                        "approval_expired_after_7_days",
                        approval["id"],
                    ),
                )
                self.set_product_status(
                    approval["product_id"],
                    ProductStatus.REJECTED,
                    actor="scheduler",
                    details={
                        "approval_id": approval["id"],
                        "reason": "approval_expired_after_7_days",
                    },
                    conn=conn,
                )
                expired.append(approval["id"])
        return expired

    def invalidate_pending_approvals(
        self, product_id: int, *, actor: str, reason: str
    ) -> None:
        with self.transaction() as conn:
            product = conn.execute(
                "SELECT * FROM products WHERE id=?", (product_id,)
            ).fetchone()
            if product is None:
                raise KeyError(f"product {product_id} not found")
            conn.execute(
                """
                UPDATE approvals SET status=?, decided_at=?, reject_reason=?
                WHERE product_id=? AND status='PENDING'
                """,
                (
                    ApprovalStatus.INVALIDATED.value,
                    iso_now(),
                    reason,
                    product_id,
                ),
            )
            if product["status"] in {
                ProductStatus.PENDING_APPROVAL.value,
                ProductStatus.APPROVED.value,
            }:
                self.set_product_status(
                    product_id,
                    ProductStatus.DRAFT,
                    actor=actor,
                    details={"reason": reason},
                    conn=conn,
                )

    def update_product_content(
        self,
        product_id: int,
        *,
        title: str,
        description: str,
        category: str,
        price_cents: int,
        normal_price_cents: int,
        pages: int,
        content_hash: str,
        assets: list[dict[str, Any]],
        actor: str = "system",
    ) -> int:
        with self.transaction() as conn:
            product = conn.execute(
                "SELECT * FROM products WHERE id=?", (product_id,)
            ).fetchone()
            if product is None:
                raise KeyError(f"product {product_id} not found")
            current = ProductStatus(product["status"])
            if current in {
                ProductStatus.PUBLISHING,
                ProductStatus.LIVE,
                ProductStatus.DELIVERY_READY,
                ProductStatus.DELIVERY_FAILED,
            }:
                raise StateTransitionError(
                    "live or publishing products cannot be edited in place"
                )
            now = iso_now()
            conn.execute(
                """
                UPDATE approvals
                SET status=?, decided_at=?, reject_reason=?
                WHERE product_id=?
                  AND status IN ('PENDING', 'APPROVED', 'CHANGES_REQUESTED')
                """,
                (
                    ApprovalStatus.INVALIDATED.value,
                    now,
                    "content_changed",
                    product_id,
                ),
            )
            if current in {
                ProductStatus.PENDING_APPROVAL,
                ProductStatus.APPROVED,
                ProductStatus.REJECTED,
            }:
                self.set_product_status(
                    product_id,
                    ProductStatus.DRAFT,
                    actor=actor,
                    details={"reason": "content_changed"},
                    conn=conn,
                )
            next_version = int(product["version"]) + 1
            conn.execute(
                """
                UPDATE products
                SET version=?, title=?, description=?, category=?, price_cents=?,
                    normal_price_cents=?, pages=?, content_hash=?, status=?, updated_at=?
                WHERE id=?
                """,
                (
                    next_version,
                    title,
                    description,
                    category,
                    price_cents,
                    normal_price_cents,
                    pages,
                    content_hash,
                    ProductStatus.DRAFT.value,
                    now,
                    product_id,
                ),
            )
            conn.execute("DELETE FROM assets WHERE product_id=?", (product_id,))
            for asset in assets:
                conn.execute(
                    """
                    INSERT INTO assets(product_id, kind, path, sha256, metadata_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        product_id,
                        asset["kind"],
                        asset["path"],
                        asset["sha256"],
                        json.dumps(asset.get("metadata", {}), ensure_ascii=False),
                        now,
                    ),
                )
            self.audit(
                "product.content_changed",
                actor,
                product_id,
                {
                    "from_version": product["version"],
                    "to_version": next_version,
                    "old_hash": product["content_hash"],
                    "new_hash": content_hash,
                },
                conn=conn,
            )
        return next_version

    def valid_approval_for_product(self, product_id: int) -> sqlite3.Row | None:
        return self.one(
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
        )

    def enqueue_job(
        self,
        kind: str,
        dedupe_key: str,
        *,
        product_id: int | None = None,
        payload: dict[str, Any] | None = None,
        available_at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        now = iso_now()
        values = (
            kind,
            dedupe_key,
            product_id,
            JobStatus.PENDING.value,
            json.dumps(payload or {}, ensure_ascii=False),
            available_at or now,
            now,
            now,
        )
        sql = """
          INSERT OR IGNORE INTO jobs(
            kind, dedupe_key, product_id, status, payload_json,
            available_at, created_at, updated_at
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        if conn is not None:
            cursor = conn.execute(sql, values)
            return cursor.rowcount == 1
        with self.transaction() as local:
            cursor = local.execute(sql, values)
            return cursor.rowcount == 1

    def claim_next_job(self, kind: str | None = None) -> sqlite3.Row | None:
        with self.transaction() as conn:
            params: tuple[Any, ...] = (iso_now(),)
            kind_clause = ""
            if kind:
                kind_clause = " AND kind=?"
                params += (kind,)
            row = conn.execute(
                f"""
                SELECT * FROM jobs
                WHERE status='PENDING' AND available_at <= ?{kind_clause}
                ORDER BY id
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE jobs
                SET status='RUNNING', attempts=attempts+1, updated_at=?
                WHERE id=?
                """,
                (iso_now(), row["id"]),
            )
            claimed = dict(row)
            claimed["attempts"] = int(row["attempts"]) + 1
            return claimed

    def recover_running_jobs(self) -> int:
        """Return interrupted jobs to the queue after a confirmed service restart."""
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status=?",
                (JobStatus.RUNNING.value,),
            ).fetchall()
            if not rows:
                return 0
            conn.execute(
                """
                UPDATE jobs
                SET status=?, available_at=?, updated_at=?,
                    last_error='recovered_after_service_restart'
                WHERE status=?
                """,
                (
                    JobStatus.PENDING.value,
                    iso_now(),
                    iso_now(),
                    JobStatus.RUNNING.value,
                ),
            )
            self.audit(
                "jobs.recovered_after_restart",
                "system",
                details={"job_ids": [int(row["id"]) for row in rows]},
                conn=conn,
            )
            return len(rows)

    def defer_job(
        self,
        job_id: int,
        *,
        available_at: str,
        reason: str,
        payload_patch: dict[str, Any] | None = None,
    ) -> None:
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT payload_json FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            payload = json.loads(existing["payload_json"]) if existing else {}
            payload.update(payload_patch or {})
            conn.execute(
                """
                UPDATE jobs
                SET status=?, available_at=?, last_error=?, payload_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    JobStatus.PENDING.value,
                    available_at,
                    reason,
                    json.dumps(payload, ensure_ascii=False),
                    iso_now(),
                    job_id,
                ),
            )

    def finish_job(
        self,
        job_id: int,
        status: JobStatus,
        *,
        error: str | None = None,
        payload_patch: dict[str, Any] | None = None,
    ) -> None:
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT payload_json FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            payload = json.loads(existing["payload_json"]) if existing else {}
            payload.update(payload_patch or {})
            conn.execute(
                """
                UPDATE jobs SET status=?, last_error=?, payload_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    status.value,
                    error,
                    json.dumps(payload, ensure_ascii=False),
                    iso_now(),
                    job_id,
                ),
            )

    def daily_publish_count(self, day: str) -> int:
        row = self.one(
            "SELECT published_count FROM daily_counters WHERE day=?", (day,)
        )
        return int(row["published_count"]) if row else 0

    def increment_daily_publish(self, day: str, *, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            INSERT INTO daily_counters(day, published_count) VALUES (?, 1)
            ON CONFLICT(day) DO UPDATE SET published_count=published_count+1
            """,
            (day,),
        )

    def count_publish_once(
        self, attempt_id: int, day: str, *, conn: sqlite3.Connection
    ) -> bool:
        row = conn.execute(
            "SELECT day_counted FROM publish_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"publish attempt {attempt_id} not found")
        if int(row["day_counted"]) == 1:
            return False
        cursor = conn.execute(
            """
            UPDATE publish_attempts SET day_counted=1
            WHERE id=? AND day_counted=0
            """,
            (attempt_id,),
        )
        if cursor.rowcount != 1:
            return False
        self.increment_daily_publish(day, conn=conn)
        return True

    def products_for_status(self, statuses: set[ProductStatus]) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in statuses)
        return self.all(
            f"SELECT * FROM products WHERE status IN ({placeholders}) ORDER BY id",
            tuple(status.value for status in statuses),
        )

    def delivery_bindings(self) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM delivery_bindings ORDER BY id")

    def save_delivery_binding(
        self,
        product_id: int,
        *,
        item_id: str | None = None,
        netdisk_link: str | None = None,
        extract_code: str | None = None,
        card_id: int | None = None,
        automation_rule_id: int | None = None,
        status: str,
    ) -> None:
        now = iso_now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO delivery_bindings(
                  product_id, item_id, netdisk_link, extract_code, card_id,
                  automation_rule_id, status, manual_confirm, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(product_id) DO UPDATE SET
                  item_id=COALESCE(excluded.item_id, delivery_bindings.item_id),
                  netdisk_link=COALESCE(excluded.netdisk_link, delivery_bindings.netdisk_link),
                  extract_code=COALESCE(excluded.extract_code, delivery_bindings.extract_code),
                  card_id=COALESCE(excluded.card_id, delivery_bindings.card_id),
                  automation_rule_id=COALESCE(excluded.automation_rule_id, delivery_bindings.automation_rule_id),
                  status=excluded.status,
                  updated_at=excluded.updated_at
                """,
                (
                    product_id,
                    item_id,
                    netdisk_link,
                    extract_code,
                    card_id,
                    automation_rule_id,
                    status,
                    now,
                    now,
                ),
            )

