from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx


class PublishUncertainError(RuntimeError):
    """The publish call failed after it may have reached the platform."""


@dataclass(slots=True)
class PublishResult:
    item_id: str
    raw: dict[str, Any]


@dataclass(slots=True)
class DeliveryResources:
    card_id: int
    automation_rule_id: int
    raw: dict[str, Any]


class YdisksPort(Protocol):
    def publish_item(
        self,
        *,
        title: str,
        description: str,
        price_cents: int,
        normal_price_cents: int,
        images: list[Path],
    ) -> PublishResult: ...

    def find_items_by_title(self, title: str) -> list[dict[str, Any]]: ...

    def create_text_card(
        self, *, name: str, content: str, description: str
    ) -> int: ...

    def find_cards_by_name(self, name: str) -> list[dict[str, Any]]: ...

    def create_paid_delivery_rule(
        self, *, item_id: str, title: str, card_id: int
    ) -> int: ...

    def find_rules_for_item(self, item_id: str) -> list[dict[str, Any]]: ...

    def get_card_details(self, card_id: int) -> dict[str, Any]: ...

    def list_orders(self, *, page_size: int = 100) -> list[dict[str, Any]]: ...


def _first_id(payload: dict[str, Any], candidates: tuple[str, ...]) -> int | None:
    for key in candidates:
        value = payload.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        data = payload.get("data")
        if isinstance(data, dict) and data.get(key) is not None:
            try:
                return int(data[key])
            except (TypeError, ValueError):
                continue
    return None


def _first_string(payload: dict[str, Any], candidates: tuple[str, ...]) -> str | None:
    for container in (payload, payload.get("data")):
        if not isinstance(container, dict):
            continue
        for key in candidates:
            value = container.get(key)
            if value is not None and str(value).strip():
                return str(value)
    return None


