from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from xianyu_auto.selection import SearchBlocked, run_selection


class BlockingSearch:
    def __init__(self) -> None:
        self.calls = 0

    def search(self, keyword: str) -> list[dict[str, Any]]:
        self.calls += 1
        raise SearchBlocked(f"blocked: {keyword}")


def test_selection_stops_at_first_access_block(tmp_path: Path) -> None:
    search = BlockingSearch()
    report = run_selection(
        cache_path=tmp_path / "selection-cache.json",
        search=search,
        day=date(2026, 11, 6),
        interval_seconds=0,
    )

    assert search.calls == 1
    assert report["blocked"] is True
    assert report["blocked_reason"].startswith("blocked:")
    assert report["fallback_used"] is True
    assert report["keyword_count"] == 0
