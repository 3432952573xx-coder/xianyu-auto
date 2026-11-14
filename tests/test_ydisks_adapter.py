from __future__ import annotations

import json

import httpx

from xianyu_auto.adapters.ydisks import HttpYdisksAdapter


def test_create_paid_delivery_rule_uses_supported_send_card_action() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/session/login":
            return httpx.Response(
                200,
                json={"success": True},
                headers={"set-cookie": "session=test-session; Path=/; HttpOnly"},
            )
        if request.url.path == "/api/v1/automation-rules":
            return httpx.Response(200, json={"success": True, "id": 42})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = HttpYdisksAdapter(
        "http://127.0.0.1:59188",
        cookie_id="account-1",
        username="admin",
        password="secret",
        client=client,
    )

    try:
        rule_id = adapter.create_paid_delivery_rule(
            item_id="item-1",
            title="测试商品",
            card_id=7,
        )
    finally:
        adapter.close()

    assert rule_id == 42
    rule_request = next(
        request
        for request in requests
        if request.url.path == "/api/v1/automation-rules"
    )
    payload = json.loads(rule_request.content)
    assert payload["cookie_id"] == "account-1"
    assert payload["item_id"] == "item-1"
    assert payload["trigger_type"] == "order_paid"
    assert payload["actions"][0]["action_type"] == "send_card"
    assert payload["actions"][0]["card_id"] == 7


def test_publish_item_uses_recommended_category(tmp_path) -> None:
    requests: list[httpx.Request] = []
    image = tmp_path / "cover.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/session/login":
            return httpx.Response(
                200,
                json={"success": True},
                headers={"set-cookie": "session=test-session; Path=/; HttpOnly"},
            )
        if request.url.path == "/api/v1/items/publish-categories/recommend":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "category": {
                        "cat_id": "cat-1",
                        "cat_name": "练字帖",
                        "channel_cat_id": "channel-1",
                        "tb_cat_id": "tb-1",
                    },
                },
            )
        if request.url.path == "/api/v1/items/publish":
            return httpx.Response(200, json={"success": True, "item_id": "item-1"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = HttpYdisksAdapter(
        "http://127.0.0.1:59188",
        cookie_id="account-1",
        username="admin",
        password="secret",
        publish_location={
            "province": "上海市",
            "city": "上海市",
            "area": "黄浦区",
            "division_id": "310101",
            "longitude": 121.475213,
            "latitude": 31.228827,
            "poi_id": "B00157917M",
            "poi_name": "上海人民广场",
        },
        client=client,
    )

    try:
        result = adapter.publish_item(
            title="测试字帖",
            description="电子版资料",
            price_cents=100,
            normal_price_cents=990,
            images=[image],
        )
    finally:
        adapter.close()

    assert result.item_id == "item-1"
    publish_request = next(
        request
        for request in requests
        if request.url.path == "/api/v1/items/publish"
    )
    body = publish_request.content
    assert b'name="category_id"\r\n\r\ncat-1' in body
    assert b'name="category_name"\r\n\r\n' in body
    assert b'name="channel_category_id"\r\n\r\nchannel-1' in body
    assert b'name="tb_category_id"\r\n\r\ntb-1' in body
    assert b'name="quantity"\r\n\r\n1' in body
    assert "上海人民广场".encode() in body


def test_find_rules_for_item_filters_http_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/session/login":
            return httpx.Response(
                200,
                json={"success": True},
                headers={"set-cookie": "session=test-session; Path=/; HttpOnly"},
            )
        if request.url.path == "/api/v1/automation-rules":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": 1, "item_id": "item-1", "enabled": True},
                        {"id": 2, "item_id": "item-2", "enabled": True},
                    ]
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = HttpYdisksAdapter(
        "http://127.0.0.1:59188",
        cookie_id="account-1",
        username="admin",
        password="secret",
        client=client,
    )

    try:
        rules = adapter.find_rules_for_item("item-1")
    finally:
        adapter.close()

    assert rules == [{"id": 1, "item_id": "item-1", "enabled": True}]


def test_delete_item_uses_account_scoped_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/session/login":
            return httpx.Response(
                200,
                json={"success": True},
                headers={"set-cookie": "session=test-session; Path=/; HttpOnly"},
            )
        if request.url.path == "/api/v1/items/account-1/item-1":
            return httpx.Response(200, json={"success": True})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = HttpYdisksAdapter(
        "http://127.0.0.1:59188",
        cookie_id="account-1",
        username="admin",
        password="secret",
        client=client,
    )

    try:
        result = adapter.delete_item("item-1")
    finally:
        adapter.close()

    delete_request = next(
        request for request in requests if request.method == "DELETE"
    )
    assert delete_request.url.path == "/api/v1/items/account-1/item-1"
    assert result == {"success": True}
