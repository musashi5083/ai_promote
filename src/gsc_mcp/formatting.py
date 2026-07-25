"""ツールの出力整形 (トークン節約のため既定は markdown テーブル)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence

__all__ = ["format_ctr", "format_position", "markdown_table", "to_json", "truncate_text"]

MAX_CELL_WIDTH = 90


def format_ctr(value: float) -> str:
    """0.0〜1.0 の CTR を ``12.34%`` 形式にする."""
    return f"{value * 100:.2f}%"


def format_position(value: float) -> str:
    """平均掲載順位を小数第 1 位までにする."""
    return f"{value:.1f}"


def truncate_text(value: str, limit: int = MAX_CELL_WIDTH) -> str:
    """長い URL / クエリを省略してテーブル幅を抑える."""
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _escape(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """列幅を揃えた markdown テーブルを組み立てる."""
    if not rows:
        return "（該当する行はありません）"

    cells: List[List[str]] = [[_escape(h) for h in headers]]
    cells += [[_escape(c) for c in row] for row in rows]

    widths = [0] * len(headers)
    for row in cells:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], len(cell))

    def line(row: Sequence[str]) -> str:
        padded = [
            (row[i] if i < len(row) else "").ljust(widths[i]) for i in range(len(widths))
        ]
        return "| " + " | ".join(padded) + " |"

    separator = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    return "\n".join([line(cells[0]), separator, *(line(r) for r in cells[1:])])


def to_json(payload: Any) -> str:
    """`output="json"` 用の生データ出力."""
    return json.dumps(payload, ensure_ascii=False, indent=2)


def totals_line(rows: Sequence[Dict[str, Any]]) -> str:
    """クリック / 表示回数 / 平均 CTR / 平均掲載順位の 1 行サマリ."""
    if not rows:
        return "合計: 0 行"
    clicks = sum(float(r.get("clicks", 0) or 0) for r in rows)
    impressions = sum(float(r.get("impressions", 0) or 0) for r in rows)
    ctr = (clicks / impressions) if impressions else 0.0
    weighted_pos = (
        sum(float(r.get("position", 0) or 0) * float(r.get("impressions", 0) or 0) for r in rows)
        / impressions
        if impressions
        else 0.0
    )
    return (
        f"合計: {len(rows)} 行 / クリック {clicks:,.0f} / 表示 {impressions:,.0f} / "
        f"CTR {format_ctr(ctr)} / 平均掲載順位 {format_position(weighted_pos)}"
    )
