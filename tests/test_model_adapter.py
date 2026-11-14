from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import httpx
from PIL import Image

from xianyu_auto.adapters.model import (
    OpenAICompatibleImageGenerator,
    OpenAICompatibleVisionModel,
)


def test_vision_model_uses_primary_then_fallback(tmp_path: Path) -> None:
    image_path = tmp_path / "preview.png"
    Image.new("RGB", (120, 160), "#f5efe3").save(image_path)
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["model"] == "primary-vision":
            return httpx.Response(
                429,
                json={"error": {"code": "1305", "message": "busy"}},
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "understanding": "降低价格",
                                    "title": "测试字帖",
                                    "title_reason": "保持",
                                    "price_cents": 490,
                                    "price_reason": "更易决策",
                                    "notes": [],
                                    "risks": [],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    model = OpenAICompatibleVisionModel(
        base_url="https://example.com/api/paas/v4/chat/completions",
        api_key="test-key",
        model="primary-vision",
        fallback_model="fallback-vision",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        result, used_model = model.review_revision(
            product={"title": "测试字帖", "price_cents": 590},
            feedback="降价",
            image_paths=[image_path],
        )
    finally:
        assert model.client is not None
        model.client.close()

    assert result["price_cents"] == 490
    assert used_model == "fallback-vision"
    assert [call["model"] for call in calls] == ["primary-vision", "fallback-vision"]
    content = calls[1]["messages"][1]["content"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_vision_model_rejects_invalid_json(tmp_path: Path) -> None:
    image_path = tmp_path / "preview.png"
    Image.new("RGB", (20, 20), "#ffffff").save(image_path)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "not-json"}}]}
        )

    model = OpenAICompatibleVisionModel(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="only-model",
        fallback_model="only-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        try:
            model.review_revision(
                product={"title": "测试"},
                feedback="调整",
                image_paths=[image_path],
            )
        except Exception as exc:
            assert type(exc).__name__ == "VisionModelError"
        else:
            raise AssertionError("invalid JSON should fail")
    finally:
        assert model.client is not None
        model.client.close()


def test_image_generator_uses_magpie_image_endpoint() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (12, 16), "#f5efe3").save(buffer, "PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    generator = OpenAICompatibleImageGenerator(
        base_url="http://127.0.0.1:3425/v1",
        api_key="test-key",
        model="gptfree-pool/gpt-image-2.6-sunburst",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        raw = generator.generate("abstract worksheet background")
    finally:
        assert generator.client is not None
        generator.client.close()

    assert raw.startswith(b"\x89PNG")
    assert calls[0][0] == "http://127.0.0.1:3425/v1/images/generations"
    assert calls[0][1]["model"] == "gptfree-pool/gpt-image-2.6-sunburst"
    assert calls[0][1]["response_format"] == "b64_json"
