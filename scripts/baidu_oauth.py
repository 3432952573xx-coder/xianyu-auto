#!/usr/bin/env python3
from __future__ import annotations

import argparse
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xianyu_auto.config import keychain_get, keychain_set


def build_authorization_url(
    app_key: str, redirect_uri: str, state: str, *, force_login: bool
) -> str:
    params = {
        "response_type": "code",
        "client_id": app_key,
        "redirect_uri": redirect_uri,
        "scope": "basic,netdisk",
        "state": state,
        "display": "popup",
    }
    if force_login:
        params["force_login"] = "1"
    return f"https://openapi.baidu.com/oauth/2.0/authorize?{urlencode(params)}"


def exchange_code(
    *, code: str, app_key: str, app_secret: str, redirect_uri: str
) -> dict:
    response = httpx.post(
        "https://openapi.baidu.com/oauth/2.0/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": app_key,
            "client_secret": app_secret,
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("access_token") or not payload.get("refresh_token"):
        error = payload.get("error_description") or payload.get("error") or "unknown error"
        raise RuntimeError(f"百度 OAuth 未返回完整令牌：{error}")
    return payload


def verify_token(access_token: str) -> None:
    response = httpx.get(
        "https://pan.baidu.com/rest/2.0/xpan/nas",
        params={"method": "uinfo", "access_token": access_token},
        headers={"User-Agent": "pan.baidu.com"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if int(payload.get("errno", -1)) != 0:
        raise RuntimeError(f"百度授权验证失败：errno={payload.get('errno')}")


class CallbackHandler(BaseHTTPRequestHandler):
    server: "OAuthServer"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/start":
            self.send_response(302)
            self.send_header("Location", self.server.authorization_url)
            self.end_headers()
            return
        if parsed.path != "/callback":
            self.send_error(404)
            return

        params = parse_qs(parsed.query)
        state = params.get("state", [""])[0]
        if not secrets.compare_digest(state, self.server.expected_state):
            self._finish(400, "授权失败", "state 校验失败，请重新运行授权工具。")
            return
        if params.get("error"):
            self._finish(400, "授权失败", "百度账号未完成授权。")
            return

        code = params.get("code", [""])[0]
        if not code:
            self._finish(400, "授权失败", "回调中没有授权码。")
            return

        try:
            payload = exchange_code(
                code=code,
                app_key=self.server.app_key,
                app_secret=self.server.app_secret,
                redirect_uri=self.server.redirect_uri,
            )
            verify_token(payload["access_token"])
            keychain_set("baidu_access_token", payload["access_token"])
            keychain_set("baidu_refresh_token", payload["refresh_token"])
        except Exception as exc:
            self.server.error = f"{type(exc).__name__}: {exc}"
            self._finish(500, "授权失败", "令牌交换或验证失败，请回到终端查看原因。")
        else:
            self.server.succeeded = True
            self._finish(200, "授权成功", "百度网盘令牌已保存到 macOS Keychain。")
        finally:
            self.server.done.set()

    def _finish(self, status: int, title: str, message: str) -> None:
        body = (
            "<!doctype html><meta charset='utf-8'>"
            f"<title>{title}</title>"
            "<style>body{font-family:-apple-system,sans-serif;max-width:620px;"
            "margin:80px auto;padding:24px;line-height:1.7}</style>"
            f"<h1>{title}</h1><p>{message}</p>"
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class OAuthServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        *,
        expected_state: str,
        redirect_uri: str,
        app_key: str,
        app_secret: str,
        authorization_url: str,
    ) -> None:
        super().__init__(address, CallbackHandler)
        self.expected_state = expected_state
        self.redirect_uri = redirect_uri
        self.app_key = app_key
        self.app_secret = app_secret
        self.authorization_url = authorization_url
        self.done = threading.Event()
        self.succeeded = False
        self.error: str | None = None


def main() -> None:
    parser = argparse.ArgumentParser(description="百度网盘 OAuth 授权码模式本地接收工具")
    parser.add_argument("--port", type=int, default=8789)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--no-force-login", action="store_true")
    args = parser.parse_args()

    app_key = keychain_get("baidu_app_key")
    app_secret = keychain_get("baidu_app_secret")
    if not app_key or not app_secret:
        raise SystemExit("请先在本机 Keychain 配置 baidu_app_key 和 baidu_app_secret")

    redirect_uri = f"http://localhost:{args.port}/callback"
    state = secrets.token_urlsafe(24)
    authorization_url = build_authorization_url(
        app_key, redirect_uri, state, force_login=not args.no_force_login
    )
    server = OAuthServer(
        ("127.0.0.1", args.port),
        expected_state=state,
        redirect_uri=redirect_uri,
        app_key=app_key,
        app_secret=app_secret,
        authorization_url=authorization_url,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("请先在百度应用“安全设置”中添加 OAuth 授权回调页地址：")
    print(redirect_uri)
    print("\n然后由你本人打开下面本机入口完成百度账号授权：")
    print(f"http://localhost:{args.port}/start")
    print(f"\n本工具只监听本机，等待 {args.timeout} 秒。")

    completed = server.done.wait(args.timeout)
    server.shutdown()
    server.server_close()
    if not completed:
        raise SystemExit("授权等待超时，请重新运行。")
    if not server.succeeded:
        raise SystemExit(server.error or "授权失败")
    print("授权成功：access token 与 refresh token 已写入 macOS Keychain。")


if __name__ == "__main__":
    main()
