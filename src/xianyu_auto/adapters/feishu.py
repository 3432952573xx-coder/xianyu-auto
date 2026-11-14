from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import httpx


FEISHU_BASE = "https://open.feishu.cn/open-apis"


class FeishuError(RuntimeError):
    pass


class FeishuClient:
    def __init__(
        self,
        *,
        app_id: str | None,
        app_secret: str | None,
        owner_open_id: str | None,
        base_url: str = FEISHU_BASE,
        client: httpx.Client | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.owner_open_id = owner_open_id
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=30)
        self._owns_client = client is None
        self._token: str | None = None
        self._token_lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret and self.owner_open_id)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _tenant_token(self, *, force: bool = False) -> str:
        with self._token_lock:
            if self._token and not force:
                return self._token
            if not self.app_id or not self.app_secret:
                raise FeishuError("Feishu app_id/app_secret are not configured")
            response = self.client.post(
                f"{self.base_url}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            response.raise_for_status()
            payload = response.json()
            token = payload.get("tenant_access_token")
            if not token:
                raise FeishuError(f"Feishu token request failed: {payload}")
            self._token = token
            return token

    def _request(
        self,
        method: str,
        path: str,
        *,
        retry_auth: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        token = self._tenant_token()
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {token}"
        response = self.client.request(
            method, f"{self.base_url}{path}", headers=headers, **kwargs
        )
        if response.status_code == 401 and retry_auth:
            self._token = None
            return self._request(method, path, retry_auth=False, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code", 0) != 0:
            raise FeishuError(f"Feishu API error: {payload}")
        return payload

    def upload_image(self, path: Path) -> str:
        with path.open("rb") as handle:
            payload = self._request(
                "POST",
                "/im/v1/images",
                data={"image_type": "message"},
                files={"image": (path.name, handle, "image/png")},
            )
        image_key = payload.get("data", {}).get("image_key")
        if not image_key:
            raise FeishuError(f"Feishu upload response lacks image_key: {payload}")
        return str(image_key)

    def send_message(
        self,
        *,
        receive_id: str,
        msg_type: str,
        content: dict[str, Any] | str,
    ) -> str:
        body_content = (
            content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        )
        payload = self._request(
            "POST",
            "/im/v1/messages",
            params={"receive_id_type": "open_id"},
            json={
                "receive_id": receive_id,
                "msg_type": msg_type,
                "content": body_content,
            },
        )
        message_id = payload.get("data", {}).get("message_id")
        if not message_id:
            raise FeishuError(f"Feishu send response lacks message_id: {payload}")
        return str(message_id)

    def send_text(self, text: str, receive_id: str | None = None) -> str:
        target = receive_id or self.owner_open_id
        if not target:
            raise FeishuError("Feishu owner_open_id is not configured")
        return self.send_message(
            receive_id=target,
            msg_type="text",
            content={"text": text},
        )

    def send_approval_card(
        self,
        *,
        approval_id: str,
        title: str,
        price: str,
        normal_price: str,
        category: str,
        pages: int,
        file_size: str,
        content_hash: str,
        checks: list[str],
        risks: list[str],
        preview_images: list[Path],
    ) -> tuple[str, list[str]]:
        image_keys = [self.upload_image(path) for path in preview_images[:3]]
        elements: list[dict[str, Any]] = []
        for image_key in image_keys:
            elements.append(
                {
                    "tag": "img",
                    "img_key": image_key,
                    "alt": {"tag": "plain_text", "content": "商品详情预览"},
                    "mode": "fit_horizontal",
                    "preview": True,
                }
            )
        risk_text = "；".join(risks) if risks else "无额外风险标记"
        check_text = "；".join(checks)
        elements.extend(
            [
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**首周价**\n¥{price}",
                            },
                        },
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**正常价**\n¥{normal_price}",
                            },
                        },
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**类目**\n{category}",
                            },
                        },
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**规格**\n{pages} 页 / {file_size}",
                            },
                        },
                    ],
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**自动检查**\n{check_text}\n\n"
                            f"**风险提示**\n{risk_text}\n\n"
                            f"**内容哈希**\n`{content_hash[:24]}…`\n\n"
                            "通过后系统才会进入发布队列；若内容变化，本次审批自动失效。"
                        ),
                    },
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "通过并上架"},
                            "type": "primary",
                            "value": {"approval_id": approval_id, "action": "approve"},
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "驳回"},
                            "type": "danger",
                            "value": {"approval_id": approval_id, "action": "reject"},
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "提出修改意见"},
                            "type": "default",
                            "value": {
                                "approval_id": approval_id,
                                "action": "request_changes",
                            },
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "查看完整预览"},
                            "type": "default",
                            "value": {"approval_id": approval_id, "action": "preview"},
                        },
                    ],
                },
            ]
        )
        card = {
            "config": {"wide_screen_mode": True, "enable_forward": False},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": f"待审批 · {title}"},
            },
            "elements": elements,
        }
        if not self.owner_open_id:
            raise FeishuError("Feishu owner_open_id is not configured")
        message_id = self.send_message(
            receive_id=self.owner_open_id,
            msg_type="interactive",
            content=card,
        )
        return message_id, image_keys

    def send_reject_reason_card(self, approval_id: str, title: str) -> str:
        reasons = (
            ("内容错误", "content_error"),
            ("排版问题", "layout_issue"),
            ("定价不合适", "pricing"),
            ("版权风险", "copyright_risk"),
            ("其他", "other"),
        )
        actions = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": "default",
                "value": {
                    "approval_id": approval_id,
                    "action": "reject_reason",
                    "reason": reason,
                },
            }
            for label, reason in reasons
        ]
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "orange",
                "title": {"tag": "plain_text", "content": f"选择驳回原因 · {title}"},
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "选择原因后本次审批才会标记为驳回。商品不会被发布。",
                    },
                },
                {"tag": "action", "actions": actions},
            ],
        }
        return self.send_message(
            receive_id=self.owner_open_id or "",
            msg_type="interactive",
            content=card,
        )

    def send_revision_proposal_card(
        self,
        *,
        proposal_id: str,
        approval_id: str,
        title: str,
        feedback: str,
        current_title: str,
        proposed_title: str,
        current_price: str,
        proposed_price: str,
        understanding: str,
        title_reason: str,
        price_reason: str,
        image_change: str,
        notes: list[str],
        risks: list[str],
        can_apply: bool,
    ) -> str:
        feedback_text = feedback if len(feedback) <= 300 else f"{feedback[:300]}…"
        notes_text = "；".join(notes) if notes else "无"
        risks_text = "；".join(risks) if risks else "无额外风险"
        body = (
            f"**你的意见**\n{feedback_text}\n\n"
            f"**理解**\n{understanding}\n\n"
            f"**标题**\n{current_title} → {proposed_title}\n"
            f"{title_reason}\n\n"
            f"**价格**\n¥{current_price} → ¥{proposed_price}\n"
            f"{price_reason}\n\n"
            f"**详情图**\n{image_change}\n\n"
            f"**本阶段不自动执行**\n{notes_text}\n\n"
            f"**风险提示**\n{risks_text}"
        )
        actions = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "继续提修改意见"},
                "type": "default",
                "value": {
                    "approval_id": approval_id,
                    "proposal_id": proposal_id,
                    "action": "request_more_changes",
                },
            }
        ]
        if can_apply:
            actions.insert(
                0,
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "确认方案并重新提交审批"},
                    "type": "primary",
                    "value": {
                        "approval_id": approval_id,
                        "proposal_id": proposal_id,
                        "action": "apply_revision",
                    },
                },
            )
        card = {
            "config": {"wide_screen_mode": True, "enable_forward": False},
            "header": {
                "template": "turquoise" if can_apply else "orange",
                "title": {
                    "tag": "plain_text",
                    "content": f"修改方案 · {title}",
                },
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body}},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            "模型不会直接改商品或发布。确认方案后只会生成新版本并重新发起审批，"
                            "仍必须由你再次点击“通过并上架”。"
                        ),
                    },
                },
                {"tag": "action", "actions": actions},
            ],
        }
        return self.send_message(
            receive_id=self.owner_open_id or "",
            msg_type="interactive",
            content=card,
        )


