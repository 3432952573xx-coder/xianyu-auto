from __future__ import annotations

import argparse
import json
import secrets
import sys
import uuid
from pathlib import Path

import uvicorn

from .adapters.feishu import run_long_connection
from .api import create_app
from .config import DEFAULT_HOME, Settings, keychain_set
from .db import Database
from .models import ProductStatus
from .service import Orchestrator, build_demo_orchestrator, price_text
from .selection import BrowserKeywordSearch, run_selection


def _write_default_config(settings: Settings) -> None:
    settings.home.mkdir(parents=True, exist_ok=True)
    config_path = settings.home / "config.json"
    if config_path.exists():
        return
    payload = {
        "database_path": str(settings.home / "data" / "xianyu_auto.db"),
        "assets_dir": str(settings.home / "assets"),
        "yd_is_base_url": "http://127.0.0.1:59188",
        "yd_is_username": "admin",
        "netdisk_mode": "quark_web",
        "publish_dry_run": True,
        "auto_confirm_delivery": False,
        "daily_publish_limit": 1,
        "timezone": "Asia/Shanghai",
        "scheduler_enabled": True,
        "scheduler_tick_seconds": 30,
    }
    config_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    config_path.chmod(0o600)


def cmd_init(args: argparse.Namespace) -> int:
    settings = Settings.load(Path(args.home).expanduser() if args.home else None)
    _write_default_config(settings)
    settings = Settings.load(settings.home)
    if not settings.admin_token:
        token = secrets.token_urlsafe(32)
        keychain_set("admin_token", token)
        print("Admin API token generated and stored in macOS Keychain.")
    db = Database(settings.database_path)
    db.initialize()
    db.close()
    print(f"Initialized: {settings.home}")
    print(f"Database: {settings.database_path}")
    print("Mode: dry-run (publish_dry_run=true)")
    return 0


def cmd_samples(args: argparse.Namespace) -> int:
    service = Orchestrator.from_settings(Settings.load())
    try:
        manifests = service.generate_samples(register=True)
        for manifest in manifests:
            print(
                f"{manifest['slug']}: product_id={manifest['product_id']} "
                f"pages={manifest['pages']} hash={manifest['content_hash'][:16]}"
            )
    finally:
        service.close()
    return 0


