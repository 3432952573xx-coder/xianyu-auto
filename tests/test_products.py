from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from xianyu_auto.checks import verify_arithmetic, verify_pdf
from xianyu_auto.catalog import PRODUCTS
from xianyu_auto.config import Settings
from xianyu_auto.products import build_all, build_product


def test_three_products_have_expected_pages_images_and_answers(tmp_path: Path) -> None:
    manifests = build_all(tmp_path)
    assert len(manifests) == 3
    expected_pages = {
        "hanzi-tracing-300": 20,
        "math-100-oral-cards": 30,
        "preschool-daily-14": 14,
    }
    for manifest in manifests:
        pdf_path = next(
            asset["path"] for asset in manifest["assets"] if asset["kind"] == "pdf"
        )
        assert len(PdfReader(pdf_path).pages) == expected_pages[manifest["slug"]]
        check = verify_pdf(pdf_path, expected_pages[manifest["slug"]])
        assert check["embedded_fonts"]
        images = [
            asset["path"] for asset in manifest["assets"] if asset["kind"] == "image"
        ]
        assert len(images) == 6
        for image_path in images:
            with Image.open(image_path) as image:
                assert image.size == (1080, 1440)

    arithmetic_dir = tmp_path / "math-100-oral-cards"
    problems = json.loads((arithmetic_dir / "problems.json").read_text(encoding="utf-8"))
    answers = json.loads((arithmetic_dir / "answers.json").read_text(encoding="utf-8"))
    assert len(problems) == 400
    assert len(answers) == 400
    verify_arithmetic(problems, answers)


def test_default_database_path_is_stable(tmp_path: Path) -> None:
    settings = Settings.load(tmp_path)
    assert settings.database_path == tmp_path / "data" / "xianyu_auto.db"


def test_product_images_use_generated_background(tmp_path: Path) -> None:
    class FakeImageGenerator:
        model = "fake-image-model"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def generate(self, prompt: str, **_: object) -> bytes:
            self.calls.append(prompt)
            buffer = io.BytesIO()
            Image.new("RGB", (1024, 1536), "#1557b0").save(buffer, "PNG")
            return buffer.getvalue()

    generator = FakeImageGenerator()
    manifest = build_product(
        PRODUCTS[0],
        tmp_path,
        image_generator=generator,  # type: ignore[arg-type]
    )

    assert len(generator.calls) == 1
    assert manifest["generation"]["image_generation"] == {
        "enabled": True,
        "model": "fake-image-model",
        "generated": 1,
        "fallback": False,
    }

