# gsc-mcp — Google Search Console MCP サーバー

Claude Code から Google Search Console (GSC) の検索パフォーマンスデータを直接クエリするための MCP サーバーです。
サービスアカウント認証で動くため、**対話的な OAuth 同意画面は不要で、一度設定すれば恒久的に使えます**（GA の設定と同じスタイル）。

主目的は **タイトル / メタディスクリプションの CTR 改善の優先順位付け** です。
`ctr_opportunities` ツールが「表示回数は多いのに、その掲載順位で期待される CTR に届いていない」ページを、
改善で得られるクリック数の見込み順に並べてくれます。そのままタイトル書き換えの作業リストになります。

---

## 1. セットアップ

### 1.1 インストール

```bash
cd /home/user/ai_promote

# uv を使う場合
uv venv
uv pip install -e ".[dev]"

# pip でも可
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

`gsc-mcp` というコンソールスクリプトが入ります（`python -m gsc_mcp` でも起動できます）。

### 1.2 GCP 側の準備

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作成（既存のものでも可）。
2. **「API とサービス」→「ライブラリ」** で **Google Search Console API** を検索し、**有効にする**。
3. **「API とサービス」→「認証情報」→「認証情報を作成」→「サービスアカウント」** でサービスアカウントを作成。
   ロールの付与は不要です（GSC 側の権限で制御するため）。
4. 作成したサービスアカウントの **「キー」タブ →「鍵を追加」→「新しい鍵を作成」→ JSON** を選び、
   JSON 鍵ファイルをダウンロードしてローカルの安全な場所に置く。
   （リポジトリ内に置く場合は `service-account.json` などの名前にすれば `.gitignore` 済みです）

### 1.3 ⚠️ 最重要: GSC 側でサービスアカウントをユーザー追加する

**GSC 特有の落とし穴です。ここを忘れると、API を有効化しても全ての呼び出しが 403 になります。**

ダウンロードした JSON の中にある `client_email`（例: `gsc-reader@your-project.iam.gserviceaccount.com`）を、

**Search Console → 対象プロパティを選択 → 左下の「設定」→「ユーザーと権限」→「ユーザーを追加」**

で追加してください。権限は **「制限付き」で十分**です（読み取りしかしないため）。「フル」でも構いません。

> GCP でサービスアカウントを作っただけでは、そのアカウントはあなたのサイトのデータにアクセスできません。
> GSC のプロパティごとに、人間のユーザーと同じ手順で明示的に招待する必要があります。
> 複数プロパティを扱う場合は、**プロパティごとに**この追加作業が必要です。

設定できたかどうかは `list_sites` ツールで確認できます。対象プロパティが一覧に出てくれば成功です。

### 1.4 環境変数

`.env.example` をコピーして使ってください。

| 環境変数 | 必須 | 説明 |
| --- | --- | --- |
| `GSC_SERVICE_ACCOUNT_FILE` | ○ | サービスアカウント JSON 鍵の絶対パス。未設定の場合は `GOOGLE_APPLICATION_CREDENTIALS` にフォールバックします。 |
| `GSC_SITE_URL` | △ | 既定で参照するプロパティ。各ツールの `site_url` 引数で個別に上書き可能です。 |

`GSC_SITE_URL` の書式は 2 種類あり、**完全一致**である必要があります。

- ドメインプロパティ: `sc-domain:example.com`
- URL プレフィックス: `https://example.com/` ← **末尾のスラッシュまで含める**

### 1.5 Claude Code への登録

リポジトリ直下の `.mcp.json` を編集し、パスとプロパティを実際の値に書き換えるだけです（このファイルにシークレットそのものは書きません。書くのは鍵ファイルの**パス**です）。

```json
{
  "mcpServers": {
    "gsc": {
      "command": "gsc-mcp",
      "args": [],
      "env": {
        "GSC_SERVICE_ACCOUNT_FILE": "/absolute/path/to/service-account.json",
        "GSC_SITE_URL": "sc-domain:example.com"
      }
    }
  }
}
```

