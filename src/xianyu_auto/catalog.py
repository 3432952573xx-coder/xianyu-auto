from __future__ import annotations

from .models import ProductSpec


PRODUCTS: tuple[ProductSpec, ...] = (
    ProductSpec(
        slug="hanzi-tracing-300",
        title="小学常用300字描红字帖",
        description=(
            "电子版原创可打印字帖，共20页。包含300个常用字描红与空白练习格，"
            "A4排版。拍下后自动发送网盘链接，不邮寄。"
        ),
        category="K12 可打印练习资料",
        price_cents=590,
        normal_price_cents=990,
        pages=20,
        risk_flags=("需家长陪同下使用", "不含教材原文"),
        metadata={"kind": "hanzi_tracing", "character_count": 300, "paper": "A4"},
    ),
    ProductSpec(
        slug="math-100-oral-cards",
        title="100以内加减法口算题卡",
        description=(
            "电子版原创口算练习，共30页，5级难度，题目与独立答案页由程序生成并复核。"
            "A4排版。拍下后自动发送网盘链接，不邮寄。"
        ),
        category="K12 可打印练习资料",
        price_cents=390,
        normal_price_cents=690,
        pages=30,
        risk_flags=("题目难度需按孩子情况选择", "不含教材原文"),
        metadata={
            "kind": "arithmetic",
            "levels": 5,
            "problem_count": 400,
            "paper": "A4",
        },
    ),
    ProductSpec(
        slug="preschool-daily-14",
        title="幼小衔接每日一练14天",
        description=(
            "电子版原创幼小衔接练习，共14页，覆盖拼音、数字与专注力活动。"
            "A4排版。拍下后自动发送网盘链接，不邮寄。"
        ),
        category="K12 可打印练习资料",
        price_cents=990,
        normal_price_cents=1290,
        pages=14,
        risk_flags=("需家长陪同下使用", "不含教材原文"),
        metadata={"kind": "preschool_daily", "days": 14, "paper": "A4"},
    ),
)


def product_by_slug(slug: str) -> ProductSpec:
    for product in PRODUCTS:
        if product.slug == slug:
            return product
    raise KeyError(slug)