class DryRunYdisksAdapter:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._cards: dict[int, dict[str, Any]] = {}
        self._rules: dict[int, dict[str, Any]] = {}
        self.publish_calls = 0

    def publish_item(
        self,
        *,
        title: str,
        description: str,
        price_cents: int,
        normal_price_cents: int,
        images: list[Path],
    ) -> PublishResult:
        self.publish_calls += 1
        existing = self.find_items_by_title(title)
        if existing:
            item = existing[0]
            return PublishResult(item_id=item["item_id"], raw=item)
        item_id = f"DRYRUN-{uuid.uuid4().hex[:12]}"
        payload = {
            "item_id": item_id,
            "item_title": title,
            "item_description": description,
            "item_price": f"{price_cents / 100:.2f}",
            "original_price": f"{normal_price_cents / 100:.2f}",
            "images": [str(path) for path in images],
            "dry_run": True,
        }
        self._items[item_id] = payload
        return PublishResult(item_id=item_id, raw=payload)

    def find_items_by_title(self, title: str) -> list[dict[str, Any]]:
        return [
            item for item in self._items.values() if item["item_title"] == title
        ]

    def create_text_card(
        self, *, name: str, content: str, description: str
    ) -> int:
        card_id = len(self._cards) + 1
        self._cards[card_id] = {
            "id": card_id,
            "name": name,
            "type": "text",
            "text_content": content,
            "description": description,
            "enabled": True,
        }
        return card_id

    def find_cards_by_name(self, name: str) -> list[dict[str, Any]]:
        return [card for card in self._cards.values() if card["name"] == name]

    def create_paid_delivery_rule(
        self, *, item_id: str, title: str, card_id: int
    ) -> int:
        rule_id = len(self._rules) + 1
        self._rules[rule_id] = {
            "id": rule_id,
            "cookie_id": "dry-run-cookie",
            "item_id": item_id,
            "name": f"{title} · 付款后自动发货",
            "trigger_type": "order_paid",
            "enabled": True,
            "priority": 100,
            "config_json": "{}",
            "actions": [
                {
                    "action_type": "send_card",
                    "card_id": card_id,
                    "delivery_count": 1,
                    "message_template": "资料链接和提取码已发送，请及时保存。",
                    "delay_seconds": 0,
                    "config_json": "{}",
                    "enabled": True,
                    "sort_order": 1,
                    "delivery_template_id": 0,
                    "template_bindings": [],
                    "custom_variables": {},
                }
            ],
        }
        return rule_id

    def find_rules_for_item(self, item_id: str) -> list[dict[str, Any]]:
        return [rule for rule in self._rules.values() if rule["item_id"] == item_id]

    def get_card_details(self, card_id: int) -> dict[str, Any]:
        return dict(self._cards.get(card_id, {"stock": 9999}))

    def list_orders(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        return []


class HttpYdisksAdapter:
    def __init__(
        self,
        base_url: str,
        *,
        cookie_id: str,
        session_cookie: str | None = None,
        username: str = "admin",
        password: str | None = None,
        publish_location: dict[str, Any] | None = None,
        timeout_seconds: float = 30,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookie_id = cookie_id
        self.username = username
        self.password = password
        self.publish_location = publish_location
        self.client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None
        if session_cookie:
            self.client.cookies.set("session", session_cookie)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _ensure_login(self) -> None:
        if self.client.cookies.get("session"):
            return
        if not self.password:
            raise RuntimeError(
                "Ydisks session cookie is missing and no admin password is configured"
            )
        response = self.client.post(
            f"{self.base_url}/api/v1/session/login",
            json={"username": self.username, "password": self.password},
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise RuntimeError(f"Ydisks login failed: {payload}")
        if not self.client.cookies.get("session"):
            raise RuntimeError("Ydisks login succeeded but no session cookie was returned")

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self._ensure_login()
        response = self.client.request(method, f"{self.base_url}{path}", **kwargs)
        response.raise_for_status()
        return response.json()

    def publish_item(
        self,
        *,
        title: str,
        description: str,
        price_cents: int,
        normal_price_cents: int,
        images: list[Path],
    ) -> PublishResult:
        handles = []
        try:
            category_payload = self._request(
                "POST",
                "/api/v1/items/publish-categories/recommend",
                json={"cookie_id": self.cookie_id, "keyword": title},
            )
            category = category_payload.get("category", {})
            category_id = str(category.get("cat_id") or "")
            if not category_id:
                raise RuntimeError(
                    f"Ydisks category recommendation lacks cat_id: {category_payload}"
                )
            files = []
            for path in images:
                handle = path.open("rb")
                handles.append(handle)
                files.append(("images", (path.name, handle, "image/png")))
            data = {
                "cookie_id": self.cookie_id,
                "title": title,
                "description": description,
                "price": f"{price_cents / 100:.2f}",
                "original_price": f"{normal_price_cents / 100:.2f}",
                "quantity": "1",
                "postage_mode": "none",
                "postage": "0.00",
                "category_id": category_id,
                "category_name": str(category.get("cat_name") or ""),
                "channel_category_id": str(category.get("channel_cat_id") or ""),
                "tb_category_id": str(category.get("tb_cat_id") or ""),
            }
            if self.publish_location:
                data["location"] = json.dumps(
                    self.publish_location, ensure_ascii=False
                )
            try:
                payload = self._request(
                    "POST", "/api/v1/items/publish", data=data, files=files
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise PublishUncertainError(str(exc)) from exc
        finally:
            for handle in handles:
                handle.close()
        item_id = _first_string(payload, ("item_id", "id", "itemId"))
        if not item_id:
            raise RuntimeError(f"Ydisks publish response lacks item_id: {payload}")
        return PublishResult(item_id=item_id, raw=payload)

    def find_items_by_title(self, title: str) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/api/v1/items",
            params={"cookie_id": self.cookie_id},
        )
        rows = payload if isinstance(payload, list) else payload.get("data", [])
        if isinstance(rows, dict):
            rows = rows.get("data", rows.get("items", []))
        matches = []
        for row in rows:
            row_title = str(row.get("item_title") or row.get("title") or "")
            if row_title.strip() == title.strip():
                matches.append(row)
        return matches

    def delete_item(self, item_id: str) -> dict[str, Any]:
        payload = self._request(
            "DELETE",
            f"/api/v1/items/{quote(self.cookie_id, safe='')}/"
            f"{quote(str(item_id), safe='')}",
        )
        return payload if isinstance(payload, dict) else {"data": payload}

    def create_text_card(
        self, *, name: str, content: str, description: str
    ) -> int:
        payload = self._request(
            "POST",
            "/api/v1/cards",
            json={
                "name": name,
                "type": "text",
                "text_content": content,
                "description": description,
                "enabled": True,
                "delay_seconds": 0,
            },
        )
        card_id = _first_id(payload, ("id", "card_id"))
        if card_id is None:
            raise RuntimeError(f"Ydisks card response lacks id: {payload}")
        return card_id

    def find_cards_by_name(self, name: str) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v1/cards")
        rows = payload if isinstance(payload, list) else payload.get("data", [])
        return [row for row in rows if str(row.get("name", "")) == name]

    def create_paid_delivery_rule(
        self, *, item_id: str, title: str, card_id: int
    ) -> int:
        payload = self._request(
            "POST",
            "/api/v1/automation-rules",
            json={
                "cookie_id": self.cookie_id,
                "item_id": item_id,
                "name": f"{title} · 付款后自动发货",
                "trigger_type": "order_paid",
                "enabled": True,
                "priority": 100,
                "config_json": "{}",
                "actions": [
                    {
                        "action_type": "send_card",
                        "card_id": card_id,
                        "delivery_count": 1,
                        "message_template": "资料链接和提取码已发送，请及时保存。",
                        "delay_seconds": 0,
                        "config_json": "{}",
                        "enabled": True,
                        "sort_order": 1,
                        "delivery_template_id": 0,
                        "template_bindings": [],
                        "custom_variables": {},
                    }
                ],
            },
        )
        rule_id = _first_id(payload, ("id", "rule_id"))
        if rule_id is None:
            raise RuntimeError(f"Ydisks rule response lacks id: {payload}")
        return rule_id

    def list_orders(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/api/v1/orders",
            params={
                "cookie_id": self.cookie_id,
                "page": 1,
                "page_size": page_size,
            },
        )
        return list(payload.get("data", []))

    def find_rules_for_item(self, item_id: str) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/api/v1/automation-rules",
            params={"cookie_id": self.cookie_id, "search": item_id, "page_size": 100},
        )
        rows = payload if isinstance(payload, list) else payload.get("data", [])
        return [row for row in rows if str(row.get("item_id")) == str(item_id)]

    def get_card_details(self, card_id: int) -> dict[str, Any]:
        payload = self._request("GET", f"/api/v1/cards/{card_id}/details")
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"]
        return payload

    def request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._request(method, path, **kwargs)


def describe_adapter(adapter: YdisksPort) -> dict[str, Any]:
    if isinstance(adapter, DryRunYdisksAdapter):
        return {"mode": "dry_run", "external_calls": False}
    return {
        "mode": "http",
        "base_url": getattr(adapter, "base_url", None),
        "external_calls": True,
    }