`gsc-mcp` が PATH 上に無い場合（プロジェクト内の venv に入れた場合など）は、`command` を絶対パスにしてください。

```json
"command": "/home/user/ai_promote/.venv/bin/gsc-mcp"
```

CLI から登録することもできます。

```bash
claude mcp add gsc \
  --env GSC_SERVICE_ACCOUNT_FILE=/absolute/path/to/service-account.json \
  --env GSC_SITE_URL=sc-domain:example.com \
  -- /home/user/ai_promote/.venv/bin/gsc-mcp
```

登録後、Claude Code で **`/mcp`** を実行し、`gsc` が `connected` になっていること、ツールが 6 つ見えることを確認してください。
うまく繋がったら、まず「`list_sites` でアクセスできるプロパティを見せて」と頼むのが確実な疎通確認です。

---

## 2. ツール一覧

| ツール | 用途 | 主な引数 |
| --- | --- | --- |
| `list_sites` | アクセス可能なプロパティ一覧。**セットアップ確認用** | なし |
| `search_analytics` | 汎用クエリ（万能の逃げ道）。国別・デバイス別・日次推移など | `start_date`, `end_date`, `dimensions`, `search_type`, `row_limit`, `dimension_filters`, `data_state`, `output` |
| `top_queries` | 表示回数の多い検索クエリ上位 | `start_date`, `end_date`, `row_limit`, `page`, `output` |
| `top_pages` | 表示回数の多いページ上位 | `start_date`, `end_date`, `row_limit`, `output` |
| `ctr_opportunities` | **中核。タイトル改善の優先順位リスト** | `start_date`, `end_date`, `dimension`, `min_impressions`, `max_position`, `row_limit`, `baseline`, `output` |
| `compare_periods` | 2 期間の差分。施策の効果検証 | `current_start`, `current_end`, `previous_start`, `previous_end`, `dimension`, `row_limit`, `output` |

すべてのツールに共通:

- `site_url` — 省略時は `GSC_SITE_URL`。
- `output` — `table`（既定、markdown テーブル。トークン節約のため）か `json`（生データ）。分析系ツールのみ。
- 日付は `YYYY-MM-DD` のほか、`today` / `yesterday` / `7daysAgo` / `28daysAgo` / `90daysAgo` / `16monthsAgo` といった相対指定が使えます。
- `row_limit` は API 仕様の上限 **25,000 行**で頭打ちになります（ページングは行いません）。

`dimensions` に指定できるのは `query` / `page` / `country` / `device` / `date` / `searchAppearance` です。

### `dimension_filters` の書き方（`search_analytics`）

```json
[{"dimension": "page", "operator": "contains", "expression": "/blog/"}]
```

`operator` は `equals` / `notEquals` / `contains` / `notContains` / `includingRegex` / `excludingRegex`。

---

## 3. タイトル CTR 改善の使い方

### 考え方

`ctr_opportunities` は各行について次を計算します。

```
potential_clicks = impressions × max(0, 期待CTR − 実CTR)
```

つまり **「掲載順位を 1 ミリも動かさずに、CTR だけを平均水準まで戻せたら増えるクリック数」** の概算です。
順位改善には時間がかかりますが、タイトルとディスクリプションの書き換えは今日できて、効果も数日〜数週間で出ます。
この降順が、そのまま費用対効果の高い作業順になります。

### 期待 CTR ベースラインについて（重要）

期待 CTR は、掲載順位 1〜20 位に対する CTR の目安テーブル（整数位の間は線形補間）から求めています。
このテーブルは `src/gsc_mcp/analysis.py` の `DEFAULT_CTR_BASELINE` にあり、およそ 1 位 28% から 20 位 1% へ減衰する形です。

**これは GSC から取得した実データではなく、公開されている各種 CTR 調査をならしたヒューリスティックな曲線です。**
実際のカーブはサイトによって大きく変わります（指名検索が多い、強調スニペットやショッピング枠に食われている、など）。
自サイトの実測カーブがある場合は `baseline` 引数（1 位から順に並べた小数の配列）で上書きしてください。

```
ctr_opportunities を baseline=[0.31, 0.18, 0.11, 0.08, 0.06, ...] で実行して
```

