from __future__ import annotations

import io
import json
import math
import random
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from .adapters.model import OpenAICompatibleImageGenerator
from .catalog import PRODUCTS
from .checks import canonical_content_hash, sha256_file, verify_arithmetic, verify_pdf
from .models import ProductSpec


PDF_FONT_NAME = "XianyuCJK"
PDF_FONT_PATH = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
IMAGE_FONT_CANDIDATES = (
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
    Path("/System/Library/Fonts/STHeiti Medium.ttc"),
)

PAPER = "#F5F0E6"
INK = "#17201D"
COBALT = "#1557B0"
ORANGE = "#DF6B35"
MUTED = "#5D6964"
LINE = "#C9C3B7"


COMMON_CHARS = (
    "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应开它合还因由其些然前外天政四日那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第向道命此变条只没结解问意建月公无系军很情者最立代想已通并提直题党程展五果料象员革位入常文总次品式活设及管特件长求老头基资边流路级少图山统接知较将组见计别她手角期根论运农指几九区强放决西被干做必战先回则任取据处队南给色光门即保治北造百规热领七海口东导器压志世金增争济阶油思术极交受联什认六共权收证改清己美再采转更单风切打白教速花带安场身车例真务具万每目至达走积示议声报斗完类八离华名确才科张信马节话米整空元况今集温传土许步群广石记需段研界拉林律叫且究观越织装影算低持音众书布复容儿须际商非验连断深难近矿千周委素技备半办青省列习响约支般史感劳便团往酸历市克何除消构府称太准精值号率族维划选标写存候毛亲快效斯院查江型眼王按格养易置派层片始却专状育厂京识适属圆包火住调满县局照参红细引听该铁价严"
)


def _register_pdf_font() -> None:
    if PDF_FONT_NAME in pdfmetrics.getRegisteredFontNames():
        return
    if not PDF_FONT_PATH.exists():
        raise FileNotFoundError(f"Chinese font not found: {PDF_FONT_PATH}")
    pdfmetrics.registerFont(TTFont(PDF_FONT_NAME, str(PDF_FONT_PATH)))


def _clean_chars(text: str) -> list[str]:
    return list(dict.fromkeys(ch for ch in text if "\u4e00" <= ch <= "\u9fff"))