class DryRunFeishuClient:
    configured = True

    def __init__(self, owner_open_id: str) -> None:
        self.owner_open_id = owner_open_id
        self.messages: list[dict[str, Any]] = []

    def send_text(self, text: str, receive_id: str | None = None) -> str:
        message_id = f"DRYRUN-MSG-{len(self.messages) + 1}"
        self.messages.append(
            {"message_id": message_id, "type": "text", "content": text}
        )
        return message_id

    def send_approval_card(self, **kwargs: Any) -> tuple[str, list[str]]:
        message_id = f"DRYRUN-CARD-{len(self.messages) + 1}"
        self.messages.append(
            {
                "message_id": message_id,
                "type": "approval_card",
                "approval_id": kwargs["approval_id"],
                "title": kwargs["title"],
            }
        )
        return message_id, ["DRYRUN-IMG"]

    def send_reject_reason_card(self, approval_id: str, title: str) -> str:
        message_id = f"DRYRUN-REJECT-{len(self.messages) + 1}"
        self.messages.append(
            {"message_id": message_id, "type": "reject_card", "approval_id": approval_id}
        )
        return message_id

    def send_revision_proposal_card(self, **kwargs: Any) -> str:
        message_id = f"DRYRUN-PROPOSAL-{len(self.messages) + 1}"
        self.messages.append(
            {
                "message_id": message_id,
                "type": "revision_proposal",
                **kwargs,
            }
        )
        return message_id


