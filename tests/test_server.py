"""ネットワーク不要のユニットテスト.

日付ショートハンドの解釈、期待 CTR の線形補間、ctr_opportunities の
スコアリング/並び順、そしてサーバーが 6 つのツールを公開していることを検証する。
認証情報が無い状態でも全て通ること（= サーバー起動と tools/list が
資格情報に依存しないこと）が要件なので、環境変数は明示的に外して実行する。
"""

from __future__ import annotations

import asyncio
import os
from datetime import date

import pytest

from gsc_mcp.analysis import (
    DEFAULT_CTR_BASELINE,
    compute_ctr_opportunities,
    compute_period_comparison,
    expected_ctr,
    normalize_rows,
    parse_date,
)


@pytest.fixture(autouse=True)
def _no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """認証情報が無い状態を保証する."""
    for name in ("GSC_SERVICE_ACCOUNT_FILE", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# 日付ショートハンド
# ---------------------------------------------------------------------------
TODAY = date(2026, 7, 25)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024-01-31", "2024-01-31"),
        ("today", "2026-07-25"),
        ("yesterday", "2026-07-24"),
        ("7daysAgo", "2026-07-18"),
        ("28daysAgo", "2026-06-27"),
        ("90daysAgo", "2026-04-26"),
        ("16monthsAgo", "2025-03-25"),
        ("  today  ", "2026-07-25"),
        ("TODAY", "2026-07-25"),
    ],
)
def test_parse_date_shorthands(value: str, expected: str) -> None:
    assert parse_date(value, today=TODAY) == expected


def test_parse_date_month_end_rounds_down() -> None:
    # 3/31 の 1 ヶ月前は 2 月末に丸める。
    assert parse_date("1monthsAgo", today=date(2026, 3, 31)) == "2026-02-28"


@pytest.mark.parametrize("value", ["", "last week", "2024-13-01", "7days", "20240101"])
def test_parse_date_rejects_garbage(value: str) -> None:
    with pytest.raises(ValueError):
        parse_date(value, today=TODAY)


# ---------------------------------------------------------------------------
# 期待 CTR の補間
# ---------------------------------------------------------------------------
def test_expected_ctr_integer_positions_match_table() -> None:
    for index, value in enumerate(DEFAULT_CTR_BASELINE):
        assert expected_ctr(index + 1) == pytest.approx(value)


def test_expected_ctr_linear_interpolation() -> None:
    midpoint = (DEFAULT_CTR_BASELINE[0] + DEFAULT_CTR_BASELINE[1]) / 2
    assert expected_ctr(1.5) == pytest.approx(midpoint)

    # 3.25 位は 3 位と 4 位の間を 25% 進んだ値。
    low, high = DEFAULT_CTR_BASELINE[2], DEFAULT_CTR_BASELINE[3]
    assert expected_ctr(3.25) == pytest.approx(low + (high - low) * 0.25)


def test_expected_ctr_clamps_out_of_range() -> None:
    assert expected_ctr(0.5) == pytest.approx(DEFAULT_CTR_BASELINE[0])
    assert expected_ctr(1.0) == pytest.approx(DEFAULT_CTR_BASELINE[0])
    assert expected_ctr(20.0) == pytest.approx(DEFAULT_CTR_BASELINE[-1])
    assert expected_ctr(75.0) == pytest.approx(DEFAULT_CTR_BASELINE[-1])


def test_expected_ctr_is_monotonically_decreasing() -> None:
    values = [expected_ctr(p / 2) for p in range(2, 41)]
    assert all(a >= b for a, b in zip(values, values[1:]))


def test_expected_ctr_custom_baseline_overrides_default() -> None:
    baseline = [0.5, 0.3, 0.1]
    assert expected_ctr(1, baseline) == pytest.approx(0.5)
    assert expected_ctr(2.5, baseline) == pytest.approx(0.2)
    assert expected_ctr(99, baseline) == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# normalize_rows
# ---------------------------------------------------------------------------
def test_normalize_rows_expands_keys() -> None:
    rows = normalize_rows(
        [{"keys": ["/a", "MOBILE"], "clicks": 3, "impressions": 100, "ctr": 0.03, "position": 4.2}],
        ["page", "device"],
    )
    assert rows == [
        {
            "page": "/a",
            "device": "MOBILE",
            "clicks": 3.0,
            "impressions": 100.0,
            "ctr": 0.03,
            "position": 4.2,
        }
    ]


def test_normalize_rows_handles_empty_input() -> None:
    assert normalize_rows([], ["query"]) == []


# ---------------------------------------------------------------------------
# ctr_opportunities のスコアリング
# ---------------------------------------------------------------------------
def _row(page: str, impressions: float, ctr: float, position: float) -> dict:
    return {
        "page": page,
        "impressions": impressions,
        "clicks": round(impressions * ctr),
        "ctr": ctr,
        "position": position,
    }


def test_scoring_sorts_by_potential_clicks_desc() -> None:
    rows = [
        # 3 位 (期待 10%) で実 CTR 2% → 余地 8% × 1000 = 80 クリック
        _row("/big-gap", 1000, 0.02, 3.0),
        # 3 位で実 CTR 2% だが表示 200 → 16 クリック
        _row("/small-gap", 200, 0.02, 3.0),
        # 5 位 (期待 5.3%) で実 CTR 12% → 期待超えなので 0
        _row("/over-performing", 5000, 0.12, 5.0),
    ]
    result = compute_ctr_opportunities(rows)

    assert [r["page"] for r in result] == ["/big-gap", "/small-gap", "/over-performing"]
    assert result[0]["potential_clicks"] == pytest.approx(1000 * (0.10 - 0.02))
    assert result[1]["potential_clicks"] == pytest.approx(200 * (0.10 - 0.02))
    # 期待を上回る行のスコアは負にならず 0 になる。
    assert result[2]["potential_clicks"] == 0.0
    assert result[2]["ctr_gap"] < 0


