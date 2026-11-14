from __future__ import annotations

import hmac
import json
import sqlite3
import threading
from contextlib import asynccontextmanager
from typing import Any, Iterator

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from .adapters.feishu import run_long_connection
from .config import Settings
from .scheduler import Scheduler
from .service import Orchestrator


class DecisionRequest(BaseModel):
    approve: bool
    actor_open_id: str = Field(min_length=1)
    reject_reason: str | None = None


def _row_to_product(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "version": row["version"],
        "title": row["title"],
        "description": row["description"],
        "category": row["category"],
        "price": f"{row['price_cents'] / 100:.2f}",
        "normal_price": f"{row['normal_price_cents'] / 100:.2f}",
        "pages": row["pages"],
        "status": row["status"],
        "content_hash": row["content_hash"],
        "risk_flags": json.loads(row["risk_flags_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _require_admin(settings: Settings, token: str | None) -> None:
    expected = settings.admin_token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="admin token is not configured; run xianyu-auto init",
        )
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="invalid admin token")


def create_app(
    settings: Settings, orchestrator: Orchestrator | None = None
) -> FastAPI:
    service = orchestrator or Orchestrator.from_settings(settings)
    scheduler = Scheduler(service)
    listener_thread: threading.Thread | None = None
    scheduler_started = False

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Iterator[None]:
        nonlocal listener_thread, scheduler_started
        service.recover_interrupted_jobs()
        if settings.scheduler_enabled:
            scheduler.start()
            scheduler_started = True
        if (
            settings.feishu_app_id
            and settings.feishu_app_secret
            and settings.owner_open_id
        ):
            def _listen() -> None:
                log_path = settings.home / "logs" / "feishu-listener.log"
                try:
                    log_path.unlink(missing_ok=True)
                    run_long_connection(
                        app_id=settings.feishu_app_id or "",
                        app_secret=settings.feishu_app_secret or "",
                        callback=service.handle_feishu_action,
                        message_callback=service.handle_feishu_message,
                    )
                except Exception as exc:
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path.write_text(
                        f"Feishu listener stopped: {type(exc).__name__}: {exc}\n",
                        encoding="utf-8",
                    )

            listener_thread = threading.Thread(
                target=_listen, name="feishu-long-connection", daemon=True
            )
            listener_thread.start()
        try:
            yield
        finally:
            if scheduler_started:
                scheduler.stop()
            service.close()

    app = FastAPI(
        title="XianyuAuto",
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.orchestrator = service
    app.state.scheduler = scheduler

    def admin_dependency(x_admin_token: str | None = Header(default=None)) -> None:
        _require_admin(settings, x_admin_token)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        try:
            service.db.one("SELECT 1")
            database = "ok"
        except Exception:
            database = "unavailable"
        return {
            "status": "ok" if database == "ok" else "degraded",
            "database": database,
            "version": "0.1.0",
            "mode": "dry_run" if settings.publish_dry_run else "live",
            "scheduler_enabled": settings.scheduler_enabled,
            "feishu_configured": bool(
                settings.feishu_app_id
                and settings.feishu_app_secret
                and settings.owner_open_id
            ),
            "feishu_listener": (
                "running"
                if listener_thread is not None and listener_thread.is_alive()
                else "stopped"
                if settings.feishu_app_id
                and settings.feishu_app_secret
                and settings.owner_open_id
                else "disabled"
            ),
            "vision_model_configured": bool(
                settings.vision_api_key
                and settings.vision_base_url
                and settings.vision_model
            ),
            "image_generation_configured": bool(
                settings.image_generation_enabled
                and settings.image_base_url
                and settings.image_model
            ),
            "timezone": settings.timezone,
        }

    @app.get("/api/products", dependencies=[Depends(admin_dependency)])
    def list_products() -> list[dict[str, Any]]:
        return [
            _row_to_product(row)
            for row in service.db.all("SELECT * FROM products ORDER BY id DESC")
        ]

    @app.get("/api/products/{product_id}", dependencies=[Depends(admin_dependency)])
    def get_product(product_id: int) -> dict[str, Any]:
        try:
            product = service.db.product(product_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="product not found") from exc
        result = _row_to_product(product)
        result["assets"] = [
            {
                "kind": row["kind"],
                "path": row["path"],
                "sha256": row["sha256"],
                "metadata": json.loads(row["metadata_json"]),
            }
            for row in service.db.all(
                "SELECT * FROM assets WHERE product_id=? ORDER BY id", (product_id,)
            )
        ]
        return result

    @app.get("/api/approvals/{approval_id}", dependencies=[Depends(admin_dependency)])
    def get_approval(approval_id: str) -> dict[str, Any]:
        try:
            approval = service.db.approval(approval_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="approval not found") from exc
        return dict(approval)

    @app.post(
        "/api/approvals/{approval_id}/decision",
        dependencies=[Depends(admin_dependency)],
    )
    def decide(approval_id: str, request: DecisionRequest) -> dict[str, Any]:
        try:
            return service.decide_approval(
                approval_id,
                approve=request.approve,
                actor_open_id=request.actor_open_id,
                reject_reason=request.reject_reason,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/api/approvals/{approval_id}/preview/{image_index}",
        dependencies=[Depends(admin_dependency)],
    )
    def preview(approval_id: str, image_index: int) -> FileResponse:
        try:
            approval = service.db.approval(approval_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="approval not found") from exc
        images = [
            row
            for row in service.db.all(
                "SELECT * FROM assets WHERE product_id=? AND kind='image' ORDER BY id",
                (approval["product_id"],),
            )
        ]
        if image_index < 0 or image_index >= len(images):
            raise HTTPException(status_code=404, detail="preview image not found")
        return FileResponse(images[image_index]["path"], media_type="image/png")

    @app.get("/api/jobs", dependencies=[Depends(admin_dependency)])
    def jobs(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in service.db.all(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
            )
        ]

    @app.get("/api/audit", dependencies=[Depends(admin_dependency)])
    def audit(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict[str, Any]]:
        rows = service.db.all(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [
            {**dict(row), "details": json.loads(row["details_json"])} for row in rows
        ]

    @app.post("/api/internal/reconcile", dependencies=[Depends(admin_dependency)])
    def reconcile() -> dict[str, Any]:
        return service.reconcile()

    @app.post("/api/internal/feishu-action")
    def feishu_http_action(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        raise HTTPException(
            status_code=410,
            detail="use Feishu long connection mode; HTTP card callback is disabled",
        )

    return app

