from __future__ import annotations

import json
import math
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote


KEYWORDS = (
    "小学字帖打印",
    "儿童口算题卡",
    "幼小衔接练习",
    "一年级拼音练习",
    "二年级口算",
    "幼儿控笔训练",
    "小学专注力训练",
    "数字描红",
    "拼音描红",
    "看图写话模板",
    "小学数学错题本",
    "乘法口诀练习",
    "英语字母描红",
    "儿童时间管理表",
    "小学阅读记录卡",
    "幼儿迷宫打印",
    "儿童涂色打印",
    "一年级生字练习",
    "二年级看图写话",
    "小学暑假计划表",
    "儿童奖励贴纸",
    "家庭打印练习",
    "幼儿数字启蒙",
    "小学硬笔练习",
    "幼儿园晨间活动",
    "儿童家务清单",
    "小学作文格子纸",
    "儿童情绪记录表",
    "小学晨读打卡",
    "幼儿连线练习",
)


class SearchBlocked(RuntimeError):
    pass


class KeywordSearchPort(Protocol):
    def search(self, keyword: str) -> list[dict[str, Any]]: ...


@dataclass(slots=True)
class BrowserKeywordSearch:
    profile_dir: Path
    headless: bool = True

    def search(self, keyword: str) -> list[dict[str, Any]]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed; install it and Chromium before live selection"
            ) from exc
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                viewport={"width": 1365, "height": 900},
                locale="zh-CN",
            )
            try:
                page = context.new_page()
                page.goto(
                    f"https://www.goofish.com/search?q={quote(keyword)}",
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                page.wait_for_timeout(2500)
                body = page.locator("body").inner_text(timeout=10000)
                if any(
                    marker in body
                    for marker in (
                        "验证码",
                        "滑动验证",
                        "请登录",
                        "登录后查看",
                        "非法访问",
                        "正常浏览器访问",
                    )
                ):
                    raise SearchBlocked(
                        f"public search hit login or verification wall for {keyword}"
                    )
                cards = page.locator('a[href*="item"], a[href*="detail"]')
                rows: list[dict[str, Any]] = []
                for index in range(min(cards.count(), 60)):
                    text = cards.nth(index).inner_text().strip().replace("\n", " ")
                    if not text:
                        continue
                    prices = [
                        float(value)
                        for value in re.findall(r"[¥￥]\s*(\d+(?:\.\d{1,2})?)", text)
                    ]
                    wants_match = re.search(r"(\d+)\s*人想要", text)
                    if prices:
                        rows.append(
                            {
                                "title": re.sub(r"[¥￥]\s*\d+(?:\.\d{1,2})?", "", text).strip(),
                                "price": prices[0],
                                "wants": int(wants_match.group(1)) if wants_match else 0,
                            }
                        )
                return rows
            finally:
                context.close()


@dataclass(slots=True)
class SelectionResult:
    keyword: str
    score: float
    selected_slug: str
    sample_count: int
    price_p25: float | None
    price_p50: float | None
    price_p75: float | None
    title_similarity: float
    risk_penalty: float
    reasons: list[str] = field(default_factory=list)


def _percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * ratio
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text)
    return {compact[i : i + 2] for i in range(max(0, len(compact) - 1))}


def _title_similarity(titles: list[str]) -> float:
    grams = [_bigrams(title) for title in titles if title]
    if len(grams) < 2:
        return 0.0
    values = []
    for index, left in enumerate(grams):
        for right in grams[index + 1 :]:
            union = left | right
            values.append(len(left & right) / len(union) if union else 0)
    return statistics.fmean(values) if values else 0.0


