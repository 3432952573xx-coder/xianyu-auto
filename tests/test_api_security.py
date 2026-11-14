from __future__ import annotations

from fastapi.testclient import TestClient

from xianyu_auto.api import create_app
from xianyu_auto.service import Orchestrator

from .conftest import TEST_ADMIN_TOKEN, create_test_product


def test_health_is_public_and_state_requires_token(
    settings, orchestrator: Orchestrator
) -> None:
    app = create_app(settings, orchestrator)
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["mode"] == "dry_run"
        assert client.get("/api/products").status_code == 401
        authorized = client.get(
            "/api/products",
            headers={"X-Admin-Token": TEST_ADMIN_TOKEN},
        )
        assert authorized.status_code == 200


def test_api_decision_requires_owner(
    settings, orchestrator: Orchestrator
) -> None:
    product_id = create_test_product(orchestrator)
    approval = orchestrator.submit_product(product_id)
    app = create_app(settings, orchestrator)
    with TestClient(app) as client:
        response = client.post(
            f"/api/approvals/{approval['id']}/decision",
            headers={"X-Admin-Token": TEST_ADMIN_TOKEN},
            json={
                "approve": True,
                "actor_open_id": "not-owner",
                "reject_reason": None,
            },
        )
        assert response.status_code == 403

