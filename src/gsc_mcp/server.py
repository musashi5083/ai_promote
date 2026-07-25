"""Google Search Console MCP サーバー (stdio).

サービスアカウント認証で Search Console の検索パフォーマンスデータを読み取り、
特にタイトル / メタディスクリプションの CTR 改善候補の優先順位付けを支援する。

環境変数:
    GSC_SERVICE_ACCOUNT_FILE  サービスアカウント JSON 鍵のパス
                              (未設定なら GOOGLE_APPLICATION_CREDENTIALS)
    GSC_SITE_URL              既定のプロパティ (例: sc-domain:example.com)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from mcp.server.fastmcp import FastMCP

from .analysis import (
    compute_ctr_opportunities,
    compute_period_comparison,
    normalize_rows,
    parse_date,
)
from .auth import (
    GSCAuthError,
    default_site_url,
    describe_api_error,
    get_client,
    resolve_site_url,
)
from .formatting import (
    format_ctr,
    format_position,
    markdown_table,
    to_json,
    totals_line,
    truncate_text,
)

mcp = FastMCP("gsc")

#: Search Console API が 1 リクエストで返せる最大行数。
MAX_ROW_LIMIT = 25000

VALID_DIMENSIONS = {"query", "page", "country", "device", "date", "searchAppearance"}


# ---------------------------------------------------------------------------
# 内部ヘルパー
# ---------------------------------------------------------------------------
def _clamp_row_limit(row_limit: int) -> tuple[int, Optional[str]]:
    """row_limit を 1〜25000 に収める。丸めた場合は注意文も返す."""
    try:
        value = int(row_limit)
    except (TypeError, ValueError):
        return 100, "row_limit が数値ではなかったため 100 を使用しました。"
    if value > MAX_ROW_LIMIT:
        return MAX_ROW_LIMIT, (
            f"注: Search Console API の 1 リクエスト上限のため row_limit を "
            f"{MAX_ROW_LIMIT} に丸めました（ページングは行いません）。"
        )
    if value < 1:
        return 1, "row_limit が 1 未満だったため 1 を使用しました。"
    return value, None


def _build_request(
    start_date: str,
    end_date: str,
    dimensions: Sequence[str],
    search_type: str = "web",
    row_limit: int = 100,
    dimension_filters: Optional[List[Dict[str, str]]] = None,
    data_state: str = "final",
    start_row: int = 0,
) -> Dict[str, Any]:
    """searchanalytics.query のリクエストボディを組み立てる."""
    body: Dict[str, Any] = {
        "startDate": parse_date(start_date),
        "endDate": parse_date(end_date),
        "dimensions": list(dimensions),
        "type": search_type,
        "rowLimit": row_limit,
        "startRow": start_row,
        "dataState": (data_state or "final").lower(),
    }
    if dimension_filters:
        filters = []
        for item in dimension_filters:
            if not item:
                continue
            filters.append(
                {
                    "dimension": item.get("dimension", ""),
                    "operator": item.get("operator", "equals"),
                    "expression": item.get("expression", ""),
                }
            )
        if filters:
            body["dimensionFilterGroups"] = [{"filters": filters}]
    return body


def _run_query(site_url: str, body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """searchanalytics.query を実行して生の rows を返す."""
    client = get_client()
    response = (
        client.searchanalytics().query(siteUrl=site_url, body=body).execute()
    )
    return response.get("rows", []) or []


def _fetch(
    site_url: Optional[str],
    start_date: str,
    end_date: str,
    dimensions: Sequence[str],
    search_type: str = "web",
    row_limit: int = 100,
    dimension_filters: Optional[List[Dict[str, str]]] = None,
    data_state: str = "final",
) -> tuple[str, List[Dict[str, Any]]]:
    """プロパティ解決 → API 呼び出し → 行の正規化 までをまとめて行う."""
    target = resolve_site_url(site_url)
    body = _build_request(
        start_date=start_date,
        end_date=end_date,
        dimensions=dimensions,
        search_type=search_type,
        row_limit=row_limit,
        dimension_filters=dimension_filters,
        data_state=data_state,
    )
    rows = _run_query(target, body)
    return target, normalize_rows(rows, dimensions)


def _page_filter(page: Optional[str]) -> Optional[List[Dict[str, str]]]:
    """page 引数を dimensionFilter に変換する (部分一致)."""
    if not page:
        return None
    return [{"dimension": "page", "operator": "contains", "expression": page}]


def _error_text(exc: Exception, site_url: Optional[str] = None) -> str:
    """例外を利用者向けメッセージへ."""
    if isinstance(exc, GSCAuthError):
        return f"エラー: {exc}"
    if isinstance(exc, ValueError):
        return f"エラー: {exc}"
    return f"エラー: {describe_api_error(exc, site_url)}"


def _header(site_url: str, start_date: str, end_date: str, extra: str = "") -> str:
    period = f"{parse_date(start_date)} 〜 {parse_date(end_date)}"
    line = f"プロパティ: {site_url} / 期間: {period}"
    return f"{line} / {extra}" if extra else line


# ---------------------------------------------------------------------------
# ツール
# ---------------------------------------------------------------------------
@mcp.tool()
def list_sites() -> str:
    """サービスアカウントがアクセスできる Search Console プロパティを一覧する.

    セットアップ確認に最初に使うツール。ここに目的のプロパティが出てこない場合は、
    サービスアカウントのメールアドレスが Search Console の
    [設定] → [ユーザーと権限] に追加されていない (= 他ツールは 403 になる)。

    Returns:
        プロパティ URL と権限レベルの一覧テキスト。
        既定プロパティ (GSC_SITE_URL) も併記する。
    """
    try:
        client = get_client()
        response = client.sites().list().execute()
    except Exception as exc:  # noqa: BLE001
        return _error_text(exc)

    entries = response.get("siteEntry", []) or []
    if not entries:
        return (
            "アクセス可能なプロパティが 1 件もありません。\n"
            "Search Console の [設定] → [ユーザーと権限] で、サービスアカウントの "
            "メールアドレス (JSON 内の client_email) を対象プロパティに追加してください。"
        )

    rows = [
        [entry.get("siteUrl", ""), entry.get("permissionLevel", "")] for entry in entries
    ]
    table = markdown_table(["siteUrl", "permissionLevel"], rows)
    default = default_site_url() or "(未設定)"
    return f"既定プロパティ (GSC_SITE_URL): {default}\n\n{table}\n\n合計: {len(rows)} 件"


@mcp.tool()
def search_analytics(
    start_date: str,
    end_date: str,
    dimensions: Optional[List[str]] = None,
    site_url: Optional[str] = None,
    search_type: str = "web",
    row_limit: int = 100,
    dimension_filters: Optional[List[Dict[str, str]]] = None,
    data_state: str = "final",
    output: str = "table",
) -> str:
    """Search Console 検索パフォーマンス API の汎用クエリ（万能の逃げ道）.

    top_queries / top_pages / ctr_opportunities で足りない切り口
    (国別・デバイス別・日次推移・複数ディメンションの組み合わせなど) を
    調べたいときに使う。

    Args:
        start_date: 開始日。'YYYY-MM-DD' か 'today' / 'yesterday' /
            '7daysAgo' / '28daysAgo' / '90daysAgo' / '16monthsAgo'。
        end_date: 終了日。同じ形式。
        dimensions: 集計軸のリスト。query / page / country / device / date /
            searchAppearance から選ぶ。省略時は ['query']。
        site_url: 対象プロパティ。省略時は環境変数 GSC_SITE_URL。
        search_type: 検索の種類。web (既定) / image / video / news / discover /
            googleNews。
        row_limit: 取得行数。既定 100、上限 25000 (超えた分は丸める。ページングはしない)。
        dimension_filters: 絞り込み条件のリスト。各要素は
            {"dimension": "page", "operator": "contains", "expression": "/blog/"}。
            operator は equals / notEquals / contains / notContains /
            includingRegex / excludingRegex。
        data_state: 'final' (確定値、既定) か 'all' (直近の未確定データを含む)。
        output: 'table' (既定、markdown テーブル) か 'json' (生データ)。

    Returns:
        markdown テーブル + 合計サマリ、または JSON 文字列。
    """
    dims = list(dimensions) if dimensions else ["query"]
    invalid = [d for d in dims if d not in VALID_DIMENSIONS]
    if invalid:
        return (
            f"エラー: 未対応の dimension です: {', '.join(invalid)}\n"
            f"使用できるのは {', '.join(sorted(VALID_DIMENSIONS))} です。"
        )

    limit, note = _clamp_row_limit(row_limit)
    try:
        target, rows = _fetch(
            site_url=site_url,
            start_date=start_date,
            end_date=end_date,
            dimensions=dims,
            search_type=search_type,
            row_limit=limit,
            dimension_filters=dimension_filters,
            data_state=data_state,
        )
    except Exception as exc:  # noqa: BLE001
        return _error_text(exc, site_url)

    rows = rows[:limit]
    if output == "json":
        return to_json({"siteUrl": target, "dimensions": dims, "rows": rows})

    headers = [*dims, "clicks", "impressions", "ctr", "position"]
    body = [
        [
            *[truncate_text(row.get(d, "")) for d in dims],
            f"{row['clicks']:,.0f}",
            f"{row['impressions']:,.0f}",
            format_ctr(row["ctr"]),
            format_position(row["position"]),
        ]
        for row in rows
    ]
    parts = [
        _header(target, start_date, end_date, f"type={search_type} / dataState={data_state}"),
        markdown_table(headers, body),
        totals_line(rows),
    ]
    if note:
        parts.append(note)
    return "\n\n".join(parts)


@mcp.tool()
def top_queries(
    start_date: str = "28daysAgo",
    end_date: str = "yesterday",
    site_url: Optional[str] = None,
    row_limit: int = 50,
    page: Optional[str] = None,
    search_type: str = "web",
    output: str = "table",
) -> str:
    """表示回数の多い検索クエリ上位を取得する.

    「どんな語で見られているか」を把握する用途。特定ページの流入語を見たい場合は
    page 引数で絞り込む (タイトル書き換え時に、そのページが実際に拾っている語を
    確認するのに便利)。

    Args:
        start_date: 開始日 (既定 '28daysAgo')。相対指定可。
        end_date: 終了日 (既定 'yesterday')。GSC のデータは 2〜3 日遅れる点に注意。
        site_url: 対象プロパティ。省略時は環境変数 GSC_SITE_URL。
        row_limit: 取得行数 (既定 50、上限 25000)。
        page: 指定すると、その文字列を含む URL のクエリだけに絞り込む (部分一致)。
        search_type: web (既定) / image / video / news / discover / googleNews。
        output: 'table' (既定) か 'json'。

    Returns:
        表示回数降順のクエリ一覧テーブル + 合計サマリ。
    """
    limit, note = _clamp_row_limit(row_limit)
    try:
        target, rows = _fetch(
            site_url=site_url,
            start_date=start_date,
            end_date=end_date,
            dimensions=["query"],
            search_type=search_type,
            row_limit=limit,
            dimension_filters=_page_filter(page),
        )
    except Exception as exc:  # noqa: BLE001
        return _error_text(exc, site_url)

    rows.sort(key=lambda r: r["impressions"], reverse=True)
    rows = rows[:limit]
    if output == "json":
        return to_json({"siteUrl": target, "rows": rows})

    body = [
        [
            truncate_text(row.get("query", "")),
            f"{row['clicks']:,.0f}",
            f"{row['impressions']:,.0f}",
            format_ctr(row["ctr"]),
            format_position(row["position"]),
        ]
        for row in rows
    ]
    extra = f"page contains '{page}'" if page else ""
    parts = [
        _header(target, start_date, end_date, extra),
        markdown_table(["query", "clicks", "impressions", "ctr", "position"], body),
        totals_line(rows),
    ]
    if note:
        parts.append(note)
    return "\n\n".join(parts)


@mcp.tool()
def top_pages(
    start_date: str = "28daysAgo",
    end_date: str = "yesterday",
    site_url: Optional[str] = None,
    row_limit: int = 50,
    search_type: str = "web",
    output: str = "table",
) -> str:
    """表示回数の多いページ上位を取得する.

    サイト全体でどのページが検索に露出しているかを把握する用途。
    「どのページのタイトルを直すべきか」を決めるなら ctr_opportunities の方が適切。

    Args:
        start_date: 開始日 (既定 '28daysAgo')。相対指定可。
        end_date: 終了日 (既定 'yesterday')。
        site_url: 対象プロパティ。省略時は環境変数 GSC_SITE_URL。
        row_limit: 取得行数 (既定 50、上限 25000)。
        search_type: web (既定) / image / video / news / discover / googleNews。
        output: 'table' (既定) か 'json'。

    Returns:
        表示回数降順のページ一覧テーブル + 合計サマリ。
    """
    limit, note = _clamp_row_limit(row_limit)
    try:
        target, rows = _fetch(
            site_url=site_url,
            start_date=start_date,
            end_date=end_date,
            dimensions=["page"],
            search_type=search_type,
            row_limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        return _error_text(exc, site_url)

    rows.sort(key=lambda r: r["impressions"], reverse=True)
    rows = rows[:limit]
    if output == "json":
        return to_json({"siteUrl": target, "rows": rows})

    body = [
        [
            truncate_text(row.get("page", "")),
            f"{row['clicks']:,.0f}",
            f"{row['impressions']:,.0f}",
            format_ctr(row["ctr"]),
            format_position(row["position"]),
        ]
        for row in rows
    ]
    parts = [
        _header(target, start_date, end_date),
        markdown_table(["page", "clicks", "impressions", "ctr", "position"], body),
        totals_line(rows),
    ]
    if note:
        parts.append(note)
    return "\n\n".join(parts)


@mcp.tool()
def ctr_opportunities(
    start_date: str = "28daysAgo",
    end_date: str = "yesterday",
    dimension: str = "page",
    site_url: Optional[str] = None,
    min_impressions: int = 100,
    max_position: float = 20.0,
    row_limit: int = 30,
    baseline: Optional[List[float]] = None,
    search_type: str = "web",
    output: str = "table",
) -> str:
    """タイトル / メタディスクリプション改善の優先順位リストを作る（このサーバーの中核）.

    十分な表示回数がありながら、その掲載順位で期待される CTR に届いていない
    ページ (または クエリ) を洗い出す。スコアは

        potential_clicks = impressions × max(0, 期待CTR − 実CTR)

    で、「順位を上げずに CTR だけ平均水準に戻せた場合に増えるクリック数」の概算。
    この降順がそのままタイトル書き換えの作業順になる。

    期待 CTR は掲載順位 1〜20 位の業界平均的なカーブ (線形補間) から求めるが、
    これは GSC の実データではなくヒューリスティックな目安である。指名検索が多い、
    強調スニペットに奪われている等でサイトごとに差が出るため、自サイトの実測
    カーブがあれば baseline 引数で差し替えること。

    Args:
        start_date: 開始日 (既定 '28daysAgo')。相対指定可。
        end_date: 終了日 (既定 'yesterday')。
        dimension: 'page' (既定、ページ単位) か 'query' (クエリ単位)。
        site_url: 対象プロパティ。省略時は環境変数 GSC_SITE_URL。
        min_impressions: この表示回数未満の行は除外 (既定 100)。少なすぎる
            サンプルは CTR が不安定なため。
        max_position: この平均掲載順位より下の行は除外 (既定 20.0)。2 ページ目
            以下は CTR ではなく順位の問題であることが多い。
        row_limit: 出力する行数 (既定 30)。API からは余裕をもって取得する。
        baseline: 1 位から順に並べた期待 CTR の配列 (例 [0.28, 0.15, ...])。
            省略時は既定カーブ。
        search_type: web (既定) / image / video / news / discover / googleNews。
        output: 'table' (既定) か 'json'。

    Returns:
        potential_clicks 降順のテーブル (impressions / clicks / ctr / position /
        expected_ctr / ctr_gap / potential_clicks) と合計サマリ。
    """
    if dimension not in {"page", "query"}:
        return "エラー: dimension は 'page' か 'query' を指定してください。"

    limit, _ = _clamp_row_limit(row_limit)
    # 絞り込みで多くが落ちるため、API からは多めに取得する。
    fetch_limit = min(MAX_ROW_LIMIT, max(limit * 20, 1000))

    try:
        target, rows = _fetch(
            site_url=site_url,
            start_date=start_date,
            end_date=end_date,
            dimensions=[dimension],
            search_type=search_type,
            row_limit=fetch_limit,
        )
    except Exception as exc:  # noqa: BLE001
        return _error_text(exc, site_url)

    scored = compute_ctr_opportunities(
        rows,
        dimension=dimension,
        min_impressions=min_impressions,
        max_position=max_position,
        baseline=baseline,
    )
    total_candidates = len(scored)
    scored = scored[:limit]

    if output == "json":
        return to_json(
            {
                "siteUrl": target,
                "dimension": dimension,
                "minImpressions": min_impressions,
                "maxPosition": max_position,
                "baselineOverridden": bool(baseline),
                "rows": scored,
            }
        )

    if not scored:
        return (
            _header(target, start_date, end_date)
            + "\n\n条件に合う行がありませんでした。"
            f"（取得 {len(rows)} 行 / 条件: 表示回数 >= {min_impressions}, 順位 <= {max_position}）\n"
            "min_impressions を下げる、max_position を上げる、期間を延ばす等をお試しください。"
        )

    body = [
        [
            truncate_text(row.get(dimension, "")),
            f"{row['impressions']:,.0f}",
            f"{row['clicks']:,.0f}",
            format_ctr(row["ctr"]),
            format_position(row["position"]),
            format_ctr(row["expected_ctr"]),
            format_ctr(row["ctr_gap"]),
            f"{row['potential_clicks']:,.1f}",
        ]
        for row in scored
    ]
    headers = [
        dimension,
        "impressions",
        "clicks",
        "ctr",
        "position",
        "expected_ctr",
        "ctr_gap",
        "potential_clicks",
    ]
    upside = sum(row["potential_clicks"] for row in scored)
    summary = (
        f"表示 {sum(r['impressions'] for r in scored):,.0f} / "
        f"クリック {sum(r['clicks'] for r in scored):,.0f} / "
        f"改善余地の合計 {upside:,.0f} クリック（上位 {len(scored)} 件 / 候補 {total_candidates} 件）"
    )
    note = (
        "※ expected_ctr は業界平均的なヒューリスティック曲線であり、GSC の実データではない。"
        "自サイトの実測カーブがある場合は baseline 引数で上書きすること。"
    )
    if baseline:
        note = "※ baseline 引数で指定された期待 CTR 曲線を使用。"
    return "\n\n".join(
        [
            _header(target, start_date, end_date, f"dimension={dimension}"),
            markdown_table(headers, body),
            summary,
            note,
        ]
    )


@mcp.tool()
def compare_periods(
    current_start: str,
    current_end: str,
    previous_start: str,
    previous_end: str,
    dimension: str = "page",
    site_url: Optional[str] = None,
    row_limit: int = 30,
    search_type: str = "web",
    output: str = "table",
) -> str:
    """2 つの期間を同じ軸で取得し、差分を並べる（施策の効果検証用）.

    「タイトルを書き換えた後、CTR とクリックは実際に伸びたか」を確認するための
    ツール。書き換え前の期間を previous_*、後の期間を current_* に指定する。
    クリック数差分の絶対値が大きい順に並ぶので、伸びた行と落ちた行が両端に来る。

    Args:
        current_start: 比較したい期間 (通常は「後」) の開始日。相対指定可。
        current_end: 同・終了日。
        previous_start: 基準期間 (通常は「前」) の開始日。
        previous_end: 同・終了日。
        dimension: 突き合わせ軸。'page' (既定) / 'query' / 'country' / 'device'。
        site_url: 対象プロパティ。省略時は環境変数 GSC_SITE_URL。
        row_limit: 出力行数 (既定 30)。
        search_type: web (既定) / image / video / news / discover / googleNews。
        output: 'table' (既定) か 'json'。

    Returns:
        clicks / impressions / ctr / position の差分テーブル。position_delta は
        マイナスが「順位改善」を意味する。
    """
    if dimension not in VALID_DIMENSIONS:
        return (
            f"エラー: 未対応の dimension です: {dimension}\n"
            f"使用できるのは {', '.join(sorted(VALID_DIMENSIONS))} です。"
        )

    limit, _ = _clamp_row_limit(row_limit)
    fetch_limit = min(MAX_ROW_LIMIT, max(limit * 20, 1000))

    try:
        target, current_rows = _fetch(
            site_url=site_url,
            start_date=current_start,
            end_date=current_end,
            dimensions=[dimension],
            search_type=search_type,
            row_limit=fetch_limit,
        )
        _, previous_rows = _fetch(
            site_url=site_url,
            start_date=previous_start,
            end_date=previous_end,
            dimensions=[dimension],
            search_type=search_type,
            row_limit=fetch_limit,
        )
    except Exception as exc:  # noqa: BLE001
        return _error_text(exc, site_url)

    merged = compute_period_comparison(current_rows, previous_rows, dimension=dimension)
    total_rows = len(merged)
    merged = merged[:limit]

    if output == "json":
        return to_json({"siteUrl": target, "dimension": dimension, "rows": merged})

    body = [
        [
            truncate_text(row.get(dimension, "")),
            f"{row['clicks_current']:,.0f}",
            f"{row['clicks_previous']:,.0f}",
            f"{row['clicks_delta']:+,.0f}",
            f"{row['impressions_delta']:+,.0f}",
            f"{row['ctr_delta'] * 100:+.2f}pt",
            f"{row['position_delta']:+.1f}",
        ]
        for row in merged
    ]
    headers = [
        dimension,
        "clicks_cur",
        "clicks_prev",
        "clicks_Δ",
        "impr_Δ",
        "ctr_Δ",
        "pos_Δ",
    ]
    clicks_delta = sum(row["clicks_delta"] for row in merged)
    header_line = (
        f"プロパティ: {target} / 当期: {parse_date(current_start)}〜{parse_date(current_end)}"
        f" / 前期: {parse_date(previous_start)}〜{parse_date(previous_end)}"
        f" / dimension={dimension}"
    )
    summary = (
        f"表示 {len(merged)} 行 (突合 {total_rows} 行) / 表示中のクリック差分合計 "
        f"{clicks_delta:+,.0f} ／ pos_Δ はマイナスが順位改善"
    )
    return "\n\n".join([header_line, markdown_table(headers, body), summary])


def main() -> None:
    """コンソールスクリプト ``gsc-mcp`` のエントリポイント (stdio で待ち受ける)."""
    mcp.run()


if __name__ == "__main__":
    main()
