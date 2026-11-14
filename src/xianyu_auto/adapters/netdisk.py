from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import secrets
import string
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import unquote

import httpx
from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright


@dataclass(slots=True)
class ShareResult:
    link: str
    extract_code: str
    raw: dict


class ManualShareRequired(RuntimeError):
    """The folder was prepared, but the platform requires a human-created share."""

    def __init__(
        self,
        *,
        share_key: str,
        remote_name: str,
        remote_path: str,
        fs_id: str | int | None,
    ) -> None:
        self.share_key = share_key
        self.remote_name = remote_name
        self.remote_path = remote_path
        self.fs_id = str(fs_id) if fs_id is not None else None
        super().__init__(
            "百度网盘商品目录已准备好，但当前应用未开通自动分享；"
            f"请在百度网盘 App 中一次性分享目录 {remote_path}，"
            "然后用 set_baidu_share.py 在本机录入链接和提取码。"
        )


class ManualShareStore:
    """Local, owner-only mapping from stable share keys to manual shares."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"shares": {}, "pending": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取手动分享映射：{self.path}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"手动分享映射格式错误：{self.path}")
        data.setdefault("shares", {})
        data.setdefault("pending", {})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def get_share(self, remote_name: str) -> tuple[str, str] | None:
        entry = self.get_entry(remote_name)
        if not isinstance(entry, dict):
            return None
        link = str(entry.get("link") or "").strip()
        code = str(entry.get("extract_code") or "").strip()
        return (link, code) if link and code else None

    def get_entry(self, share_key: str) -> dict[str, Any] | None:
        entry = self._load()["shares"].get(share_key)
        return entry if isinstance(entry, dict) else None

    def get_pending(self, share_key: str) -> dict[str, Any] | None:
        entry = self._load()["pending"].get(share_key)
        return entry if isinstance(entry, dict) else None

    def pending(self) -> dict[str, Any]:
        return dict(self._load()["pending"])

    def save_pending(
        self,
        share_key: str,
        *,
        remote_name: str,
        remote_path: str,
        fs_id: str | int | None,
    ) -> None:
        data = self._load()
        data["pending"][share_key] = {
            "remote_name": remote_name,
            "remote_path": remote_path,
            "fs_id": str(fs_id) if fs_id is not None else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save(data)

    def save_share(
        self,
        share_key: str,
        *,
        link: str,
        extract_code: str,
        remote_name: str | None = None,
    ) -> None:
        data = self._load()
        pending = data["pending"].pop(share_key, {})
        data["shares"][share_key] = {
            "link": link,
            "extract_code": extract_code,
            "last_remote_name": remote_name or pending.get("remote_name"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save(data)

    def mark_uploaded(self, share_key: str, *, remote_name: str) -> None:
        data = self._load()
        entry = data["shares"].get(share_key)
        if not isinstance(entry, dict):
            raise RuntimeError(f"分享记录不存在：{share_key}")
        entry["last_remote_name"] = remote_name
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save(data)


class NetdiskPort(Protocol):
    def share_file(
        self,
        file_path: Path,
        remote_name: str,
        *,
        share_key: str | None = None,
    ) -> ShareResult: ...


ShareCreator = Callable[[str, str, int, str], dict[str, Any]]


class ManualNetdiskAdapter:
    def __init__(self, link: str | None, code: str | None):
        self.link = link
        self.code = code

    def share_file(
        self,
        file_path: Path,
        remote_name: str,
        *,
        share_key: str | None = None,
    ) -> ShareResult:
        if not self.link or not self.code:
            raise RuntimeError(
                "manual netdisk link/code missing; configure them in Keychain before approval"
            )
        return ShareResult(
            link=self.link,
            extract_code=self.code,
            raw={"mode": "manual", "remote_name": remote_name},
        )


class DryRunNetdiskAdapter:
    def share_file(
        self,
        file_path: Path,
        remote_name: str,
        *,
        share_key: str | None = None,
    ) -> ShareResult:
        token = hashlib.sha256(
            f"{file_path}:{remote_name}:{share_key or ''}".encode()
        ).hexdigest()[:12]
        return ShareResult(
            link=f"https://pan.baidu.com/s/DRYRUN{token}",
            extract_code="DRYRUN",
            raw={
                "mode": "dry_run",
                "remote_name": remote_name,
                "share_key": share_key,
            },
        )


class QuarkWebShareAdapter:
    """Upload and share through a normally authenticated Quark web session."""

    API_BASE = "https://drive-pc.quark.cn/1/clouddrive"
    COMMON_PARAMS = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
    HEADERS = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "origin": "https://pan.quark.cn",
        "referer": "https://pan.quark.cn/",
    }
    PART_SIZE = 4 * 1024 * 1024
    SINGLE_PART_LIMIT = 5 * 1024 * 1024

    def __init__(
        self,
        *,
        profile_dir: Path,
        headless: bool = True,
        expire_days: int = 0,
        request_timeout_ms: int = 120_000,
    ) -> None:
        self.profile_dir = profile_dir
        self.headless = headless
        self.expire_days = expire_days
        self.request_timeout_ms = request_timeout_ms

    def share_file(
        self,
        file_path: Path,
        remote_name: str,
        *,
        share_key: str | None = None,
    ) -> ShareResult:
        file_path = file_path.expanduser().resolve()
        if not file_path.is_file():
            raise FileNotFoundError(file_path)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir.chmod(0o700)
        with sync_playwright() as playwright:
            context = self._open_context(playwright)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(
                    "https://pan.quark.cn/",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                page.wait_for_timeout(2_000)
                capacity = self._require_authenticated(page, context)
                self._ensure_capacity(capacity, file_path.stat().st_size)
                return self._upload_and_share(context, file_path, remote_name)
            finally:
                context.close()

    def _open_context(self, playwright: Playwright) -> BrowserContext:
        return playwright.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=self.headless,
            locale="zh-CN",
            viewport={"width": 1440, "height": 1000},
            args=["--disable-blink-features=AutomationControlled"],
        )

    def _require_authenticated(
        self, page: Page, context: BrowserContext
    ) -> dict[str, Any]:
        body = ""
        try:
            body = page.locator("body").inner_text(timeout=5_000)
        except Exception:
            pass
        security_terms = ("安全验证", "验证码", "账号异常", "异常登录", "访问受限")
        if any(term in body for term in security_terms):
            raise RuntimeError(
                "夸克网盘要求安全验证；请在专用登录窗口中人工完成，程序不会绕过验证"
            )

        response = context.request.get(
            f"{self.API_BASE}/member",
            params=self._params(),
            headers=self.HEADERS,
            timeout=self.request_timeout_ms,
        )
        if response.status in {401, 403}:
            raise RuntimeError(
                "夸克网盘登录已失效；请运行 ./scripts/login_quark.py 后重新扫码登录"
            )
        if response.status >= 400:
            raise RuntimeError(f"夸克网盘登录检查失败：HTTP {response.status}")
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") != 200:
            status = payload.get("status") if isinstance(payload, dict) else None
            raise RuntimeError(
                "夸克网盘登录不可用"
                + (f"：status={status}" if status is not None else "")
                + "；请运行 ./scripts/login_quark.py 重新登录"
            )
        return payload

    @staticmethod
    def _ensure_capacity(capacity_payload: dict[str, Any], file_size: int) -> None:
        data = capacity_payload.get("data") or {}
        try:
            total = int(data.get("total_capacity") or data.get("total") or 0)
            used = int(data.get("use_capacity") or data.get("used") or 0)
        except (TypeError, ValueError):
            return
        if total > 0 and used + file_size > total:
            available = max(total - used, 0)
            unit = 1024**3
            raise RuntimeError(
                "夸克网盘容量不足："
                f"当前已用 {used / unit:.2f} GB，配额 {total / unit:.2f} GB，"
                f"剩余 {available / unit:.2f} GB，本次需要 {file_size / (1024**2):.2f} MB；"
                "请清理文件或扩容后重试"
            )

    def _upload_and_share(
        self,
        context: BrowserContext,
        file_path: Path,
        remote_name: str,
    ) -> ShareResult:
        size = file_path.stat().st_size
        if size <= 0:
            raise RuntimeError("夸克上传文件为空")
        md5_hash, sha1_hash, sha256_hash = self._hash_file(file_path)
        now_ms = int(time.time() * 1000)
        pre_payload = self._api(
            context,
            "POST",
            "file/upload/pre",
            json_data={
                "ccp_hash_update": True,
                "parallel_upload": True,
                "pdir_fid": "0",
                "dir_name": "",
                "size": size,
                "file_name": remote_name,
                "format_type": mimetypes.guess_type(remote_name)[0]
                or "application/octet-stream",
                "l_updated_at": now_ms,
                "l_created_at": now_ms,
            },
        )
        data = pre_payload.get("data") or {}
        task_id = data.get("task_id")
        auth_info = data.get("auth_info", "")
        upload_id = data.get("upload_id", "")
        obj_key = data.get("obj_key", "")
        bucket = data.get("bucket", "ul-zb")
        callback_info = data.get("callback") or {}
        if not all((task_id, upload_id, obj_key, callback_info)):
            raise RuntimeError("夸克预上传响应缺少必要字段")

        self._api(
            context,
            "POST",
            "file/update/hash",
            json_data={"task_id": task_id, "md5": md5_hash, "sha1": sha1_hash},
        )

        if size <= self.SINGLE_PART_LIMIT:
            parts = [(1, size, "")]
        else:
            part_count = (size + self.PART_SIZE - 1) // self.PART_SIZE
            parts = [
                (
                    part_number,
                    min(self.PART_SIZE, size - (part_number - 1) * self.PART_SIZE),
                    "",
                )
                for part_number in range(1, part_count + 1)
            ]

        uploaded: list[tuple[int, str]] = []
        mime_type = mimetypes.guess_type(remote_name)[0] or "application/octet-stream"
        with file_path.open("rb") as handle:
            for part_number, part_size, _ in parts:
                handle.seek((part_number - 1) * self.PART_SIZE)
                chunk = handle.read(part_size)
                hash_context = (
                    self._sha1_hash_context(file_path, (part_number - 1) * self.PART_SIZE)
                    if len(parts) > 1 and part_number > 1
                    else ""
                )
                oss_date = datetime.now(timezone.utc).strftime(
                    "%a, %d %b %Y %H:%M:%S GMT"
                )
                auth_meta = self._put_auth_meta(
                    mime_type=mime_type,
                    oss_date=oss_date,
                    bucket=bucket,
                    obj_key=obj_key,
                    upload_id=upload_id,
                    part_number=part_number,
                    hash_context=hash_context,
                )
                auth_payload = self._api(
                    context,
                    "POST",
                    "file/upload/auth",
                    json_data={
                        "task_id": task_id,
                        "auth_info": auth_info,
                        "auth_meta": auth_meta,
                    },
                )
                auth_key = (auth_payload.get("data") or {}).get("auth_key", "")
                if not auth_key:
                    raise RuntimeError(f"夸克未返回第 {part_number} 片上传授权")
                upload_headers = {
                    "Content-Type": mime_type,
                    "x-oss-date": oss_date,
                    "x-oss-user-agent": self._oss_user_agent(),
                    "authorization": auth_key,
                }
                if hash_context:
                    upload_headers["x-oss-hash-ctx"] = hash_context
                upload_url = (
                    f"https://{bucket}.pds.quark.cn/{obj_key}"
                    f"?partNumber={part_number}&uploadId={upload_id}"
                )
                response = context.request.put(
                    upload_url,
                    data=chunk,
                    headers=upload_headers,
                    timeout=self.request_timeout_ms,
                )
                if response.status not in {200, 201}:
                    raise RuntimeError(
                        f"夸克第 {part_number} 片上传失败：HTTP {response.status}"
                    )
                etag = (response.headers.get("etag") or "").strip('"')
                if not etag:
                    raise RuntimeError(f"夸克第 {part_number} 片上传缺少 ETag")
                uploaded.append((part_number, etag))

        if len(uploaded) == 1:
            merge_parts = uploaded
        else:
            merge_parts = uploaded
        xml_parts = "".join(
            f"<Part><PartNumber>{number}</PartNumber>"
            f"<ETag>\"{etag}\"</ETag></Part>"
            for number, etag in merge_parts
        )
        xml_data = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f"<CompleteMultipartUpload>{xml_parts}</CompleteMultipartUpload>"
        )
        self._complete_upload(
            context,
            task_id=task_id,
            auth_info=auth_info,
            upload_id=upload_id,
            obj_key=obj_key,
            bucket=bucket,
            callback_info=callback_info,
            xml_data=xml_data,
        )
        finish_payload = self._api(
            context,
            "POST",
            "file/upload/finish",
            json_data={"task_id": task_id, "obj_key": obj_key},
        )
        file_id = self._find_file_id(
            context,
            remote_name=remote_name,
            fallback=(finish_payload.get("data") or {}).get("fid"),
        )
        share = self._create_share(context, file_id, remote_name)
        return ShareResult(
            link=share["link"],
            extract_code=share["extract_code"],
            raw={
                "mode": "quark_web",
                "file_id": file_id,
                "md5": md5_hash,
                "sha256": sha256_hash,
                "share_id": share["share_id"],
            },
        )

    def _complete_upload(
        self,
        context: BrowserContext,
        *,
        task_id: str,
        auth_info: str,
        upload_id: str,
        obj_key: str,
        bucket: str,
        callback_info: dict[str, Any],
        xml_data: str,
    ) -> None:
        oss_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        xml_md5 = base64.b64encode(
            hashlib.md5(xml_data.encode("utf-8")).digest()
        ).decode("ascii")
        callback = base64.b64encode(
            json.dumps(callback_info, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        user_agent = self._oss_user_agent()
        auth_meta = (
            f"POST\n{xml_md5}\napplication/xml\n{oss_date}\n"
            f"x-oss-callback:{callback}\n"
            f"x-oss-date:{oss_date}\n"
            f"x-oss-user-agent:{user_agent}\n"
            f"/{bucket}/{obj_key}?uploadId={upload_id}"
        )
        auth_payload = self._api(
            context,
            "POST",
            "file/upload/auth",
            json_data={
                "task_id": task_id,
                "auth_meta": auth_meta,
                "auth_info": auth_info,
            },
        )
        auth_key = (auth_payload.get("data") or {}).get("auth_key", "")
        if not auth_key:
            raise RuntimeError("夸克未返回合并上传授权")
        response = context.request.post(
            f"https://{bucket}.pds.quark.cn/{obj_key}?uploadId={upload_id}",
            data=xml_data.encode("utf-8"),
            headers={
                "Content-Type": "application/xml",
                "Content-MD5": xml_md5,
                "x-oss-callback": callback,
                "x-oss-date": oss_date,
                "x-oss-user-agent": user_agent,
                "authorization": auth_key,
            },
            timeout=self.request_timeout_ms,
        )
        if response.status not in {200, 203}:
            raise RuntimeError(f"夸克合并上传失败：HTTP {response.status}")

    def _find_file_id(
        self,
        context: BrowserContext,
        *,
        remote_name: str,
        fallback: Any = None,
    ) -> str:
        if fallback:
            return str(fallback)
        for _ in range(10):
            payload = self._api(
                context,
                "GET",
                "file/sort",
                params={
                    "pdir_fid": "0",
                    "_page": 1,
                    "_size": 100,
                    "_sort": "updated_at:desc",
                },
            )
            matches = [
                item
                for item in (payload.get("data") or {}).get("list", [])
                if item.get("file_name") == remote_name and not item.get("dir")
            ]
            if matches:
                matches.sort(key=lambda item: int(item.get("updated_at") or 0), reverse=True)
                return str(matches[0]["fid"])
            time.sleep(1)
        raise RuntimeError("夸克上传已完成，但在网盘中找不到目标文件")

    def _create_share(
        self,
        context: BrowserContext,
        file_id: str,
        title: str,
    ) -> dict[str, str]:
        alphabet = string.ascii_letters + string.digits
        password = "".join(secrets.choice(alphabet) for _ in range(4))
        payload: dict[str, Any] = {
            "fid_list": [file_id],
            "title": title,
            "url_type": 2,
            "expired_type": 1 if self.expire_days == 0 else 2,
            "passcode": password,
        }
        if self.expire_days:
            payload["expired_at"] = int(
                (time.time() + self.expire_days * 24 * 3600) * 1000
            )
        created = self._api(context, "POST", "share", json_data=payload)
        task_id = (created.get("data") or {}).get("task_id")
        if not task_id:
            raise RuntimeError("夸克创建分享响应缺少 task_id")
        share_id = ""
        for retry_index in range(20):
            task = self._api(
                context,
                "GET",
                "task",
                params={"task_id": task_id, "retry_index": retry_index},
            )
            task_data = task.get("data") or {}
            if task_data.get("status") == 2:
                share_id = str(task_data.get("share_id") or "")
                break
            if task_data.get("status") == 3:
                raise RuntimeError("夸克创建分享失败")
            time.sleep(1)
        if not share_id:
            raise RuntimeError("夸克创建分享超时")
        details = self._api(
            context,
            "POST",
            "share/password",
            json_data={"share_id": share_id},
        )
        share_data = details.get("data") or {}
        link = str(share_data.get("share_url") or "")
        if not link.startswith("https://pan.quark.cn/s/"):
            raise RuntimeError("夸克分享响应缺少有效分享链接")
        return {
            "link": link,
            "extract_code": str(share_data.get("passcode") or password),
            "share_id": share_id,
        }

    def _api(
        self,
        context: BrowserContext,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_params = self._params()
        request_params.update(params or {})
        url = f"{self.API_BASE}/{path.lstrip('/')}"
        if method == "GET":
            response = context.request.get(
                url,
                params=request_params,
                headers=self.HEADERS,
                timeout=self.request_timeout_ms,
            )
        else:
            response = context.request.post(
                url,
                params=request_params,
                data=json_data,
                headers=self.HEADERS,
                timeout=self.request_timeout_ms,
            )
        if response.status in {401, 403}:
            raise RuntimeError("夸克网盘登录已失效，请重新运行登录脚本")
        if response.status >= 400:
            message = ""
            try:
                error_payload = response.json()
                message = str(error_payload.get("message") or "")[:120]
            except Exception:
                pass
            raise RuntimeError(
                f"夸克网盘接口失败：HTTP {response.status}"
                + (f", message={message}" if message else "")
            )
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") not in {200, 2000000}:
            status = payload.get("status") if isinstance(payload, dict) else None
            message = payload.get("message") if isinstance(payload, dict) else None
            safe_message = str(message or "")[:120]
            raise RuntimeError(
                f"夸克网盘接口失败：status={status}"
                + (f", message={safe_message}" if safe_message else "")
            )
        return payload

    @classmethod
    def _params(cls) -> dict[str, Any]:
        params: dict[str, Any] = dict(cls.COMMON_PARAMS)
        params["__t"] = int(time.time() * 1000)
        params["__dt"] = 1000
        return params

    @classmethod
    def _oss_user_agent(cls) -> str:
        return "aliyun-sdk-js/1.0.0 Chrome 139.0.0.0 on OS X 10.15.7 64-bit"

    @classmethod
    def _put_auth_meta(
        cls,
        *,
        mime_type: str,
        oss_date: str,
        bucket: str,
        obj_key: str,
        upload_id: str,
        part_number: int,
        hash_context: str,
    ) -> str:
        hash_line = f"x-oss-hash-ctx:{hash_context}\n" if hash_context else ""
        return (
            f"PUT\n\n{mime_type}\n{oss_date}\n"
            f"x-oss-date:{oss_date}\n"
            f"{hash_line}"
            f"x-oss-user-agent:{cls._oss_user_agent()}\n"
            f"/{bucket}/{obj_key}?partNumber={part_number}&uploadId={upload_id}"
        )

    @staticmethod
    def _hash_file(file_path: Path) -> tuple[str, str, str]:
        md5_hash = hashlib.md5()
        sha1_hash = hashlib.sha1()
        sha256_hash = hashlib.sha256()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                md5_hash.update(chunk)
                sha1_hash.update(chunk)
                sha256_hash.update(chunk)
        return md5_hash.hexdigest(), sha1_hash.hexdigest(), sha256_hash.hexdigest()

    @staticmethod
    def _sha1_hash_context(file_path: Path, prefix_size: int) -> str:
        h0 = 0x67452301
        h1 = 0xEFCDAB89
        h2 = 0x98BADCFE
        h3 = 0x10325476
        h4 = 0xC3D2E1F0
        with file_path.open("rb") as handle:
            data = handle.read(prefix_size)
        for offset in range(0, len(data) - (len(data) % 64), 64):
            words = [
                int.from_bytes(data[offset + index : offset + index + 4], "big")
                for index in range(0, 64, 4)
            ]
            for index in range(16, 80):
                value = words[index - 3] ^ words[index - 8] ^ words[index - 14] ^ words[index - 16]
                words.append(((value << 1) | (value >> 31)) & 0xFFFFFFFF)
            a, b, c, d, e = h0, h1, h2, h3, h4
            for index in range(80):
                if index < 20:
                    function = (b & c) | ((~b) & d)
                    constant = 0x5A827999
                elif index < 40:
                    function = b ^ c ^ d
                    constant = 0x6ED9EBA1
                elif index < 60:
                    function = (b & c) | (b & d) | (c & d)
                    constant = 0x8F1BBCDC
                else:
                    function = b ^ c ^ d
                    constant = 0xCA62C1D6
                temporary = (
                    (((a << 5) | (a >> 27)) & 0xFFFFFFFF)
                    + function
                    + e
                    + constant
                    + words[index]
                ) & 0xFFFFFFFF
                e = d
                d = c
                c = ((b << 30) | (b >> 2)) & 0xFFFFFFFF
                b = a
                a = temporary
            h0 = (h0 + a) & 0xFFFFFFFF
            h1 = (h1 + b) & 0xFFFFFFFF
            h2 = (h2 + c) & 0xFFFFFFFF
            h3 = (h3 + d) & 0xFFFFFFFF
            h4 = (h4 + e) & 0xFFFFFFFF
        context = {
            "hash_type": "sha1",
            "h0": str(h0),
            "h1": str(h1),
            "h2": str(h2),
            "h3": str(h3),
            "h4": str(h4),
            "Nl": str(prefix_size * 8),
            "Nh": "0",
            "data": "",
            "num": "0",
        }
        return base64.b64encode(
            json.dumps(context, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")


class BaiduNetdiskAdapter:
    """Minimal Baidu Netdisk open-platform upload and share flow."""

    def __init__(
        self,
        *,
        access_token: str | None = None,
        refresh_token: str | None = None,
        app_id: str | None = None,
        app_key: str | None = None,
        app_secret: str | None = None,
        remote_dir: str = "/XianyuAuto",
        period_days: int = 365,
        manual_store: Path | None = None,
        client: httpx.Client | None = None,
        share_creator: ShareCreator | None = None,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.app_id = app_id
        self.app_key = app_key
        self.app_secret = app_secret
        self.remote_dir = remote_dir.rstrip("/") or "/XianyuAuto"
        self.period_days = period_days
        self.manual_store = ManualShareStore(manual_store) if manual_store else None
        self.client = client or httpx.Client(timeout=60)
        self._owns_client = client is None
        self._share_creator = share_creator or self._mcp_share

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _refresh_access_token(self) -> None:
        if not all((self.refresh_token, self.app_key, self.app_secret)):
            raise RuntimeError("Baidu OAuth refresh credentials are incomplete")
        response = self.client.get(
            "https://openapi.baidu.com/oauth/2.0/token",
            params={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.app_key,
                "client_secret": self.app_secret,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if "access_token" not in payload:
            raise RuntimeError(f"Baidu token refresh failed: {payload}")
        self.access_token = payload["access_token"]
        self.refresh_token = payload.get("refresh_token", self.refresh_token)

    def _token(self) -> str:
        if not self.access_token:
            self._refresh_access_token()
        if not self.access_token:
            raise RuntimeError("Baidu access token unavailable")
        return self.access_token

    def share_file(
        self,
        file_path: Path,
        remote_name: str,
        *,
        share_key: str | None = None,
    ) -> ShareResult:
        share_key = share_key or remote_name
        if not share_key or "/" in share_key or share_key in {".", ".."}:
            raise RuntimeError(f"invalid Baidu share key: {share_key!r}")

        manual = self.manual_store.get_share(share_key) if self.manual_store else None
        entry = self.manual_store.get_entry(share_key) if self.manual_store else None
        if manual and entry and entry.get("last_remote_name") == remote_name:
            link, code = manual
            return ShareResult(
                link=link,
                extract_code=code,
                raw={
                    "mode": "baidu_manual",
                    "remote_name": remote_name,
                    "share_key": share_key,
                },
            )
        pending = self.manual_store.get_pending(share_key) if self.manual_store else None
        if pending and pending.get("remote_name") == remote_name:
            raise ManualShareRequired(
                share_key=share_key,
                remote_name=remote_name,
                remote_path=str(pending.get("remote_path") or ""),
                fs_id=pending.get("fs_id"),
            )

        token = self._token()
        local_size = file_path.stat().st_size
        block_md5s = []
        content_md5 = hashlib.md5()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                block_md5s.append(hashlib.md5(chunk).hexdigest())
                content_md5.update(chunk)

        folder_path = (
            f"{self.remote_dir}/{share_key}"
            if share_key != remote_name
            else self.remote_dir
        )
        folder_fs_id = (
            self._ensure_folder(token, folder_path)
            if share_key != remote_name
            else None
        )
        remote_path = f"{folder_path}/{remote_name}"

        precreate = self.client.post(
            "https://pan.baidu.com/rest/2.0/xpan/file",
            params={"method": "precreate", "access_token": token},
            data={
                "path": remote_path,
                "size": local_size,
                "isdir": 0,
                "autoinit": 1,
                "block_list": str(block_md5s).replace("'", '"'),
                "rtype": 3,
            },
        )
        precreate.raise_for_status()
        pre_payload = precreate.json()
        if int(pre_payload.get("errno", -1)) != 0:
            raise RuntimeError(f"Baidu precreate failed: {pre_payload}")
        upload_id = pre_payload.get("uploadid")
        if upload_id:
            with file_path.open("rb") as upload_handle:
                content_type = (
                    mimetypes.guess_type(file_path.name)[0]
                    or "application/octet-stream"
                )
                for partseq, _ in enumerate(block_md5s):
                    upload_handle.seek(partseq * 4 * 1024 * 1024)
                    upload = self.client.post(
                        "https://d.pcs.baidu.com/rest/2.0/pcs/superfile2",
                        params={
                            "method": "upload",
                            "access_token": token,
                            "type": "tmpfile",
                            "path": remote_path,
                            "uploadid": upload_id,
                            "partseq": partseq,
                        },
                        files={
                            "file": (
                                file_path.name,
                                upload_handle.read(4 * 1024 * 1024),
                                content_type,
                            )
                        },
                    )
                    upload.raise_for_status()
                    upload_payload = upload.json()
                    if upload_payload.get("error_code"):
                        raise RuntimeError(
                            f"Baidu upload failed at part {partseq}: {upload_payload}"
                        )

        create = self.client.post(
            "https://pan.baidu.com/rest/2.0/xpan/file",
            params={"method": "create", "access_token": token},
            data={
                "path": remote_path,
                "size": local_size,
                "isdir": 0,
                "rtype": 3,
                "uploadid": upload_id or "",
                "block_list": str(block_md5s).replace("'", '"'),
            },
        )
        create.raise_for_status()
        create_payload = create.json()
        if int(create_payload.get("errno", -1)) != 0:
            raise RuntimeError(f"Baidu create failed: {create_payload}")
        fs_id = create_payload.get("fs_id")
        if fs_id is None:
            raise RuntimeError(f"Baidu create response lacks fs_id: {create_payload}")

        if self.manual_store and manual:
            self.manual_store.mark_uploaded(share_key, remote_name=remote_name)
            link, code = manual
            return ShareResult(
                link=link,
                extract_code=code,
                raw={
                    "mode": "baidu_manual_folder",
                    "remote_name": remote_name,
                    "remote_path": remote_path,
                    "share_key": share_key,
                    "md5": content_md5.hexdigest(),
                },
            )
        pwd = uuid.uuid4().hex[:4]
        share_target = folder_fs_id if share_key != remote_name else fs_id
        if share_target is None:
            share_target = fs_id
        share_payload = self._share_creator(
            token, str(share_target), self.period_days, pwd
        )
        link = share_payload.get("link")
        if not link:
            raise RuntimeError("Baidu MCP share response lacks a link")
        extract_code = str(share_payload.get("pwd") or pwd)
        if self.manual_store:
            self.manual_store.save_share(
                share_key,
                link=str(link),
                extract_code=extract_code,
                remote_name=remote_name,
            )
        return ShareResult(
            link=str(link),
            extract_code=extract_code,
            raw={
                "precreate": pre_payload,
                "create": create_payload,
                "share": share_payload,
                "md5": content_md5.hexdigest(),
            },
        )

    def _mcp_share(
        self, token: str, fs_id: str, period_days: int, pwd: str
    ) -> dict[str, Any]:
        async def call() -> dict[str, Any]:
            from mcp import ClientSession
            from mcp.client.sse import sse_client

            url = f"https://mcp-pan.baidu.com/sse?access_token={token}"
            async with sse_client(url) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "file_sharelink_set",
                        {
                            "fsid_list": json.dumps(
                                [fs_id], separators=(",", ":")
                            ),
                            "period": period_days,
                            "pwd": pwd,
                            "remark": "闲鱼自动化交付",
                        },
                    )

            text = "".join(
                getattr(content, "text", "")
                for content in result.content
                if getattr(content, "type", None) == "text"
            )
            try:
                payload = json.loads(text)
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Baidu MCP share returned invalid JSON") from exc
            if result.isError or int(payload.get("errno", -1)) != 0:
                message = payload.get("show_msg") or "unknown error"
                raise RuntimeError(f"Baidu MCP share failed: {message}")
            data = payload.get("data") or {}
            if not data.get("link"):
                raise RuntimeError("Baidu MCP share response lacks a link")
            return {**data, "raw": payload}

        try:
            return asyncio.run(call())
        except Exception as exc:
            raise RuntimeError(
                f"Baidu MCP share failed: {str(exc).replace(token, '[REDACTED]')}"
            ) from None

    def _ensure_folder(self, token: str, folder_path: str) -> str | None:
        response = self.client.post(
            "https://pan.baidu.com/rest/2.0/xpan/file",
            params={"method": "create", "access_token": token},
            data={"path": folder_path, "isdir": 1, "rtype": 3},
        )
        response.raise_for_status()
        payload = response.json()
        errno = int(payload.get("errno", -1))
        fs_id = payload.get("fs_id")
        if errno == 0 and fs_id is not None:
            return str(fs_id)
        existing = self._find_folder_fs_id(token, folder_path)
        if existing is not None:
            return existing
        raise RuntimeError(f"Baidu folder create failed: {payload}")

    def _find_folder_fs_id(self, token: str, folder_path: str) -> str | None:
        parent, _, name = folder_path.rstrip("/").rpartition("/")
        response = self.client.get(
            "https://pan.baidu.com/rest/2.0/xpan/file",
            params={
                "method": "list",
                "access_token": token,
                "dir": parent or "/",
                "start": 0,
                "limit": 1000,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if int(payload.get("errno", -1)) != 0:
            return None
        for item in payload.get("list") or []:
            if not isinstance(item, dict):
                continue
            path = unquote(str(item.get("path") or "")).rstrip("/")
            if path == folder_path and item.get("fs_id") is not None:
                return str(item["fs_id"])
        return None