def cmd_select(args: argparse.Namespace) -> int:
    settings = Settings.load()
    search = (
        BrowserKeywordSearch(
            profile_dir=settings.home / "selection-browser",
            headless=not args.headed,
        )
        if args.live
        else None
    )
    report = run_selection(
        cache_path=settings.home / "cache" / "selection-cache.json",
        search=search,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    service = Orchestrator.from_settings(Settings.load())
    try:
        approval = service.submit_product(int(args.product_id))
        print(json.dumps(approval, ensure_ascii=False, indent=2))
    finally:
        service.close()
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    settings = Settings.load()
    owner = settings.owner_open_id or (
        service_owner(settings) if hasattr(settings, "owner_open_id") else None
    )
    if not owner and settings.publish_dry_run:
        owner = "dry-run-owner"
    if not owner:
        raise SystemExit("owner_open_id is not configured")
    service = Orchestrator.from_settings(settings)
    try:
        result = service.decide_approval(
            args.approval_id,
            approve=args.approve,
            actor_open_id=owner,
            reject_reason=None if args.approve else args.reason,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        service.close()
    return 0


def service_owner(settings: Settings) -> str | None:
    return settings.owner_open_id


def cmd_worker_once(args: argparse.Namespace) -> int:
    service = Orchestrator.from_settings(Settings.load())
    try:
        processed = service.process_jobs(max_jobs=args.max_jobs)
        print(f"processed={processed}")
    finally:
        service.close()
    return 0


def cmd_reconcile(_: argparse.Namespace) -> int:
    service = Orchestrator.from_settings(Settings.load())
    try:
        print(json.dumps(service.reconcile(), ensure_ascii=False, indent=2))
    finally:
        service.close()
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    service = Orchestrator.from_settings(Settings.load())
    try:
        rows = service.db.all(
            """
            SELECT p.id, p.slug, p.title, p.status, p.price_cents,
                   a.id AS approval_id, a.status AS approval_status
            FROM products p
            LEFT JOIN approvals a ON a.product_id=p.id
            ORDER BY p.id DESC, a.created_at DESC
            """
        )
        for row in rows:
            print(
                f"{row['id']:>3} {row['status']:<16} "
                f"{price_text(row['price_cents']):>6} {row['title']} "
                f"approval={row['approval_id'] or '-'} ({row['approval_status'] or '-'})"
            )
    finally:
        service.close()
    return 0


def cmd_demo(_: argparse.Namespace) -> int:
    home = Path.cwd() / "work" / "demo-home" / uuid.uuid4().hex
    service = build_demo_orchestrator(home)
    try:
        manifests = service.generate_samples(register=True)
        product_id = manifests[0]["product_id"]
        approval = service.submit_product(product_id, owner_open_id="demo-owner")
        decision = service.decide_approval(
            approval["id"], approve=True, actor_open_id="demo-owner"
        )
        processed = service.process_jobs(max_jobs=5)
        product = service.db.product(product_id)
        binding = service.db.one(
            "SELECT * FROM delivery_bindings WHERE product_id=?", (product_id,)
        )
        print(
            json.dumps(
                {
                    "home": str(home),
                    "product_id": product_id,
                    "approval": approval,
                    "decision": decision,
                    "jobs_processed": processed,
                    "product_status": product["status"],
                    "delivery_binding": dict(binding) if binding else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        service.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    settings = Settings.load()
    app = create_app(settings)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_feishu_listen(_: argparse.Namespace) -> int:
    settings = Settings.load()
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        raise SystemExit("Feishu app_id/app_secret are not configured")
    service = Orchestrator.from_settings(settings)
    try:
        run_long_connection(
            app_id=settings.feishu_app_id,
            app_secret=settings.feishu_app_secret,
            callback=service.handle_feishu_action,
            message_callback=service.handle_feishu_message,
        )
    finally:
        service.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xianyu-auto")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="initialize local runtime")
    init.add_argument("--home", default=None)
    init.set_defaults(func=cmd_init)

    samples = sub.add_parser("generate-samples", help="generate and register three products")
    samples.set_defaults(func=cmd_samples)

    select = sub.add_parser("select", help="run daily selection with safe fallback")
    select.add_argument(
        "--live",
        action="store_true",
        help="read public Xianyu search pages without the seller account",
    )
    select.add_argument(
        "--headed",
        action="store_true",
        help="show the isolated selection browser window",
    )
    select.set_defaults(func=cmd_select)

    submit = sub.add_parser("submit", help="submit a DRAFT product for owner approval")
    submit.add_argument("--product-id", type=int, required=True)
    submit.set_defaults(func=cmd_submit)

    decide = sub.add_parser("decide", help="record an explicit owner decision")
    decide.add_argument("--approval-id", required=True)
    group = decide.add_mutually_exclusive_group(required=True)
    group.add_argument("--approve", action="store_true")
    group.add_argument("--reject", action="store_true")
    decide.add_argument(
        "--reason",
        choices=("content_error", "layout_issue", "pricing", "copyright_risk", "other"),
    )
    decide.set_defaults(func=cmd_decide)

    worker = sub.add_parser("worker-once", help="process pending jobs")
    worker.add_argument("--max-jobs", type=int, default=10)
    worker.set_defaults(func=cmd_worker_once)

    reconcile = sub.add_parser("reconcile", help="run delivery reconciliation")
    reconcile.set_defaults(func=cmd_reconcile)

    status = sub.add_parser("status", help="show products and approvals")
    status.set_defaults(func=cmd_status)

    demo = sub.add_parser("demo", help="run a self-contained dry-run demo")
    demo.set_defaults(func=cmd_demo)

    serve = sub.add_parser("serve", help="run local FastAPI service and scheduler")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.set_defaults(func=cmd_serve)

    feishu = sub.add_parser("feishu-listen", help="run Feishu long connection only")
    feishu.set_defaults(func=cmd_feishu_listen)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "decide":
        args.approve = bool(args.approve)
        if not args.approve and not args.reason:
            parser.error("--reason is required with --reject")
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()

