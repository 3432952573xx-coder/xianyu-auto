#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xianyu_auto.config import keychain_get, keychain_set


def main() -> int:
    import lark_oapi as lark

    app_id = keychain_get("feishu_app_id")
    app_secret = keychain_get("feishu_app_secret")
    if not app_id or not app_secret:
        print("Feishu app_id/app_secret are not configured.", file=sys.stderr)
        return 1

    def on_message(event: object) -> None:
        data = getattr(event, "event", None)
        sender = getattr(data, "sender", None)
        sender_id = getattr(sender, "sender_id", None)
        open_id = getattr(sender_id, "open_id", None)
        if not open_id:
            return
        keychain_set("owner_open_id", open_id)
        print("FEISHU_OPEN_ID_SAVED=yes", flush=True)
        os._exit(0)

    builder = lark.EventDispatcherHandler.builder("", "")
    builder.register_p2_im_message_receive_v1(on_message)
    client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=builder.build(),
        log_level=lark.LogLevel.INFO,
        auto_reconnect=True,
    )
    print("BOT_READY=yes")
    client.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
