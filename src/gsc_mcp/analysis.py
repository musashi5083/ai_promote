"""Google Search Console データの純粋な計算ロジック.

このモジュールはネットワークアクセスを一切行わない。API 呼び出しから
返ってきた行データ (dict) を受け取り、集計・スコアリングして返すだけなので、
そのまま pytest でテストできる。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

__all__ = [
    "DEFAULT_CTR_BASELINE",
    "parse_date",
    "expected_ctr",
    "normalize_rows",
    "compute_ctr_opportunities",
    "compute_period_comparison",
]


# ---------------------------------------------------------------------------
# 期待 CTR ベースライン
# ---------------------------------------------------------------------------
# 掲載順位 1〜20 位に対する「業界平均的な」クリック率の目安。
#
# 重要: これは GSC から取得した実データではなく、公開されている各種 CTR
# 調査 (Advanced Web Ranking / Backlinko 等) の形をならした *ヒューリスティック
# な曲線* である。実際の CTR カーブはサイト・クエリ種別 (指名/一般)・SERP の
# 機能 (強調スニペット、ショッピング枠など) によって大きく変わる。
#
# したがって「このスコアが高い = 必ず改善余地がある」ではなく、
# 「タイトル/ディスクリプションを見直す候補の優先順位付け」に使うこと。
# 自サイトの実測カーブがある場合は各ツールの `baseline` 引数で差し替えられる。
#
# index 0 が 1 位、index 19 が 20 位に対応する。
DEFAULT_CTR_BASELINE: List[float] = [
    0.280,  # 1 位
    0.155,  # 2 位
    0.100,  # 3 位
    0.070,  # 4 位
    0.053,  # 5 位
    0.042,  # 6 位
    0.034,  # 7 位
    0.028,  # 8 位
    0.024,  # 9 位
    0.021,  # 10 位
    0.018,  # 11 位
    0.016,  # 12 位
    0.014,  # 13 位
    0.013,  # 14 位
    0.012,  # 15 位
    0.011,  # 16 位
    0.0105,  # 17 位
    0.010,  # 18 位
    0.0098,  # 19 位
    0.0095,  # 20 位
]


# ---------------------------------------------------------------------------
# 日付
# ---------------------------------------------------------------------------
_RELATIVE_DAYS_RE = re.compile(r"^(\d+)daysAgo$", re.IGNORECASE)
_RELATIVE_MONTHS_RE = re.compile(r"^(\d+)monthsAgo$", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _subtract_months(anchor: date, months: int) -> date:
    """anchor から months ヶ月前の日付 (月末は丸める)."""
    total = (anchor.year * 12 + (anchor.month - 1)) - months
    year, month = divmod(total, 12)
    month += 1
    # 対象月に存在しない日 (例: 3/31 の 1 ヶ月前) は月末に丸める。
    day = anchor.day
    while day > 28:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1
    return date(year, month, day)


def parse_date(value: str, today: Optional[date] = None) -> str:
    """日付文字列を GSC API が受け付ける ``YYYY-MM-DD`` に正規化する.

    受け付ける形式:
      * ``YYYY-MM-DD``            … そのまま返す
      * ``today`` / ``yesterday`` … 相対指定
      * ``NdaysAgo``              … 例 ``7daysAgo`` ``28daysAgo`` ``90daysAgo``
      * ``NmonthsAgo``            … 例 ``16monthsAgo``

    Args:
        value: 変換したい日付文字列。
        today: 相対指定の基準日 (省略時は実行日)。テスト用。

    Raises:
        ValueError: 解釈できない文字列の場合。
    """
    if not isinstance(value, str):
        raise ValueError(f"日付は文字列で指定してください: {value!r}")

    raw = value.strip()
    anchor = today or date.today()

    if _ISO_DATE_RE.match(raw):
        # 妥当な日付かどうかも検証する (2025-13-40 などを弾く)。
        year, month, day = (int(part) for part in raw.split("-"))
        return date(year, month, day).isoformat()

    lowered = raw.lower()
    if lowered == "today":
        return anchor.isoformat()
    if lowered == "yesterday":
        return (anchor - timedelta(days=1)).isoformat()

    match = _RELATIVE_DAYS_RE.match(raw)
    if match:
        return (anchor - timedelta(days=int(match.group(1)))).isoformat()

    match = _RELATIVE_MONTHS_RE.match(raw)
    if match:
        return _subtract_months(anchor, int(match.group(1))).isoformat()

    raise ValueError(
        f"日付 {value!r} を解釈できません。"
        "'YYYY-MM-DD' か 'today' / 'yesterday' / '7daysAgo' / '16monthsAgo' "
        "のような形式で指定してください。"
    )


# ---------------------------------------------------------------------------
# 期待 CTR
# ---------------------------------------------------------------------------
def expected_ctr(
    position: float,
    baseline: Optional[Sequence[float]] = None,
) -> float:
    """平均掲載順位に対する期待 CTR を線形補間で求める.

    ``position`` は GSC の平均掲載順位なので 3.7 のような小数になる。
    整数位の間は線形補間する。1 位より上 (=1.0 未満) は 1 位の値、
    テーブル末尾より下の順位は末尾の値でクランプする。

    Args:
        position: 平均掲載順位 (1.0 起点)。
        baseline: 1 位から順に並べた期待 CTR (0.0〜1.0)。省略時は
            :data:`DEFAULT_CTR_BASELINE`。

    Returns:
        期待 CTR (0.0〜1.0 の小数)。
    """
    table = list(baseline) if baseline else DEFAULT_CTR_BASELINE
    if not table:
        return 0.0

    if position <= 1:
        return float(table[0])
    if position >= len(table):
        return float(table[-1])

    lower_index = int(position) - 1  # position=3.7 -> index 2 (3 位)
    upper_index = lower_index + 1
    fraction = position - int(position)
    lower = float(table[lower_index])
    upper = float(table[upper_index])
    return lower + (upper - lower) * fraction


# ---------------------------------------------------------------------------
# 行の正規化
# ---------------------------------------------------------------------------
def normalize_rows(
    rows: Iterable[Dict[str, Any]],
    dimensions: Sequence[str],
) -> List[Dict[str, Any]]:
    """API の ``rows`` を扱いやすい dict のリストに変換する.

    ``keys`` 配列を dimension 名のキーに展開し、``clicks`` / ``impressions``
    / ``ctr`` / ``position`` を数値として持たせる。
    """
    normalized: List[Dict[str, Any]] = []
    for row in rows or []:
        keys = row.get("keys") or []
        item: Dict[str, Any] = {}
        for index, dimension in enumerate(dimensions):
            item[dimension] = keys[index] if index < len(keys) else ""
        item["clicks"] = float(row.get("clicks", 0) or 0)
        item["impressions"] = float(row.get("impressions", 0) or 0)
        item["ctr"] = float(row.get("ctr", 0) or 0)
        item["position"] = float(row.get("position", 0) or 0)
        normalized.append(item)
    return normalized


# ---------------------------------------------------------------------------
# CTR 改善余地スコアリング
# ---------------------------------------------------------------------------
def compute_ctr_opportunities(
    rows: Iterable[Dict[str, Any]],
    dimension: str = "page",
    min_impressions: float = 100,
    max_position: float = 20.0,
    baseline: Optional[Sequence[float]] = None,
) -> List[Dict[str, Any]]:
    """タイトル/ディスクリプション改善の優先度を計算する純粋関数.

    各行について期待 CTR (順位から線形補間) と実 CTR を比較し、
    ``potential_clicks = impressions * max(0, expected_ctr - ctr)``
    をスコアとして降順に並べる。これは「順位を 1 ミリも動かさずに、
    CTR だけを平均水準まで戻せた場合に増えるクリック数」の概算である。

    Args:
        rows: :func:`normalize_rows` 済み、または ``keys`` を展開済みの行。
            ``dimension`` / ``clicks`` / ``impressions`` / ``ctr`` /
            ``position`` を含むこと。
        dimension: 行を識別するキー名 (``page`` または ``query``)。
        min_impressions: この表示回数未満の行は除外する (ノイズ除去)。
        max_position: この平均掲載順位より下の行は除外する。
        baseline: 期待 CTR カーブの上書き。

    Returns:
        ``potential_clicks`` 降順のリスト。各要素は入力の指標に加えて
        ``expected_ctr`` / ``ctr_gap`` / ``potential_clicks`` を持つ。
    """
    results: List[Dict[str, Any]] = []
    for row in rows or []:
        impressions = float(row.get("impressions", 0) or 0)
        position = float(row.get("position", 0) or 0)
        if impressions < min_impressions:
            continue
        if position > max_position:
            continue

        clicks = float(row.get("clicks", 0) or 0)
        ctr = float(row.get("ctr", 0) or 0)
        if not ctr and impressions:
            ctr = clicks / impressions

        exp = expected_ctr(position, baseline)
        gap = exp - ctr
        potential = impressions * max(0.0, gap)

        results.append(
            {
                dimension: row.get(dimension, ""),
                "impressions": impressions,
                "clicks": clicks,
                "ctr": ctr,
                "position": position,
                "expected_ctr": exp,
                "ctr_gap": gap,
                "potential_clicks": potential,
            }
        )

    # 主キー: potential_clicks 降順。同点は表示回数の多い方を上に。
    results.sort(key=lambda item: (item["potential_clicks"], item["impressions"]), reverse=True)
    return results


# ---------------------------------------------------------------------------
# 期間比較
# ---------------------------------------------------------------------------
def compute_period_comparison(
    current_rows: Iterable[Dict[str, Any]],
    previous_rows: Iterable[Dict[str, Any]],
    dimension: str = "page",
) -> List[Dict[str, Any]]:
    """2 期間の行を dimension 値で突き合わせて差分を出す純粋関数.

    どちらか一方にしか存在しない行も残し、欠けている側は 0 として扱う
    (新規に順位が付いたページ / 圏外に落ちたページを見落とさないため)。
    並び順はクリック数差分の絶対値の降順。

    Args:
        current_rows: 比較したい期間 (通常は「後」の期間) の行。
        previous_rows: 基準となる期間 (通常は「前」の期間) の行。
        dimension: 突き合わせキー (``page`` / ``query`` など)。

    Returns:
        ``*_current`` / ``*_previous`` / ``*_delta`` を持つ dict のリスト。
        ``position_delta`` は「順位が改善するとマイナス」になる素の差分
        (current - previous) である点に注意。
    """
    current_map = {str(row.get(dimension, "")): row for row in current_rows or []}
    previous_map = {str(row.get(dimension, "")): row for row in previous_rows or []}

    merged: List[Dict[str, Any]] = []
    for key in list(current_map) + [k for k in previous_map if k not in current_map]:
        cur = current_map.get(key, {})
        prev = previous_map.get(key, {})

        def metric(source: Dict[str, Any], name: str) -> float:
            return float(source.get(name, 0) or 0)

        clicks_c, clicks_p = metric(cur, "clicks"), metric(prev, "clicks")
        impr_c, impr_p = metric(cur, "impressions"), metric(prev, "impressions")
        ctr_c, ctr_p = metric(cur, "ctr"), metric(prev, "ctr")
        pos_c, pos_p = metric(cur, "position"), metric(prev, "position")

        merged.append(
            {
                dimension: key,
                "clicks_current": clicks_c,
                "clicks_previous": clicks_p,
                "clicks_delta": clicks_c - clicks_p,
                "impressions_current": impr_c,
                "impressions_previous": impr_p,
                "impressions_delta": impr_c - impr_p,
                "ctr_current": ctr_c,
                "ctr_previous": ctr_p,
                "ctr_delta": ctr_c - ctr_p,
                "position_current": pos_c,
                "position_previous": pos_p,
                # 片側にしか存在しない場合は順位差分を出さない (0 との差は無意味)。
                "position_delta": (pos_c - pos_p) if (pos_c and pos_p) else 0.0,
            }
        )

    merged.sort(key=lambda item: abs(item["clicks_delta"]), reverse=True)
    return merged
