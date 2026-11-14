from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from PIL import Image


class VisionModelError(RuntimeError):
    pass


class ImageGenerationError(RuntimeError):
    pass


@dataclass(slots=True)
class OpenAICompatibleModel:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 45

    def chat(self, system: str, user: str) -> str:
        response = httpx.post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        return payload["choices"][0]["message"]["content"]

    def health(self) -> bool:
        response = httpx.get(
            f"{self.base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=15,
        )
        return response.is_success


@dataclass(slots=True)
class OpenAICompatibleVisionModel:
    base_url: str
    api_key: str
    model: str
    fallback_model: str = "glm-4.5v"
    timeout_seconds: float = 90
    client: httpx.Client | None = None

    def _chat_url(self) -> str:
        url = self.base_url.rstrip("/")
        if url.endswith("/chat/completions"):
            return url
        return f"{url}/chat/completions"

    @staticmethod
    def _image_data_url(path: Path) -> str:
        with Image.open(path) as source:
            image = source.convert("RGB")
            image.thumbnail((960, 1280))
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=82, optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    @staticmethod
    def _extract_json(content: Any) -> dict[str, Any]:
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict)
            )
        if not isinstance(content, str) or not content.strip():
            raise VisionModelError("vision model returned empty content")
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            raise VisionModelError("vision model did not return a JSON object")
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise VisionModelError("vision model returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise VisionModelError("vision model JSON must be an object")
        return payload

    def review_revision(
        self,
        *,
        product: dict[str, Any],
        feedback: str,
        image_paths: list[Path],
    ) -> tuple[dict[str, Any], str]:
        system = (
            "你是闲鱼原创虚拟资料商品的修改审核助手。只返回一个 JSON 对象，"
            "不要 Markdown，不要解释 JSON 之外的文字。你只能建议系统可自动执行的"
            "标题、价格和详情图背景重绘；版式、页数或题目内容只能放入 notes，"
            "不得声称已经执行。若用户要求更换封面、配色、插画、背景或视觉风格，"
            "regenerate_images 必须为 true，并给出适合 GPT Image 的 image_prompt。"
            "image_prompt 只描述装饰性背景和插画，不包含商品中文文案；必须明确"
            "no people, no text, no letters, no numbers, no logos, no trademarks, "
            "no copyrighted characters。若用户没有要求改图，regenerate_images "
            "为 false 且 image_prompt 为空字符串。标题不超过30字，不写微信、二维码、"
            "外链、加V、扫码等引流内容，不作提分或效果承诺。价格必须是人民币分，"
            "范围100至1990，"
            "即 ¥1.00 至 ¥19.90，且不得高于 normal_price_cents。若用户要求低于1元，"
            "建议100分（即 ¥1.00）并说明平台门禁；price_reason 中的金额必须写元，"
            "不得把分值直接写成元。"
            "输出字段：understanding（字符串）、title（字符串）、"
            "title_reason（字符串）、price_cents（整数）、price_reason（字符串）、"
            "regenerate_images（布尔值）、image_prompt（字符串）、"
            "notes（字符串数组）、risks（字符串数组）。"
        )
        user_text = json.dumps(
            {
                "product": product,
                "owner_feedback": feedback,
                "instruction": (
                    "结合商品图和反馈给出保守、可执行的修改方案。若不需要修改某项，"
                    "保留当前值。notes 只列系统本阶段不会自动执行的内容。"
                ),
            },
            ensure_ascii=False,
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        for path in image_paths:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": self._image_data_url(path)},
                }
            )
        models = [self.model]
        if self.fallback_model and self.fallback_model != self.model:
            models.append(self.fallback_model)
        last_error: Exception | None = None
        owns_client = self.client is None
        client = self.client or httpx.Client(timeout=self.timeout_seconds)
        try:
            for model in models:
                try:
                    request_json: dict[str, Any] = {
                        "model": model,
                        "temperature": 0.1,
                        "max_tokens": 1800,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": content},
                        ],
                    }
                    if "bigmodel.cn" in self.base_url:
                        request_json["thinking"] = {"type": "disabled"}
                    response = client.post(
                        self._chat_url(),
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=request_json,
                        timeout=self.timeout_seconds,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    message = payload["choices"][0]["message"]
                    return self._extract_json(message.get("content")), model
                except (httpx.HTTPError, KeyError, IndexError, TypeError, VisionModelError) as exc:
                    last_error = exc
                    status_code = getattr(getattr(exc, "response", None), "status_code", None)
                    if status_code not in (429, 500, 502, 503, 504) and not isinstance(
                        exc, VisionModelError
                    ):
                        break
            raise VisionModelError(
                f"vision model request failed: {type(last_error).__name__}: {last_error}"
            ) from last_error
        finally:
            if owns_client:
                client.close()


@dataclass(slots=True)
class OpenAICompatibleImageGenerator:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 180
    client: httpx.Client | None = None

    def _images_url(self) -> str:
        url = self.base_url.rstrip("/")
        if url.endswith("/images/generations"):
            return url
        return f"{url}/images/generations"

    def generate(
        self,
        prompt: str,
        *,
        size: str = "1024x1536",
        quality: str = "medium",
    ) -> bytes:
        owns_client = self.client is None
        client = self.client or httpx.Client(timeout=self.timeout_seconds)
        try:
            response = client.post(
                self._images_url(),
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "size": size,
                    "quality": quality,
                    "n": 1,
                    "response_format": "b64_json",
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            item = payload["data"][0]
            encoded = item.get("b64_json")
            if encoded:
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError) as exc:
                    raise ImageGenerationError("image model returned invalid base64") from exc
            else:
                image_url = item.get("url")
                if not image_url:
                    raise ImageGenerationError("image model returned neither b64_json nor url")
                download = client.get(image_url, timeout=self.timeout_seconds)
                download.raise_for_status()
                raw = download.content
            with Image.open(io.BytesIO(raw)) as image:
                image.verify()
            return raw
        except (httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
            raise ImageGenerationError(
                f"image generation request failed: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            if owns_client:
                client.close()

