from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from pypdf import PdfReader


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_content_hash(
    *,
    title: str,
    description: str,
    category: str,
    price_cents: int,
    normal_price_cents: int,
    pages: int,
    asset_hashes: Iterable[str],
) -> str:
    payload = {
        "title": re.sub(r"\s+", " ", title).strip(),
        "description": re.sub(r"\s+", " ", description).strip(),
        "category": category.strip(),
        "price_cents": price_cents,
        "normal_price_cents": normal_price_cents,
        "pages": pages,
        "asset_hashes": sorted(asset_hashes),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def verify_pdf(path: str | Path, expected_pages: int) -> dict[str, Any]:
    reader = PdfReader(str(path))
    if len(reader.pages) != expected_pages:
        raise ValueError(
            f"{path}: expected {expected_pages} pages, got {len(reader.pages)}"
        )
    embedded_fonts: set[str] = set()
    for page in reader.pages:
        resources = page.get("/Resources")
        if not resources:
            continue
        fonts = resources.get("/Font")
        if not fonts:
            continue
        for _, font_ref in fonts.items():
            font = font_ref.get_object()
            base_font = str(font.get("/BaseFont", ""))
            descriptor = font.get("/FontDescriptor")
            if descriptor is None and font.get("/DescendantFonts"):
                descendant = font["/DescendantFonts"][0].get_object()
                descriptor = descendant.get("/FontDescriptor")
            if descriptor is not None:
                descriptor = descriptor.get_object()
                if any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")):
                    embedded_fonts.add(base_font)
    if not embedded_fonts:
        raise ValueError(f"{path}: no embedded fonts detected")
    text_chars = sum(len((page.extract_text() or "").strip()) for page in reader.pages)
    if text_chars < expected_pages * 8:
        raise ValueError(f"{path}: unusually low extractable text count")
    return {
        "pages": len(reader.pages),
        "embedded_fonts": sorted(embedded_fonts),
        "extractable_chars": text_chars,
        "size_bytes": Path(path).stat().st_size,
    }


def verify_arithmetic(
    problems: list[dict[str, Any]], answers: list[dict[str, Any]]
) -> None:
    if len(problems) != len(answers):
        raise ValueError("problem and answer counts differ")
    seen: set[str] = set()
    for problem, answer_row in zip(problems, answers, strict=True):
        expression = problem["expression"]
        if expression in seen:
            raise ValueError(f"duplicate expression: {expression}")
        seen.add(expression)
        match = re.fullmatch(r"(\d+)([+\-])(\d+)(?:([+\-])(\d+))?", expression)
        if not match:
            raise ValueError(f"invalid expression: {expression}")
        first, op, second, op3, third = match.groups()
        value = int(first) + int(second) if op == "+" else int(first) - int(second)
        if op3:
            value = value + int(third) if op3 == "+" else value - int(third)
        if value != answer_row["answer"]:
            raise ValueError(f"answer mismatch for {expression}")
        if value < 0 or value > 100:
            raise ValueError(f"out-of-range answer for {expression}: {value}")