### 実際の流れ

**1. 改善候補を出す**

```
過去 28 日の ctr_opportunities を出して
```

出力例:

```
| page                                      | impressions | clicks | ctr   | position | expected_ctr | ctr_gap | potential_clicks |
|-------------------------------------------|-------------|--------|-------|----------|--------------|---------|------------------|
| https://example.com/blog/ai-writing-tools | 4,200       | 12     | 0.29% | 3.4      | 8.80%        | 8.51%   | 357.6            |
| https://example.com/blog/seo-title        | 1,500       | 140    | 9.33% | 2.1      | 14.95%       | 5.62%   | 84.3             |
```

1 行目は「3.4 位に表示されているのにほとんどクリックされていない」= タイトルが検索意図とズレている典型です。
ここを直すだけで月 350 クリック程度の見込みがある、と読みます。

**2. そのページが実際に拾っている検索語を確認する**

```
そのページの top_queries を page="/blog/ai-writing-tools" で出して
```

タイトルに入れるべき語（ユーザーが実際に打ち込んでいる語）が分かります。
ここで初めて、根拠のあるタイトル案が書けます。

**3. タイトル / メタディスクリプションを書き換える**

**4. 2〜4 週間後に効果を検証する**

```
compare_periods で書き換え前後を比べて
（前: 2026-06-01〜2026-06-28、後: 2026-07-01〜2026-07-28）
```

`ctr_Δ` がプラスなら成功です。`pos_Δ` は**マイナスが順位改善**を意味します（順位は数字が小さいほど上位のため）。
CTR が上がると順位も後追いで上がることがあるので、両方を見てください。

### 絞り込みのコツ

- `min_impressions`（既定 100）: 表示回数が少ない行は CTR がブレるため除外しています。サイト規模が小さければ 30〜50 に下げてください。
- `max_position`（既定 20.0）: 2 ページ目以下は CTR ではなく順位そのものの問題なので、既定では切っています。
- `dimension="query"`: ページ単位ではなくクエリ単位で見たいとき。「この語では表示されているのに全く選ばれていない」を発見できます。

---

## 4. GSC データの制約

- **データは 2〜3 日遅れます。** 昨日や今日のデータは未確定か、まだ存在しません。既定の `end_date` は `yesterday` にしていますが、直近を見たい場合は `data_state="all"`（未確定値を含む）を指定してください。
- **保持期間は約 16 ヶ月**です。それより前のデータは取得できません（`16monthsAgo` が実質の下限）。
- 表示回数の少ない行や、個人特定につながりうるクエリは Google 側で間引かれるため、ツールの合計値と GSC 画面上の数値は完全には一致しません。
- クエリ × ページのように複数ディメンションを組み合わせると、間引きの影響で行の合計が全体値より小さくなります。

---

## 5. 開発

```bash
.venv/bin/python -m pytest -q
```

テストはネットワークも認証情報も使いません。日付ショートハンドの解釈、期待 CTR の補間、
`ctr_opportunities` のスコアリングと並び順を、`src/gsc_mcp/analysis.py` の純粋関数に対して検証しています。

構成:

```
src/gsc_mcp/
  server.py      MCP サーバー本体とツール定義 (FastMCP / stdio)
  analysis.py    純粋な計算ロジック（日付解釈・期待CTR・スコアリング・期間比較）
  auth.py        サービスアカウント認証とクライアントの遅延生成
  formatting.py  markdown テーブル整形
tests/test_server.py
```

認証クライアントは**最初のツール呼び出し時に遅延生成**されます。
そのため認証情報が無くてもサーバーは起動し、`/mcp` でのツール一覧表示は成功します。
認証情報が無い状態でツールを呼ぶと、何を設定すべきかを説明したエラーメッセージが返ります。

### セキュリティ

`.gitignore` で `credentials*.json` / `service-account*.json` / `*-sa.json` / `secrets/` / `.env` を除外しています。
**サービスアカウントの JSON 鍵は絶対にコミットしないでください。** `.mcp.json` に書くのは鍵ファイルのパスだけです。