def _wrap_chars(text: str, max_chars: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        if char == "\n":
            lines.append(current)
            current = ""
            continue
        current += char
        if len(current) >= max_chars:
            lines.append(current)
            current = ""
    if current:
        lines.append(current)
    return lines


def _draw_text(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    *,
    size: float = 11,
    color: str = INK,
    max_chars: int = 40,
    leading: float = 17,
) -> float:
    c.setFont(PDF_FONT_NAME, size)
    c.setFillColor(HexColor(color))
    for line in _wrap_chars(text, max_chars):
        c.drawString(x, y, line)
        y -= leading
    return y


def _page_header(c: canvas.Canvas, title: str, page_no: int, total: int) -> None:
    width, height = A4
    c.setFillColor(HexColor(PAPER))
    c.rect(0, 0, width, height, fill=1, stroke=0)
    c.setFillColor(HexColor(COBALT))
    c.rect(0, height - 12, width, 12, fill=1, stroke=0)
    c.setFont(PDF_FONT_NAME, 8)
    c.setFillColor(HexColor(MUTED))
    c.drawString(42, height - 35, "原创可打印练习资料")
    c.drawRightString(width - 42, height - 35, f"{page_no} / {total}")
    c.setFillColor(HexColor(INK))
    c.setFont(PDF_FONT_NAME, 17)
    c.drawString(42, height - 68, title)


def _page_footer(c: canvas.Canvas) -> None:
    width, _ = A4
    c.setStrokeColor(HexColor(LINE))
    c.setLineWidth(0.5)
    c.line(42, 34, width - 42, 34)
    c.setFont(PDF_FONT_NAME, 7.5)
    c.setFillColor(HexColor(MUTED))
    c.drawString(42, 22, "电子版 · 仅供家庭学习使用 · 请勿转售")


def _cover(c: canvas.Canvas, spec: ProductSpec) -> None:
    width, height = A4
    c.setFillColor(HexColor(PAPER))
    c.rect(0, 0, width, height, fill=1, stroke=0)
    c.setFillColor(HexColor(INK))
    c.rect(0, 0, 58, height, fill=1, stroke=0)
    c.setFillColor(HexColor(ORANGE))
    c.circle(width - 76, height - 92, 28, fill=1, stroke=0)
    c.setFont(PDF_FONT_NAME, 12)
    c.setFillColor(HexColor(COBALT))
    c.drawString(92, height - 112, "ORIGINAL PRACTICE SERIES")
    c.setFillColor(HexColor(INK))
    c.setFont(PDF_FONT_NAME, 28)
    title_lines = _wrap_chars(spec.title, 12)
    y = height - 185
    for line in title_lines:
        c.drawString(92, y, line)
        y -= 42
    c.setFillColor(HexColor(MUTED))
    _draw_text(
        c,
        spec.description,
        92,
        y - 20,
        size=11,
        color=MUTED,
        max_chars=28,
        leading=20,
    )
    c.setFillColor(HexColor(INK))
    c.setFont(PDF_FONT_NAME, 11)
    c.drawString(92, 170, f"页数  {spec.pages} 页")
    c.drawString(92, 146, "纸张  A4 纵向")
    c.drawString(92, 122, "版本  原创程序排版")
    c.setStrokeColor(HexColor(LINE))
    c.line(92, 100, width - 76, 100)
    c.setFont(PDF_FONT_NAME, 8.5)
    c.drawString(92, 80, "本资料不含教材扫描件、品牌标识或第三方付费素材。")
    c.showPage()


def _grid(
    c: canvas.Canvas,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    cols: int,
    rows: int,
    stroke: str = LINE,
) -> None:
    c.setStrokeColor(HexColor(stroke))
    c.setLineWidth(0.45)
    c.rect(x, y, width, height, fill=0, stroke=1)
    for i in range(1, cols):
        c.line(x + width * i / cols, y, x + width * i / cols, y + height)
    for j in range(1, rows):
        c.line(x, y + height * j / rows, x + width, y + height * j / rows)


def _make_hanzi(spec: ProductSpec, output: Path) -> tuple[Path, dict[str, Any]]:
    pdf_path = output / "product.pdf"
    chars = _clean_chars(COMMON_CHARS)
    if len(chars) < 300:
        raise ValueError(f"need 300 unique characters, got {len(chars)}")
    chars = chars[:300]
    c = canvas.Canvas(str(pdf_path), pagesize=A4)
    c.setTitle(spec.title)
    c.setAuthor("XianyuAuto original worksheet generator")
    _cover(c, spec)

    width, height = A4
    _page_header(c, "使用说明", 2, spec.pages)
    instructions = [
        "1. 打印建议：A4 纵向，实际大小，不缩放。",
        "2. 每天练习 8 至 12 个字，先观察范字，再描红，最后在空白格独立书写。",
        "3. 每个字下方留有空白格，可重复练习，不需要一次写完。",
        "4. 家长可关注坐姿、握笔和书写速度，不把完成页数作为唯一目标。",
        "5. 本资料为通用常用字练习，不按任何教材单元编排，也不替代学校教学。",
    ]
    y = height - 120
    for line in instructions:
        y = _draw_text(c, line, 52, y, size=12, max_chars=34, leading=23) - 12
    c.setFillColor(HexColor("#FFFFFF"))
    c.setStrokeColor(HexColor(LINE))
    c.rect(52, 150, width - 104, 150, fill=1, stroke=1)
    c.setFillColor(HexColor(MUTED))
    _draw_text(
        c,
        "笔顺提示：先看范字的结构和起笔位置，按照学校示范的规范笔顺描红。"
        "本页不提供逐笔动画，家长可使用字典或学校材料核对具体笔顺。",
        68,
        268,
        size=10.5,
        color=MUTED,
        max_chars=30,
        leading=19,
    )
    _page_footer(c)
    c.showPage()

    practice_pages = spec.pages - 2
    per_page = math.ceil(len(chars) / practice_pages)
    cursor = 0
    for page in range(3, spec.pages + 1):
        chunk = chars[cursor : cursor + per_page]
        cursor += per_page
        _page_header(c, f"常用字描红  {chunk[0]} - {chunk[-1]}", page, spec.pages)
        top = height - 102
        row_height = 58
        for idx, char in enumerate(chunk):
            row = idx // 9
            col = idx % 9
            x = 45 + col * 57
            y = top - (row + 1) * row_height
            _grid(c, x, y, 52, 52, cols=4, rows=4)
            c.setFillColor(HexColor(INK))
            c.setFont(PDF_FONT_NAME, 27)
            c.drawCentredString(x + 26, y + 16, char)
            c.setFont(PDF_FONT_NAME, 6.5)
            c.setFillColor(HexColor(MUTED))
            c.drawString(x + 2, y - 10, "描红后独立书写")
        _page_footer(c)
        c.showPage()
    c.save()
    return pdf_path, {"characters": chars, "practice_pages": practice_pages}


def _arithmetic_problem(level: int, rng: random.Random) -> tuple[str, int]:
    if level == 1:
        a, b = rng.randint(1, 10), rng.randint(1, 10)
        if rng.random() < 0.5:
            return f"{a}+{b}", a + b
        high, low = max(a, b), min(a, b)
        return f"{high}-{low}", high - low
    if level == 2:
        if rng.random() < 0.5:
            a, b = rng.randint(10, 35), rng.randint(1, 14)
            return f"{a}+{b}", a + b
        a = rng.randint(20, 50)
        b = rng.randint(1, a)
        return f"{a}-{b}", a - b
    if level == 3:
        if rng.random() < 0.5:
            a = rng.randint(12, 79)
            b = rng.randint(11, 100 - a)
            return f"{a}+{b}", a + b
        a = rng.randint(35, 100)
        b = rng.randint(11, a - 1)
        return f"{a}-{b}", a - b
    if level == 4:
        a = rng.randint(20, 60)
        b = rng.randint(5, 25)
        c = rng.randint(1, 15)
        if rng.random() < 0.5:
            return f"{a}+{b}-{c}", a + b - c
        return f"{a}-{b}+{c}", a - b + c
    a = rng.randint(30, 70)
    b = rng.randint(10, 25)
    c = rng.randint(5, 20)
    if rng.random() < 0.5:
        return f"{a}-{b}+{c}", a - b + c
    return f"{a}+{b}-{c}", a + b - c


def _make_arithmetic(spec: ProductSpec, output: Path) -> tuple[Path, dict[str, Any]]:
    pdf_path = output / "product.pdf"
    rng = random.Random(20261106)
    problems: list[dict[str, Any]] = []
    seen: set[str] = set()
    problem_pages = 20
    questions_per_page = 20
    for page_index in range(problem_pages):
        level = min(5, page_index // 4 + 1)
        page_problems: list[dict[str, Any]] = []
        while len(page_problems) < questions_per_page:
            expression, answer = _arithmetic_problem(level, rng)
            if expression in seen:
                continue
            seen.add(expression)
            page_problems.append(
                {
                    "number": len(problems) + len(page_problems) + 1,
                    "level": level,
                    "page": page_index + 3,
                    "expression": expression,
                    "answer": answer,
                }
            )
        problems.extend(page_problems)
    answers = [
        {"number": item["number"], "answer": item["answer"]} for item in problems
    ]
    verify_arithmetic(problems, answers)

    c = canvas.Canvas(str(pdf_path), pagesize=A4)
    c.setTitle(spec.title)
    c.setAuthor("XianyuAuto deterministic worksheet generator")
    _cover(c, spec)
    width, height = A4
    _page_header(c, "使用说明与难度说明", 2, spec.pages)
    level_text = [
        ("第 1 级", "20 以内加减法，建立基本熟练度"),
        ("第 2 级", "50 以内加减法，练习退位与进位"),
        ("第 3 级", "100 以内加减法，逐步提高准确率"),
        ("第 4 级", "三个数的连续加减，保持步骤清晰"),
        ("第 5 级", "100 以内混合运算，建议先估算再计算"),
    ]
    y = height - 116
    for level, text in level_text:
        c.setFillColor(HexColor(COBALT))
        c.setFont(PDF_FONT_NAME, 12)
        c.drawString(52, y, level)
        c.setFillColor(HexColor(INK))
        c.setFont(PDF_FONT_NAME, 10.5)
        c.drawString(130, y, text)
        y -= 31
    _draw_text(
        c,
        "建议每次完成 1 页，限时不是必须。答案集中放在最后 8 页，方便家长核对。"
        "题目和答案均由程序按固定规则生成，并通过独立重算校验。",
        52,
        400,
        size=11,
        color=MUTED,
        max_chars=34,
        leading=20,
    )
    _page_footer(c)
    c.showPage()

    for page_index in range(problem_pages):
        page_no = page_index + 3
        page_problems = problems[
            page_index * questions_per_page : (page_index + 1) * questions_per_page
        ]
        _page_header(c, f"第 {page_problems[0]['level']} 级  口算练习", page_no, spec.pages)
        c.setFont(PDF_FONT_NAME, 8.5)
        c.setFillColor(HexColor(MUTED))
        c.drawString(44, height - 92, "姓名 __________    日期 __________    用时 ______ 分")
        for idx, problem in enumerate(page_problems):
            col = idx % 2
            row = idx // 2
            x = 54 + col * 253
            y = height - 130 - row * 48
            c.setFillColor(HexColor(INK))
            c.setFont(PDF_FONT_NAME, 14)
            c.drawRightString(x + 28, y, f"{problem['number']}.")
            c.setFont(PDF_FONT_NAME, 16)
            c.drawString(x + 44, y, problem["expression"])
            c.drawString(x + 164, y, "=")
            c.setStrokeColor(HexColor(LINE))
            c.line(x + 182, y - 2, x + 228, y - 2)
        _page_footer(c)
        c.showPage()

    answer_pages = 8
    per_answer_page = math.ceil(len(answers) / answer_pages)
    for page_offset in range(answer_pages):
        page_no = 23 + page_offset
        chunk = answers[
            page_offset * per_answer_page : (page_offset + 1) * per_answer_page
        ]
        if not chunk:
            chunk = []
        _page_header(c, "独立答案页", page_no, spec.pages)
        c.setFont(PDF_FONT_NAME, 8.5)
        c.setFillColor(HexColor(MUTED))
        c.drawString(44, height - 93, "家长核对页 · 请与题目页分开打印或折叠保存")
        for idx, answer in enumerate(chunk):
            col = idx % 5
            row = idx // 5
            x = 44 + col * 102
            y = height - 122 - row * 35
            c.setFont(PDF_FONT_NAME, 10)
            c.setFillColor(HexColor(INK))
            c.drawString(x, y, f"第{answer['number']}题 = {answer['answer']}")
        _page_footer(c)
        c.showPage()
    c.save()
    (output / "problems.json").write_text(
        json.dumps(problems, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "answers.json").write_text(
        json.dumps(answers, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return pdf_path, {"problems": problems, "answers": answers}


def _make_preschool(spec: ProductSpec, output: Path) -> tuple[Path, dict[str, Any]]:
    pdf_path = output / "product.pdf"
    c = canvas.Canvas(str(pdf_path), pagesize=A4)
    c.setTitle(spec.title)
    c.setAuthor("XianyuAuto original preschool worksheet generator")
    width, height = A4
    days = [
        ("认识笔画", "横、竖、撇、点", "描画基本笔画", "找到图中 5 个圆形"),
        ("拼音 a o e", "听读与描红", "跟读四声", "按顺序圈出 a o e"),
        ("数字 1 至 5", "数一数再写", "描数字并数物", "找出数量为 3 的图"),
        ("方向与位置", "上下左右", "画出向左的箭头", "从迷宫中找到出口"),
        ("拼音 i u ü", "观察口形", "描红并读一读", "圈出相同的字母"),
        ("数字 6 至 10", "数量对应", "按数量画圈", "补全缺少的圆点"),
        ("比较大小", "多与少", "在较大一组旁打勾", "比较两组图形"),
        ("拼音 b p m f", "认读声母", "描红并连线", "找出不同项"),
        ("十以内加法", "合并数量", "用圆点表示算式", "找出和为 7 的一组"),
        ("十以内减法", "拿走多少", "画掉相应数量", "补全数量变化"),
        ("观察与记忆", "看图 20 秒", "回忆图中物品", "记下缺少的物品"),
        ("规律与排序", "颜色和形状", "继续排列图形", "找出下一项"),
        ("表达与倾听", "说完整句子", "讲一讲今天的练习", "听指令做动作"),
        ("综合闯关", "复习本周内容", "完成小挑战", "和家长一起检查"),
    ]
    for day_index, (theme, focus, practice, attention) in enumerate(days, start=1):
        _page_header(c, f"第 {day_index} 天  {theme}", day_index, spec.pages)
        c.setFillColor(HexColor(MUTED))
        c.setFont(PDF_FONT_NAME, 9.5)
        c.drawString(44, height - 91, f"今日重点：{focus}")

        c.setFillColor(HexColor("#FFFFFF"))
        c.setStrokeColor(HexColor(LINE))
        c.rect(44, height - 285, width - 88, 160, fill=1, stroke=1)
        c.setFillColor(HexColor(COBALT))
        c.setFont(PDF_FONT_NAME, 10)
        c.drawString(60, height - 147, "拼音 / 数字描红区")
        _grid(c, 60, height - 273, width - 120, 100, cols=6, rows=3)
        if "笔画" in theme:
            sample = "一  丨  丿  丶"
        elif "拼音" in theme or "字母" in theme:
            sample = "a  o  e  i  u  ü"
        elif "方向" in theme:
            sample = "上  下  左  右"
        elif "比较" in theme:
            sample = "3  >  2     2  <  5"
        elif "加法" in theme:
            sample = "3 + 2 = 5"
        elif "减法" in theme:
            sample = "5 - 2 = 3"
        elif "规律" in theme:
            sample = "●  ■  ▲  ●  ■"
        elif "数字 6" in theme:
            sample = "6  7  8  9  10"
        elif "数字 1" in theme:
            sample = "1  2  3  4  5"
        else:
            sample = "读  看  想  说"
        c.setFillColor(HexColor(INK))
        c.setFont(PDF_FONT_NAME, 24)
        c.drawCentredString(width / 2, height - 239, sample)

        c.setFillColor(HexColor("#FFFFFF"))
        c.setStrokeColor(HexColor(LINE))
        c.rect(44, height - 470, width - 88, 160, fill=1, stroke=1)
        c.setFillColor(HexColor(ORANGE))
        c.setFont(PDF_FONT_NAME, 10)
        c.drawString(60, height - 334, "专注力小任务")
        c.setFillColor(HexColor(INK))
        _draw_text(
            c,
            attention,
            60,
            height - 365,
            size=13,
            max_chars=22,
            leading=22,
        )
        if "圆形" in attention or "圆点" in attention or "数量" in attention:
            for i in range(12):
                x = 72 + (i % 6) * 82
                y = height - 424 + (i // 6) * 45
                c.setFillColor(HexColor(COBALT if i % 3 else ORANGE))
                c.circle(x, y, 9 if i % 2 else 13, fill=1, stroke=0)
        else:
            if "拼音" in theme or "字母" in theme:
                glyph_line = "a   o   e      b   p   m   f"
            elif "规律" in theme:
                glyph_line = "●   ■   ▲   ●   ■   ?"
            elif "比较" in theme:
                glyph_line = "●●●       ●●       ●●●●"
            else:
                glyph_line = "看    听    说    做"
            c.setFillColor(HexColor(INK))
            c.setFont(PDF_FONT_NAME, 20)
            c.drawCentredString(width / 2, height - 398, glyph_line)

        c.setFillColor(HexColor("#FFFFFF"))
        c.setStrokeColor(HexColor(LINE))
        c.rect(44, 76, width - 88, 230, fill=1, stroke=1)
        c.setFillColor(HexColor(COBALT))
        c.setFont(PDF_FONT_NAME, 10)
        c.drawString(60, 282, "今日练习")
        c.setFillColor(HexColor(INK))
        _draw_text(
            c,
            practice,
            60,
            250,
            size=12,
            max_chars=28,
            leading=21,
        )
        _draw_text(
            c,
            "完成方式：读一遍、描一遍、独立做一遍。遇到不会的内容先做标记，"
            "和家长一起讨论，不追求一次全对。",
            60,
            180,
            size=10,
            color=MUTED,
            max_chars=34,
            leading=19,
        )
        _page_footer(c)
        c.showPage()
    c.save()
    return pdf_path, {"days": days, "day_count": len(days)}


def _image_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in IMAGE_FONT_CANDIDATES:
        if candidate.exists():
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except OSError:
                continue
    raise FileNotFoundError("No usable Chinese font for image generation")


def _gradient_background(width: int, height: int) -> Image.Image:
    image = Image.new("RGB", (width, height), PAPER)
    pixels = image.load()
    for y in range(height):
        ratio = y / max(height - 1, 1)
        # A warm paper-to-sage wash keeps the listing images distinct from flat marketplace templates.
        r = int(245 - 14 * ratio)
        g = int(240 - 8 * ratio)
        b = int(230 - 13 * ratio)
        for x in range(width):
            pixels[x, y] = (r, g, b)
    return image


def _image_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    size: int,
    color: str = INK,
    max_width: int = 900,
    line_gap: int = 18,
) -> int:
    font = _image_font(size)
    max_chars = max(1, max_width // size)
    lines = _wrap_chars(text, max_chars)
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=font, fill=color)
        y += size + line_gap
    return y


def _generated_detail_background(
    spec: ProductSpec,
    image_generator: OpenAICompatibleImageGenerator,
    custom_prompt: str | None = None,
) -> Image.Image:
    kind = (spec.metadata or {}).get("kind")
    theme = {
        "hanzi_tracing": "汉字书写、描红练习格与铅笔",
        "arithmetic": "数字卡片、口算练习与几何图形",
        "preschool_daily": "幼儿启蒙、拼音卡片与专注力游戏",
    }.get(str(kind), "儿童学习与可打印练习资料")
    default_prompt = (
        "Original premium editorial illustration for a Chinese educational "
        f"printable worksheet product listing. Theme: {theme}. "
        "Warm ivory paper texture, cobalt blue and orange accents, clean "
        "paper-cut geometric composition, calm stationery aesthetic, ample "
        "negative space, suitable as a subtle background behind product copy. "
        "Vertical 3:4 composition. No people, no faces, no hands, no text, "
        "no letters, no numbers, no logos, no trademarks, no copyrighted "
        "characters, no watermark, no device mockup."
    )
    prompt = (
        f"{custom_prompt.strip()} {default_prompt}"
        if custom_prompt and custom_prompt.strip()
        else default_prompt
    )
    raw = image_generator.generate(prompt, size="1024x1536", quality="medium")
    with Image.open(io.BytesIO(raw)) as source:
        generated = ImageOps.fit(
            source.convert("RGB"),
            (1080, 1440),
            method=Image.Resampling.LANCZOS,
        ).convert("RGBA")
    generated.putalpha(76)
    return Image.alpha_composite(
        _gradient_background(1080, 1440).convert("RGBA"),
        generated,
    ).convert("RGB")


def _detail_images(
    spec: ProductSpec,
    output: Path,
    image_generator: OpenAICompatibleImageGenerator | None = None,
    image_prompt: str | None = None,
    require_generated_image: bool = False,
) -> tuple[list[Path], dict[str, Any]]:
    image_dir = output / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    width, height = 1080, 1440
    generation: dict[str, Any] = {
        "enabled": image_generator is not None,
        "model": image_generator.model if image_generator else None,
        "generated": 0,
        "fallback": False,
    }
    generated_background = None
    if image_generator is not None:
        try:
            generated_background = _generated_detail_background(
                spec, image_generator, image_prompt
            )
            generation["generated"] = 1
        except Exception as exc:
            if require_generated_image:
                raise
            generation["fallback"] = True
            generation["fallback_reason"] = type(exc).__name__
    labels = [
        ("01", "封面", spec.title, "A4 原创可打印练习资料"),
        ("02", "内容预览", f"{spec.pages} 页完整内容", "封面、说明页、练习页与独立答案页完整呈现"),
        ("03", "适用场景", "家庭打印练习", "按孩子当前水平选择难度，不替代学校教学"),
        ("04", "使用建议", "每天少量练习", "先读说明，再完成当页，最后核对答案"),
        ("05", "交付说明", "电子版自动发货", "拍下后发送网盘链接与提取码，不邮寄"),
        ("06", "版权声明", "原创排版资料", "不含教材扫描件、品牌标识或第三方付费素材"),
    ]
    paths: list[Path] = []
    for index, (number, section, headline, body) in enumerate(labels, start=1):
        image = (
            generated_background.copy()
            if generated_background is not None
            else _gradient_background(width, height)
        )
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 24, height), fill=INK)
        draw.ellipse((width - 210, 88, width - 70, 228), fill=ORANGE)
        draw.ellipse((86, 110, 160, 184), outline=COBALT, width=8)
        draw.text((110, 116), number, font=_image_font(30), fill=COBALT)
        draw.text((210, 112), section, font=_image_font(24), fill=COBALT)
        y = _image_text(
            draw,
            (88, 270),
            headline,
            size=62,
            color=INK,
            max_width=820,
            line_gap=18,
        )
        y = _image_text(
            draw,
            (92, y + 26),
            body,
            size=28,
            color=MUTED,
            max_width=820,
            line_gap=15,
        )

        card_top = max(y + 62, 760)
        draw.rounded_rectangle(
            (84, card_top, width - 84, height - 170),
            radius=34,
            fill="#FFFFFF",
            outline=LINE,
            width=3,
        )
        if index in (1, 2):
            draw.text((128, card_top + 46), "页面样例", font=_image_font(28), fill=INK)
            for p in range(3):
                left = 142 + p * 250
                top = card_top + 120
                draw.rounded_rectangle(
                    (left, top, left + 208, top + 300),
                    radius=10,
                    fill="#FBF8F2",
                    outline=LINE,
                    width=2,
                )
                draw.text(
                    (left + 20, top + 20),
                    f"PAGE {p + 1}",
                    font=_image_font(16),
                    fill=COBALT,
                )
                kind = (spec.metadata or {}).get("kind")
                if kind == "hanzi_tracing":
                    for row in range(4):
                        for col in range(2):
                            gx = left + 22 + col * 88
                            gy = top + 62 + row * 55
                            draw.rectangle(
                                (gx, gy, gx + 70, gy + 46),
                                outline="#BDB6A9",
                                width=1,
                            )
                            draw.text(
                                (gx + 18, gy + 7),
                                "永" if p == 0 else ("和" if p == 1 else "春"),
                                font=_image_font(22),
                                fill=INK,
                            )
                elif kind == "arithmetic":
                    lines = (
                        ("1. 12+5 = ___", "2. 9-3 = ___"),
                        ("1. 38+21 = ___", "2. 64-18 = ___"),
                        ("1. 46+27-8 = ___", "2. 72-35+6 = ___"),
                    )[p]
                    for row, line in enumerate(lines):
                        draw.text(
                            (left + 18, top + 76 + row * 92),
                            line,
                            font=_image_font(17),
                            fill=INK,
                        )
                else:
                    draw.text(
                        (left + 18, top + 70),
                        "第 1 天",
                        font=_image_font(18),
                        fill=COBALT,
                    )
                    draw.rectangle(
                        (left + 18, top + 105, left + 190, top + 170),
                        outline="#BDB6A9",
                        width=1,
                    )
                    draw.text(
                        (left + 34, top + 122),
                        "1  2  3  4  5",
                        font=_image_font(18),
                        fill=INK,
                    )
                    draw.text(
                        (left + 18, top + 205),
                        "专注力任务",
                        font=_image_font(17),
                        fill=ORANGE,
                    )
        elif index == 3:
            bullets = ["准备 A4 纸和打印机", "根据孩子水平选择练习", "每次 10 至 20 分钟", "完成后一起核对"]
            for i, item in enumerate(bullets):
                draw.ellipse(
                    (132, card_top + 105 + i * 105, 162, card_top + 135 + i * 105),
                    fill=COBALT if i % 2 == 0 else ORANGE,
                )
                draw.text(
                    (192, card_top + 96 + i * 105),
                    item,
                    font=_image_font(28),
                    fill=INK,
                )
        elif index == 4:
            steps = [
                ("01", "阅读说明"),
                ("02", "完成当页"),
                ("03", "独立核对"),
                ("04", "标记复习"),
            ]
            for i, (step, text) in enumerate(steps):
                top = card_top + 90 + i * 112
                draw.text((132, top), step, font=_image_font(32), fill=ORANGE)
                draw.text((235, top), text, font=_image_font(30), fill=INK)
                draw.line((132, top + 65, 930, top + 65), fill=LINE, width=2)
        elif index == 5:
            draw.text((132, card_top + 70), "交付内容", font=_image_font(30), fill=INK)
            details = [
                "1 份 A4 可打印 PDF",
                "1 个网盘分享链接",
                "1 个提取码",
                "使用说明随卡密发送",
            ]
            for i, item in enumerate(details):
                draw.text(
                    (150, card_top + 150 + i * 82),
                    f"+  {item}",
                    font=_image_font(27),
                    fill=MUTED,
                )
        else:
            draw.text((132, card_top + 70), "使用边界", font=_image_font(30), fill=INK)
            rights = [
                "原创程序排版与生成",
                "不含教材扫描页",
                "不含品牌与第三方图片",
                "仅限购买家庭学习使用",
                "请勿转售或公开传播",
            ]
            for i, item in enumerate(rights):
                draw.text(
                    (150, card_top + 148 + i * 68),
                    f"·  {item}",
                    font=_image_font(26),
                    fill=MUTED,
                )

        draw.line((88, height - 108, width - 88, height - 108), fill=LINE, width=2)
        draw.text(
            (90, height - 80),
            "XianyuAuto · 原创可打印资料",
            font=_image_font(22),
            fill=MUTED,
        )
        path = image_dir / f"{index:02d}-{section}.png"
        image.save(path, "PNG", optimize=True)
        paths.append(path)
    return paths, generation


def build_product(
    spec: ProductSpec,
    output_root: str | Path,
    image_generator: OpenAICompatibleImageGenerator | None = None,
    image_prompt: str | None = None,
    require_generated_image: bool = False,
) -> dict[str, Any]:
    _register_pdf_font()
    output = Path(output_root) / spec.slug
    output.mkdir(parents=True, exist_ok=True)
    if spec.metadata and spec.metadata.get("kind") == "hanzi_tracing":
        pdf_path, generation_meta = _make_hanzi(spec, output)
    elif spec.metadata and spec.metadata.get("kind") == "arithmetic":
        pdf_path, generation_meta = _make_arithmetic(spec, output)
    else:
        pdf_path, generation_meta = _make_preschool(spec, output)
    pdf_check = verify_pdf(pdf_path, spec.pages)
    images, image_generation = _detail_images(
        spec,
        output,
        image_generator,
        image_prompt,
        require_generated_image,
    )
    assets = [
        {
            "kind": "pdf",
            "path": str(pdf_path),
            "sha256": sha256_file(pdf_path),
            "metadata": pdf_check,
        }
    ]
    assets.extend(
        {
            "kind": "image",
            "path": str(path),
            "sha256": sha256_file(path),
            "metadata": {"width": 1080, "height": 1440},
        }
        for path in images
    )
    content_hash = canonical_content_hash(
        title=spec.title,
        description=spec.description,
        category=spec.category,
        price_cents=spec.price_cents,
        normal_price_cents=spec.normal_price_cents,
        pages=spec.pages,
        asset_hashes=[asset["sha256"] for asset in assets],
    )
    manifest = {
        "slug": spec.slug,
        "title": spec.title,
        "description": spec.description,
        "category": spec.category,
        "price_cents": spec.price_cents,
        "normal_price_cents": spec.normal_price_cents,
        "pages": spec.pages,
        "risk_flags": list(spec.risk_flags),
        "metadata": spec.metadata or {},
        "content_hash": content_hash,
        "pdf_check": pdf_check,
        "generation": {
            **{
                k: v
                for k, v in generation_meta.items()
                if k not in {"problems", "answers"}
            },
            "image_generation": image_generation,
        },
        "assets": assets,
        "preview_path": str(images[0]),
    }
    (output / "product.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def build_all(
    output_root: str | Path,
    image_generator: OpenAICompatibleImageGenerator | None = None,
) -> list[dict[str, Any]]:
    return [
        build_product(spec, output_root, image_generator=image_generator)
        for spec in PRODUCTS
    ]