def score_rows(
    keyword: str,
    rows: list[dict[str, Any]],
    *,
    target_price: float = 9.9,
    feasibility: float = 0.9,
    risk_penalty: float = 0.0,
) -> SelectionResult:
    if not rows:
        return SelectionResult(
            keyword=keyword,
            score=0.0,
            selected_slug="hanzi-tracing-300",
            sample_count=0,
            price_p25=None,
            price_p50=None,
            price_p75=None,
            title_similarity=0.0,
            risk_penalty=risk_penalty,
            reasons=["没有可用公开样本，使用保底产品"],
        )
    prices = [float(row["price"]) for row in rows if row.get("price") is not None]
    wants = [int(row.get("wants", 0)) for row in rows]
    p25 = _percentile(prices, 0.25) if prices else None
    p50 = _percentile(prices, 0.50) if prices else None
    p75 = _percentile(prices, 0.75) if prices else None
    demand_raw = statistics.fmean(wants) if wants else 0.0
    demand = min(1.0, math.log1p(demand_raw) / math.log1p(200))
    similarity = _title_similarity([str(row.get("title", "")) for row in rows])
    competition_gap = 1.0 - similarity
    price_fit = 0.5
    if p50 and p50 > 0:
        price_fit = max(0.0, 1.0 - abs(target_price - p50) / max(target_price, p50))
    score = max(
        0.0,
        min(
            1.0,
            0.35 * demand
            + 0.25 * competition_gap
            + 0.20 * price_fit
            + 0.20 * feasibility
            - risk_penalty,
        ),
    )
    if "字帖" in keyword or "控笔" in keyword or "生字" in keyword:
        slug = "hanzi-tracing-300"
    elif any(word in keyword for word in ("口算", "加减", "乘法", "数学")):
        slug = "math-100-oral-cards"
    else:
        slug = "preschool-daily-14"
    return SelectionResult(
        keyword=keyword,
        score=round(score, 4),
        selected_slug=slug,
        sample_count=len(rows),
        price_p25=round(p25, 2) if p25 is not None else None,
        price_p50=round(p50, 2) if p50 is not None else None,
        price_p75=round(p75, 2) if p75 is not None else None,
        title_similarity=round(similarity, 4),
        risk_penalty=risk_penalty,
        reasons=[] if score >= 0.65 else ["评分低于 0.65，使用预设保底产品"],
    )


def keywords_for_day(day: date, batch_size: int = 8) -> list[str]:
    start = (day.toordinal() * batch_size) % len(KEYWORDS)
    return [KEYWORDS[(start + offset) % len(KEYWORDS)] for offset in range(batch_size)]


def run_selection(
    *,
    cache_path: Path,
    search: KeywordSearchPort | None,
    day: date | None = None,
    interval_seconds: int = 15,
) -> dict[str, Any]:
    day = day or datetime.now(timezone.utc).astimezone().date()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache: dict[str, Any] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    results: list[SelectionResult] = []
    blocked = False
    blocked_reason: str | None = None
    for index, keyword in enumerate(keywords_for_day(day)):
        cached = cache.get(keyword)
        rows: list[dict[str, Any]] | None = None
        if cached:
            cached_at = datetime.fromisoformat(cached["cached_at"])
            if now - cached_at < timedelta(hours=24):
                rows = cached["rows"]
        if rows is None and search is not None:
            search_error: str | None = None
            try:
                rows = search.search(keyword)
                cache[keyword] = {"cached_at": now.isoformat(), "rows": rows}
                cache_path.write_text(
                    json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except SearchBlocked as exc:
                blocked = True
                blocked_reason = str(exc)
                break
            except Exception as exc:
                search_error = f"公开搜索失败：{type(exc).__name__}: {exc}"
                rows = []
            if index < 7:
                time.sleep(interval_seconds)
        else:
            search_error = None
        if search_error:
            results.append(
                SelectionResult(
                    keyword=keyword,
                    score=0.0,
                    selected_slug="hanzi-tracing-300",
                    sample_count=0,
                    price_p25=None,
                    price_p50=None,
                    price_p75=None,
                    title_similarity=0.0,
                    risk_penalty=0.0,
                    reasons=[search_error],
                )
            )
        else:
            results.append(score_rows(keyword, rows or []))
    best = max(results, key=lambda item: item.score) if results else None
    report = {
        "date": day.isoformat(),
        "blocked": blocked,
        "blocked_reason": blocked_reason,
        "keyword_count": len(results),
        "selected_slug": best.selected_slug if best and best.score >= 0.65 else "hanzi-tracing-300",
        "selected_keyword": best.keyword if best and best.score >= 0.65 else None,
        "score": best.score if best else 0.0,
        "fallback_used": not bool(best and best.score >= 0.65),
        "results": [
            {
                "keyword": item.keyword,
                "score": item.score,
                "selected_slug": item.selected_slug,
                "sample_count": item.sample_count,
                "price_p25": item.price_p25,
                "price_p50": item.price_p50,
                "price_p75": item.price_p75,
                "title_similarity": item.title_similarity,
                "reasons": item.reasons,
            }
            for item in results
        ],
    }
    (cache_path.parent / "selection-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report

