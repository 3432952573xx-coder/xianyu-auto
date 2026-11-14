from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import httpx
import pytest

from xianyu_auto.adapters.netdisk import (
    BaiduNetdiskAdapter,
    ManualShareStore,
    QuarkWebShareAdapter,
)


def test_baidu_upload_and_share_requests(tmp_path: Path) -> None:
    payload = b"archive-bytes"
    archive = tmp_path / "delivery.zip"
    archive.write_bytes(payload)
    requests: list[httpx.Request] = []
    share_calls: list[tuple[str, str, int, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        method = request.url.params.get("method")
        if method == "precreate":
            return httpx.Response(200, json={"errno": 0, "uploadid": "upload-1"})
        if request.url.host == "d.pcs.baidu.com":
            return httpx.Response(200, json={"error_code": 0})
        if method == "create":
            return httpx.Response(200, json={"errno": 0, "fs_id": 123})
        return httpx.Response(404)

    def create_share(
        token: str, fs_id: str, period_days: int, pwd: str
    ) -> dict:
        share_calls.append((token, fs_id, period_days, pwd))
        return {"link": "https://pan.baidu.com/s/test", "pwd": "abcd"}

    adapter = BaiduNetdiskAdapter(
        access_token="access-token",
        app_id="app-id",
        remote_dir="/apps/test/deliveries",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        share_creator=create_share,
    )
    try:
        result = adapter.share_file(archive, archive.name)
    finally:
        adapter.close()

    assert result.link == "https://pan.baidu.com/s/test"
    assert result.extract_code == "abcd"
    assert result.raw["md5"] == hashlib.md5(payload).hexdigest()
    assert [request.url.params.get("method") for request in requests] == [
        "precreate",
        "upload",
        "create",
    ]
    precreate = requests[0]
    assert precreate.content
    assert "/apps/test/deliveries/delivery.zip" in unquote(
        precreate.content.decode()
    )
    assert share_calls == [("access-token", "123", 365, share_calls[0][3])]
    assert len(share_calls[0][3]) == 4
    assert all(character in "0123456789abcdef" for character in share_calls[0][3])


def test_baidu_mcp_share_is_saved_for_reuse(tmp_path: Path) -> None:
    payload = b"archive-bytes"
    archive = tmp_path / "delivery.zip"
    archive.write_bytes(payload)
    store_path = tmp_path / "manual-netdisk.json"

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.params.get("method")
        if method == "precreate":
            return httpx.Response(200, json={"errno": 0, "uploadid": "upload-1"})
        if request.url.host == "d.pcs.baidu.com":
            return httpx.Response(200, json={"error_code": 0})
        if method == "create":
            return httpx.Response(200, json={"errno": 0, "fs_id": 123})
        return httpx.Response(404)

    adapter = BaiduNetdiskAdapter(
        access_token="access-token",
        app_id="app-id",
        remote_dir="/apps/test/deliveries",
        manual_store=store_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        share_creator=lambda *_: {
            "link": "https://pan.baidu.com/s/automatic-share",
            "pwd": "ab12",
        },
    )
    try:
        result = adapter.share_file(archive, archive.name, share_key="product-one")
    finally:
        adapter.close()

    assert result.link == "https://pan.baidu.com/s/automatic-share"
    assert result.extract_code == "ab12"
    share = ManualShareStore(store_path).get_share("product-one")
    assert share == ("https://pan.baidu.com/s/automatic-share", "ab12")


def test_baidu_existing_folder_is_looked_up_before_sharing(tmp_path: Path) -> None:
    archive = tmp_path / "delivery.zip"
    archive.write_bytes(b"archive-bytes")
    share_fs_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.params.get("method")
        if request.url.path.endswith("/xpan/file") and method == "create":
            data = unquote(request.content.decode())
            if "isdir=1" in data:
                return httpx.Response(200, json={"category": 6})
            return httpx.Response(200, json={"errno": 0, "fs_id": 123})
        if request.url.path.endswith("/xpan/file") and method == "list":
            return httpx.Response(
                200,
                json={
                    "errno": 0,
                    "list": [
                        {
                            "path": "%2Fapps%2Ftest%2Fdeliveries%2Fproduct-one",
                            "fs_id": 777,
                            "isdir": 1,
                        }
                    ],
                },
            )
        if method == "precreate":
            return httpx.Response(200, json={"errno": 0, "uploadid": "upload-1"})
        if request.url.host == "d.pcs.baidu.com":
            return httpx.Response(200, json={"error_code": 0})
        return httpx.Response(404)

    def create_share(
        token: str, fs_id: str, period_days: int, pwd: str
    ) -> dict:
        share_fs_ids.append(fs_id)
        return {"link": "https://pan.baidu.com/s/existing-folder", "pwd": "ab12"}

    adapter = BaiduNetdiskAdapter(
        access_token="access-token",
        remote_dir="/apps/test/deliveries",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        share_creator=create_share,
    )
    try:
        result = adapter.share_file(archive, archive.name, share_key="product-one")
    finally:
        adapter.close()

    assert result.link == "https://pan.baidu.com/s/existing-folder"
    assert share_fs_ids == ["777"]


def test_baidu_mcp_error_does_not_create_fake_share(tmp_path: Path) -> None:
    archive = tmp_path / "delivery.zip"
    archive.write_bytes(b"archive-bytes")
    store_path = tmp_path / "manual-netdisk.json"

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.params.get("method")
        if method == "precreate":
            return httpx.Response(200, json={"errno": 0, "uploadid": "upload-1"})
        if request.url.host == "d.pcs.baidu.com":
            return httpx.Response(200, json={"error_code": 0})
        if method == "create":
            return httpx.Response(200, json={"errno": 0, "fs_id": 123})
        return httpx.Response(404)

    def fail_share(*_: object) -> dict:
        raise RuntimeError("Baidu MCP share failed: invalid app")

    adapter = BaiduNetdiskAdapter(
        access_token="access-token",
        app_id="app-id",
        remote_dir="/apps/test/deliveries",
        manual_store=store_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        share_creator=fail_share,
    )
    try:
        with pytest.raises(RuntimeError, match="invalid app"):
            adapter.share_file(archive, archive.name, share_key="product-one")
    finally:
        adapter.close()

    assert ManualShareStore(store_path).get_share("product-one") is None


def test_baidu_manual_share_is_reused_without_network(tmp_path: Path) -> None:
    archive = tmp_path / "delivery.zip"
    archive.write_bytes(b"archive-bytes")
    store_path = tmp_path / "manual-netdisk.json"
    ManualShareStore(store_path).save_share(
        "product-one",
        link="https://pan.baidu.com/s/real-share",
        extract_code="abcd",
        remote_name=archive.name,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    adapter = BaiduNetdiskAdapter(
        access_token="access-token",
        app_id="app-id",
        remote_dir="/apps/test/deliveries",
        manual_store=store_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        result = adapter.share_file(archive, archive.name, share_key="product-one")
    finally:
        adapter.close()

    assert result.link == "https://pan.baidu.com/s/real-share"
    assert result.extract_code == "abcd"
    assert result.raw["mode"] == "baidu_manual"


def test_baidu_new_version_uploads_to_shared_folder(tmp_path: Path) -> None:
    archive = tmp_path / "product-v2.pdf"
    archive.write_bytes(b"version-two")
    store_path = tmp_path / "manual-netdisk.json"
    ManualShareStore(store_path).save_share(
        "product-one",
        link="https://pan.baidu.com/s/stable-folder",
        extract_code="abcd",
        remote_name="product-v1.pdf",
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        method = request.url.params.get("method")
        if method == "create":
            return httpx.Response(200, json={"errno": 0, "fs_id": 123})
        if method == "precreate":
            return httpx.Response(200, json={"errno": 0, "uploadid": "upload-2"})
        if request.url.host == "d.pcs.baidu.com":
            return httpx.Response(200, json={"error_code": 0})
        return httpx.Response(404)

    adapter = BaiduNetdiskAdapter(
        access_token="access-token",
        app_id="app-id",
        remote_dir="/apps/test/deliveries",
        manual_store=store_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        result = adapter.share_file(
            archive, archive.name, share_key="product-one"
        )
    finally:
        adapter.close()

    assert result.link == "https://pan.baidu.com/s/stable-folder"
    assert result.extract_code == "abcd"
    assert result.raw["mode"] == "baidu_manual_folder"
    assert "/apps/test/deliveries/product-one/product-v2.pdf" in unquote(
        requests[1].content.decode()
    )
    entry = ManualShareStore(store_path).get_entry("product-one")
    assert entry is not None
    assert entry["last_remote_name"] == archive.name


class _QuarkResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self._payload = payload
        self.headers: dict[str, str] = {}

    def json(self) -> dict:
        return self._payload


def test_quark_security_check_fails_closed(tmp_path: Path) -> None:
    adapter = QuarkWebShareAdapter(profile_dir=tmp_path / "quark-browser")

    class ForbiddenRequest:
        def get(self, *args, **kwargs):
            raise AssertionError("API must not be called before security verification")

    page = SimpleNamespace(
        locator=lambda _: SimpleNamespace(
            inner_text=lambda timeout: "夸克网盘 安全验证"
        )
    )
    context = SimpleNamespace(request=ForbiddenRequest())

    with pytest.raises(RuntimeError, match="安全验证"):
        adapter._require_authenticated(page, context)


def test_quark_expired_login_fails_closed(tmp_path: Path) -> None:
    adapter = QuarkWebShareAdapter(profile_dir=tmp_path / "quark-browser")

    class ExpiredRequest:
        def get(self, *args, **kwargs):
            return _QuarkResponse(401, {"status": 401})

    page = SimpleNamespace(
        locator=lambda _: SimpleNamespace(inner_text=lambda timeout: "请扫码登录")
    )
    context = SimpleNamespace(request=ExpiredRequest())

    with pytest.raises(RuntimeError, match="登录已失效"):
        adapter._require_authenticated(page, context)


def test_quark_share_returns_real_link_and_code(tmp_path: Path) -> None:
    adapter = QuarkWebShareAdapter(profile_dir=tmp_path / "quark-browser")
    calls: list[tuple[str, dict]] = []

    class ShareRequest:
        def get(self, url: str, *, params=None, **kwargs):
            calls.append(("get", params or {}))
            return _QuarkResponse(
                200,
                {
                    "status": 200,
                    "data": {"status": 2, "share_id": "share-123"},
                },
            )

        def post(self, url: str, *, data=None, **kwargs):
            calls.append(("post", data or {}))
            if url.endswith("/share"):
                return _QuarkResponse(
                    200, {"status": 200, "data": {"task_id": "task-1"}}
                )
            if url.endswith("/share/password"):
                return _QuarkResponse(
                    200,
                    {
                        "status": 200,
                        "data": {
                            "share_url": "https://pan.quark.cn/s/real-share",
                            "passcode": "7x2k",
                        },
                    },
                )
            raise AssertionError(url)

    context = SimpleNamespace(request=ShareRequest())
    result = adapter._create_share(context, "file-id", "测试资料")

    assert result == {
        "link": "https://pan.quark.cn/s/real-share",
        "extract_code": "7x2k",
        "share_id": "share-123",
    }
    assert calls[0][1]["fid_list"] == ["file-id"]
    assert calls[0][1]["url_type"] == 2
    assert calls[0][1]["expired_type"] == 1


def test_quark_sha1_context_encodes_intermediate_state(tmp_path: Path) -> None:
    source = tmp_path / "part.bin"
    source.write_bytes(b"\0" * 64)
    encoded = QuarkWebShareAdapter._sha1_hash_context(source, 64)
    context = json.loads(base64.b64decode(encoded))

    assert context["hash_type"] == "sha1"
    assert context["Nl"] == str(64 * 8)
    assert all(context[key].isdigit() for key in ("h0", "h1", "h2", "h3", "h4"))
