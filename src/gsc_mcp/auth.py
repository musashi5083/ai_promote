"""サービスアカウント認証と Search Console API クライアントの生成.

インタラクティブな OAuth は使わない。サービスアカウントの JSON 鍵だけで
恒久的に動作させる。クライアントは最初のツール呼び出し時に遅延生成するため、
認証情報が無くてもサーバー起動と ``tools/list`` は成功する。
"""

from __future__ import annotations

import os
from typing import Any, Optional

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]

SERVICE_ACCOUNT_ENV = "GSC_SERVICE_ACCOUNT_FILE"
FALLBACK_ENV = "GOOGLE_APPLICATION_CREDENTIALS"
SITE_URL_ENV = "GSC_SITE_URL"

_client: Optional[Any] = None


class GSCAuthError(RuntimeError):
    """認証情報が見つからない / 不正な場合に送出する (メッセージはそのまま利用者に返す)."""


def credentials_path() -> Optional[str]:
    """使用するサービスアカウント JSON のパスを返す (未設定なら None)."""
    return os.environ.get(SERVICE_ACCOUNT_ENV) or os.environ.get(FALLBACK_ENV) or None


def default_site_url() -> Optional[str]:
    """環境変数で指定された既定プロパティ URL を返す."""
    value = os.environ.get(SITE_URL_ENV)
    return value.strip() if value and value.strip() else None


def resolve_site_url(site_url: Optional[str] = None) -> str:
    """引数 > 環境変数の優先順で対象プロパティを決定する.

    Raises:
        GSCAuthError: どちらも未指定の場合。
    """
    resolved = (site_url or "").strip() or default_site_url()
    if not resolved:
        raise GSCAuthError(
            "対象プロパティが指定されていません。\n"
            f"環境変数 {SITE_URL_ENV} に設定するか、ツールの site_url 引数で指定してください。\n"
            "例) sc-domain:example.com  または  https://example.com/\n"
            "利用可能なプロパティは list_sites ツールで確認できます。"
        )
    return resolved


def _auth_help(detail: str = "") -> str:
    """認証まわりの失敗時に返す、そのまま読める案内文."""
    lines = [
        "Search Console API の認証に失敗しました。",
        "",
        "確認してください:",
        f"1. 環境変数 {SERVICE_ACCOUNT_ENV} (未設定なら {FALLBACK_ENV}) に、",
        "   サービスアカウントの JSON 鍵ファイルの絶対パスが設定されているか。",
        "2. そのファイルが実際に存在し、読み取り可能な正しい JSON か。",
        "3. GCP プロジェクトで「Google Search Console API」が有効化されているか。",
        "4. サービスアカウントのメールアドレス (JSON 内の client_email。",
        "   例: xxxx@your-project.iam.gserviceaccount.com) を、Search Console の",
        "   対象プロパティの [設定] → [ユーザーと権限] に「制限付き」以上の権限で",
        "   追加しているか。これを忘れると全ての呼び出しが 403 になります。",
    ]
    if detail:
        lines += ["", f"詳細: {detail}"]
    return "\n".join(lines)


def get_client(force_reload: bool = False) -> Any:
    """Search Console API クライアントを遅延生成して返す (プロセス内でキャッシュ).

    Args:
        force_reload: True なら既存のキャッシュを破棄して作り直す。

    Raises:
        GSCAuthError: 認証情報が見つからない、または不正な場合。
            メッセージはそのままユーザーに提示できる日本語の案内文。
    """
    global _client
    if _client is not None and not force_reload:
        return _client

    path = credentials_path()
    if not path:
        raise GSCAuthError(
            _auth_help(
                f"{SERVICE_ACCOUNT_ENV} も {FALLBACK_ENV} も設定されていません。"
            )
        )
    if not os.path.isfile(path):
        raise GSCAuthError(_auth_help(f"指定されたファイルが見つかりません: {path}"))

    try:
        from google.oauth2 import service_account  # 遅延 import
        from googleapiclient.discovery import build

        credentials = service_account.Credentials.from_service_account_file(
            path, scopes=SCOPES
        )
        _client = build(
            "searchconsole", "v1", credentials=credentials, cache_discovery=False
        )
    except GSCAuthError:
        raise
    except Exception as exc:  # noqa: BLE001 - 利用者向けに整形して返す
        raise GSCAuthError(_auth_help(f"{type(exc).__name__}: {exc}")) from exc

    return _client


def describe_api_error(exc: Exception, site_url: Optional[str] = None) -> str:
    """API 呼び出しの例外を、利用者がそのまま読める日本語メッセージに変換する."""
    status: Optional[int] = None
    resp = getattr(exc, "resp", None)
    if resp is not None:
        try:
            status = int(getattr(resp, "status", 0)) or None
        except (TypeError, ValueError):
            status = None

    target = f"（対象プロパティ: {site_url}）" if site_url else ""

    if status == 403:
        return (
            f"Search Console API から 403 (権限なし) が返りました{target}。\n"
            "サービスアカウントのメールアドレス (JSON 内の client_email) を、"
            "Search Console の [設定] → [ユーザーと権限] で対象プロパティに"
            "「制限付き」以上の権限で追加してください。\n"
            "また GCP プロジェクトで「Google Search Console API」が有効か確認してください。\n"
            f"詳細: {exc}"
        )
    if status == 404:
        return (
            f"指定されたプロパティが見つかりません{target}。\n"
            "ドメインプロパティは 'sc-domain:example.com'、URL プレフィックスは "
            "'https://example.com/' のように末尾スラッシュまで正確に指定してください。\n"
            "list_sites ツールでアクセス可能なプロパティ一覧を確認できます。\n"
            f"詳細: {exc}"
        )
    if status == 429:
        return f"APIのレート制限に達しました。時間をおいて再実行してください。\n詳細: {exc}"

    return f"Search Console API の呼び出しに失敗しました{target}。\n詳細: {type(exc).__name__}: {exc}"
