from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx

from xianyu_auto.adapters.feishu import FeishuClient, parse_message_event


def test_approval_card_has_revision_action(tmp_path: Path) -> None:
    previews = []
    for index in range(3):
        path = tmp_path / f"{index}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        previews.append(path)
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"tenant_access_token": "token"})
        if path.endswith("/im/v1/images"):
            return httpx.Response(
                200, json={"code": 0, "data": {"image_key": "img-key"}}
            )
        if path.endswith("/im/v1/messages"):
            captured.update(json.loads(request.content))
            return httpx.Response(
                200, json={"code": 0, "data": {"message_id": "om-test"}}
            )
        return httpx.Response(404)

    client = FeishuClient(
        app_id="app-id",
        app_secret="app-secret",
        owner_open_id="owner-open-id",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        message_id, image_keys = client.send_approval_card(
            approval_id="approval-id",
            title="测试商品",
            price="5.90",
            normal_price="9.90",
            category="K12",
            pages=20,
            file_size="1 MB",
            content_hash="a" * 64,
            checks=["检查通过"],
            risks=[],
            preview_images=previews,
        )
    finally:
        client.close()

    assert message_id == "om-test"
    assert image_keys == ["img-key", "img-key", "img-key"]
    card = json.loads(str(captured["content"]))
    actions = next(
        element["actions"] for element in card["elements"] if element["tag"] == "action"
    )
    assert [action["text"]["content"] for action in actions] == [
        "通过并上架",
        "驳回",
        "提出修改意见",
        "查看完整预览",
    ]


def test_parse_p2p_text_message() -> None:
    event = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_type="user",
                sender_id=SimpleNamespace(open_id="ou-owner"),
            ),
            message=SimpleNamespace(
                chat_type="p2p",
                message_type="text",
                content=json.dumps({"text": "  标题改短一点  "}),
            ),
        )
    )
    assert parse_message_event(event) == {
        "open_id": "ou-owner",
        "text": "标题改短一点",
    }