def update_approval_message_id(
    conn: sqlite3.Connection, approval_id: str, message_id: str
) -> None:
    conn.execute(
        "UPDATE approvals SET card_message_id=? WHERE id=?",
        (message_id, approval_id),
    )


def parse_card_action(event: Any) -> dict[str, Any]:
    data = event.event
    if data is None or data.operator is None or data.action is None:
        raise ValueError("card action event lacks operator or action")
    value = data.action.value or {}
    return {
        "open_id": data.operator.open_id,
        "message_id": data.context.open_message_id if data.context else None,
        "approval_id": value.get("approval_id"),
        "proposal_id": value.get("proposal_id"),
        "action": value.get("action"),
        "reason": value.get("reason"),
    }


def parse_message_event(event: Any) -> dict[str, str] | None:
    data = getattr(event, "event", None)
    sender = getattr(data, "sender", None)
    message = getattr(data, "message", None)
    sender_id = getattr(sender, "sender_id", None)
    open_id = getattr(sender_id, "open_id", None)
    sender_type = getattr(sender, "sender_type", None)
    chat_type = getattr(message, "chat_type", None)
    message_type = getattr(message, "message_type", None)
    content = getattr(message, "content", None)
    if (
        sender_type != "user"
        or chat_type != "p2p"
        or message_type != "text"
        or not open_id
        or not content
    ):
        return None
    try:
        text = json.loads(content).get("text", "").strip()
    except (TypeError, ValueError):
        return None
    if not text:
        return None
    return {"open_id": str(open_id), "text": text}


def build_event_handler(
    callback: Any,
    message_callback: Any | None = None,
) -> Any:
    import lark_oapi as lark
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackToast,
        P2CardActionTriggerResponse,
    )

    def _make_response(content: str) -> Any:
        response = P2CardActionTriggerResponse()
        response.toast = CallBackToast(
            {
                "type": "success",
                "content": content,
            }
        )
        return response

    def handler(event: Any) -> Any:
        try:
            parsed = parse_card_action(event)
            content = callback(
                approval_id=parsed["approval_id"],
                proposal_id=parsed["proposal_id"],
                action=parsed["action"],
                actor_open_id=parsed["open_id"],
                reason=parsed["reason"],
            )
            return _make_response(content)
        except PermissionError:
            return _make_response("只有配置的所有者可以审批")
        except Exception as exc:
            return _make_response(f"操作未完成：{str(exc)[:80]}")

    builder = lark.EventDispatcherHandler.builder("", "")
    builder.register_p2_card_action_trigger(handler)
    if message_callback is not None:
        def handle_message(event: Any) -> None:
            parsed = parse_message_event(event)
            if parsed:
                message_callback(
                    actor_open_id=parsed["open_id"],
                    text=parsed["text"],
                )

        builder.register_p2_im_message_receive_v1(handle_message)
    return builder.build()


def run_long_connection(
    *,
    app_id: str,
    app_secret: str,
    callback: Any,
    message_callback: Any | None = None,
) -> None:
    import lark_oapi as lark

    event_handler = build_event_handler(callback, message_callback)
    ws_client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
        auto_reconnect=True,
    )
    ws_client.start()