def test_scoring_filters_low_impressions_and_deep_positions() -> None:
    rows = [
        _row("/keep", 500, 0.01, 4.0),
        _row("/too-few-impressions", 99, 0.001, 4.0),
        _row("/too-deep", 5000, 0.001, 25.0),
    ]
    result = compute_ctr_opportunities(rows, min_impressions=100, max_position=20.0)
    assert [r["page"] for r in result] == ["/keep"]


def test_scoring_thresholds_are_configurable() -> None:
    rows = [_row("/small", 50, 0.001, 30.0)]
    assert compute_ctr_opportunities(rows) == []
    loosened = compute_ctr_opportunities(rows, min_impressions=10, max_position=40.0)
    assert len(loosened) == 1


def test_scoring_derives_ctr_when_missing() -> None:
    rows = [{"page": "/x", "impressions": 1000, "clicks": 50, "position": 3.0}]
    result = compute_ctr_opportunities(rows)
    assert result[0]["ctr"] == pytest.approx(0.05)
    assert result[0]["potential_clicks"] == pytest.approx(1000 * (0.10 - 0.05))


def test_scoring_supports_query_dimension_and_custom_baseline() -> None:
    rows = [{"query": "ai 記事作成", "impressions": 400, "clicks": 4, "ctr": 0.01, "position": 2.0}]
    result = compute_ctr_opportunities(rows, dimension="query", baseline=[0.5, 0.25, 0.1])
    assert result[0]["query"] == "ai 記事作成"
    assert result[0]["expected_ctr"] == pytest.approx(0.25)
    assert result[0]["potential_clicks"] == pytest.approx(400 * 0.24)


def test_scoring_output_contains_all_expected_fields() -> None:
    result = compute_ctr_opportunities([_row("/a", 1000, 0.01, 2.0)])
    assert set(result[0]) == {
        "page",
        "impressions",
        "clicks",
        "ctr",
        "position",
        "expected_ctr",
        "ctr_gap",
        "potential_clicks",
    }


# ---------------------------------------------------------------------------
# 期間比較
# ---------------------------------------------------------------------------
def test_compare_periods_computes_deltas_and_sorts_by_abs_click_delta() -> None:
    current = [
        {"page": "/up", "clicks": 120, "impressions": 1000, "ctr": 0.12, "position": 3.0},
        {"page": "/down", "clicks": 10, "impressions": 900, "ctr": 0.011, "position": 8.0},
        {"page": "/new", "clicks": 30, "impressions": 300, "ctr": 0.10, "position": 5.0},
    ]
    previous = [
        {"page": "/up", "clicks": 40, "impressions": 950, "ctr": 0.042, "position": 4.0},
        {"page": "/down", "clicks": 200, "impressions": 1000, "ctr": 0.20, "position": 2.0},
        {"page": "/gone", "clicks": 5, "impressions": 100, "ctr": 0.05, "position": 9.0},
    ]
    result = compute_period_comparison(current, previous)

    assert [r["page"] for r in result] == ["/down", "/up", "/new", "/gone"]

    up = next(r for r in result if r["page"] == "/up")
    assert up["clicks_delta"] == 80
    assert up["impressions_delta"] == 50
    assert up["ctr_delta"] == pytest.approx(0.078)
    assert up["position_delta"] == pytest.approx(-1.0)  # マイナス = 順位改善

    # 片側にしか無い行も残り、欠けている側は 0 として扱う。
    gone = next(r for r in result if r["page"] == "/gone")
    assert gone["clicks_current"] == 0
    assert gone["clicks_delta"] == -5
    assert gone["position_delta"] == 0.0  # 片側欠損なので順位差分は出さない


# ---------------------------------------------------------------------------
# サーバーのツール公開状況 (認証情報なしで import & list できること)
# ---------------------------------------------------------------------------
EXPECTED_TOOLS = {
    "list_sites",
    "search_analytics",
    "top_queries",
    "top_pages",
    "ctr_opportunities",
    "compare_periods",
}


def test_server_exposes_all_six_tools() -> None:
    from gsc_mcp.server import mcp

    tools = asyncio.run(mcp.list_tools())
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    assert len(tools) == 6


def test_every_tool_has_a_description() -> None:
    from gsc_mcp.server import mcp

    tools = asyncio.run(mcp.list_tools())
    for tool in tools:
        assert tool.description and tool.description.strip(), tool.name


def test_tools_import_without_credentials() -> None:
    """認証情報が無くても import 時に例外を投げない (遅延初期化の確認)."""
    assert os.environ.get("GSC_SERVICE_ACCOUNT_FILE") is None
    from gsc_mcp import server

    assert server.MAX_ROW_LIMIT == 25000


def test_tool_returns_actionable_error_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """認証情報が無い状態でツールを呼ぶと、日本語の案内文が返る (例外で落ちない)."""
    from gsc_mcp import auth, server

    monkeypatch.setattr(auth, "_client", None)
    monkeypatch.setenv("GSC_SITE_URL", "sc-domain:example.com")

    message = server.list_sites()
    assert "GSC_SERVICE_ACCOUNT_FILE" in message
    assert "ユーザーと権限" in message


def test_row_limit_is_capped_at_api_maximum() -> None:
    from gsc_mcp.server import MAX_ROW_LIMIT, _clamp_row_limit

    assert _clamp_row_limit(100) == (100, None)
    value, note = _clamp_row_limit(999999)
    assert value == MAX_ROW_LIMIT
    assert note and "25000" in note
