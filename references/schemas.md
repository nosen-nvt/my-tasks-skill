# スキーマリファレンス

## 1. データソース定義 (`datasources/{datasource_id}.json`)

1データソース1ファイル。データソースの設定と操作コマンドを定義する。

### スキーマ

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `datasource_id` | string | Yes | データソースの識別子（ファイル名と一致） |
| `type` | string | Yes | データソースの種別（`jira`, `todo`, `mail`） |
| `description` | string | Yes | データソースの説明 |
| `script` | string | Yes | 収集スクリプトのパス（リポジトリルートからの相対パス） |
| `sync_mode` | string | No | 同期モード（`"full"` or `"append"`、デフォルト: `"full"`） |
| `project_mapping` | object | No | `project_key` → `project_id` のマッピング |
| `operations` | object | No | データソース側でのタスク操作コマンド定義 |

### `sync_mode`

データソースの同期モードを指定する。

| 値 | 説明 |
|---|---|
| `"full"` | 全量同期。JSONL に含まれないタスクを「消失」として検出・削除する。JIRA・To Do などの状態型データソース向け |
| `"append"` | 追記同期。消失検出をスキップし、新規追加と既存更新のみ行う。sync 実行時に `done` タスクを GC（インデックス除去 + md 削除）する。メールなどのイベント型データソース向け |

デフォルト値は `"full"`（後方互換性）。

### `project_mapping`

JSONL の `project_key` 値（完全一致）から `projects/` 配下のプロジェクトIDへのマッピング。
タスク収集時に新規タスクの自動割り当てに使用する。

### `operations`

データソース側でタスクを操作するためのコマンド例と説明。
操作名をキーとし、`description`（説明）と `command`（実行コマンドテンプレート）を持つ。
コマンドテンプレートの `{変数名}` はエージェントが実際の値で置換して実行する。

### 例

```json
{
  "datasource_id": "jira",
  "type": "jira",
  "sync_mode": "full",
  "description": "JIRA のタスク",
  "script": "scripts/fetch-jira.sh",
  "project_mapping": {
    "UBS": "ubs-mgmt-tool",
    "DL": "data-lake"
  },
  "operations": {
    "update_status": {
      "description": "タスクのステータスを更新する",
      "command": "jira issue move {remote_id} {status}"
    }
  }
}
```

```json
{
  "datasource_id": "mail-outlook",
  "type": "mail",
  "sync_mode": "append",
  "description": "Outlook (Microsoft 365) メールからのアクションアイテム",
  "script": "scripts/fetch-mail-outlook.sh",
  "project_mapping": {},
  "operations": {
    "mark_read": {
      "description": "メールを既読にする",
      "command": "msgraph mail update --message-id {remote_id} --is-read true"
    }
  }
}
```

```json
{
  "datasource_id": "mail-gmail-nvt",
  "type": "mail",
  "sync_mode": "append",
  "description": "Gmail (nvt) メールからのアクションアイテム",
  "script": "scripts/fetch-mail-gmail.sh nvt",
  "project_mapping": {},
  "operations": {
    "mark_read": {
      "description": "メールを既読にする（google-cli modify 実装後に有効）",
      "command": "google mail modify {remote_id} --remove-label UNREAD --account nvt"
    }
  }
}
```

```json
{
  "datasource_id": "mail-gmail-qzl",
  "type": "mail",
  "sync_mode": "append",
  "description": "Gmail (qzl) メールからのアクションアイテム",
  "script": "scripts/fetch-mail-gmail.sh qzl",
  "project_mapping": {},
  "operations": {
    "mark_read": {
      "description": "メールを既読にする（google-cli modify 実装後に有効）",
      "command": "google mail modify {remote_id} --remove-label UNREAD --account qzl"
    }
  }
}
```

---

## 2. タスクインデックス (`tasks/index.jsonl`)

全データソースのタスクを統合管理する JSONL ファイル。1行1タスク。
`tasks/` ディレクトリは `.gitignore` で git 管理対象外とする。

### スキーマ

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `id` | string | Yes | タスク ID（`YYYYMMDD-NNN` 形式） |
| `remote_id` | string | No | データソース内でのタスク一意識別子 |
| `datasource_id` | string | Yes | データソースの識別子 |
| `title` | string | Yes | タスクタイトル |
| `status` | string | Yes | タスクステータス |
| `project_id` | string | No | 紐づくプロジェクトの ID |
| `run_count` | integer | No | ジョブ実行回数（デフォルト: `0`）。評価ジョブが完了時にインクリメントする |
| `generation` | integer | No | 再オープン世代（デフォルト: `1`）。done タスクが同じ remote_id で再度取り込まれた際にインクリメントする |

### ステータス定義

```
pending → reshaping ⇄ needs_input
            ↑  ↓
            │  scoped → [auto_approve / 初回は手動] → approved → running
            │                                                       ↓
            │                                                  evaluating
            │                                                 ↙    ↓    ↘
            │                                           RETRY   PASS   BLOCKED
            │                                             ↓       ↓       ↓
            └─────────────────────────────────────────────┘     done   needs_input
                                                                         ↓
                                                                     [ユーザ回答]
                                                                         ↓
                                                                     reshaping
                                                                     (ループ継続)

※ ABORT / max_runs 到達 → aborted
※ done → sync で同じ remote_id が再出現 → reshaping（再オープン、generation++）
```

| ステータス | 意味 |
|-----------|------|
| `pending` | データソースから取り込まれた初期状態 |
| `reshaping` | 精査・見直しプロセス中。初回精査（`run_count=0`）とジョブ実行後の再精査（`run_count>0`）の両方を含む |
| `needs_input` | 質問が生成されたが、未回答の項目がある。精査時（仕様の曖昧さ）と評価時（実行時ブロッカー）の両方で使用される。manual プロジェクトでは全回答後 `done` へ直接遷移 |
| `scoped` | 前提条件・達成条件が明確で、実行プロンプトが生成済み（精査ジョブが scoped 遷移時に同時生成） |
| `approved` | ユーザがプロンプトを承認し、実行待ち。オーケストレーション有効時は自動遷移も可能 |
| `running` | ディスパッチャーで実行中 |
| `done` | 完了（評価ジョブの PASS 判定、またはユーザが明示的に完了とした状態） |
| `aborted` | 実行不可能と判断された状態（ABORT 判定、または max_runs 到達） |

### 遷移ルール補足

- `pending` → `reshaping`: ユーザが精査対象として選択したとき
- `reshaping` → `scoped`: 精査ジョブで未決事項がなく達成条件が明確な場合
- `reshaping` → `needs_input`: 精査ジョブで未決事項がある場合
- `scoped` → `approved`: ユーザが承認、またはオーケストレーションによる自動承認
- `approved` → `running`: ディスパッチャーでジョブ実行開始
- `running` → 評価ジョブ: 実行ジョブ完了後、オーケストレーション有効時は自動で評価ジョブをディスパッチ
- 評価ジョブ → `done`: PASS 判定
- 評価ジョブ → `reshaping`: RETRY 判定（`run_count` をインクリメント）。オーケストレーション有効時は自動で精査ジョブをディスパッチし、ループ継続
- 評価ジョブ → `needs_input`: BLOCKED 判定（ユーザの追加情報が必要）
- 評価ジョブ → `aborted`: ABORT 判定、または `max_runs_per_generation` 到達
- `reshaping` → `done`: ユーザがタスクの完了を確認したとき（操作9）。完了時アクションも同時に実行する
- `done` → `reshaping`: 同じ remote_id のタスクが再度取り込まれたとき（再オープン、`generation` をインクリメント）
- **manual プロジェクト短縮フロー**: プロジェクト定義に `working_directory` がないプロジェクトは manual 扱い。`needs_input` で全未決事項が `[x]` になったら `scoped` / `approved` / `running` をスキップし `done` へ直接遷移する。完了時アクション（操作9）は通常通り実行する

### ID 生成規則

- 形式: `YYYYMMDD-NNN`（日付 + 当日の連番、3桁ゼロ埋め）
- 例: `20260301-001`, `20260301-002`, ...`20260301-999`
- 日をまたいだ場合、連番はリセットされる

### 例

```jsonl
{"id":"20260301-001","remote_id":"UBS-101","datasource_id":"jira","title":"API実装","status":"pending","project_id":"ubs-mgmt-tool","run_count":0,"generation":1}
{"id":"20260301-002","remote_id":"abc123","datasource_id":"ms-todo","title":"書類提出","status":"needs_input","project_id":"","run_count":0,"generation":1}
```

---

## 3. タスク実体 (`tasks/{id}.md`)

1タスク1ファイル。タスクの詳細情報を Markdown 形式で保持する。

### 構造

```markdown
# {title}

- ID: {id}
- Remote ID: {remote_id}
- Datasource: {datasource_id}
- Project: {project_id}
- Status: {status}

## 概要

（タスクの説明。sync-tasks.py による初期生成時は空）

## 未決事項

- [ ] （精査フェーズで生成される質問リスト）

## 事前条件

（タスク実行の前提条件）

## 達成条件

（タスクの完了判定基準）

## 完了時アクション

（タスク完了後に実行するアクション）

## 実行プロンプト

（scoped ステータスに遷移した際にエージェントが生成）

## 実行履歴

（ジョブ実行後に reshaping に戻す際、実行結果を追記する。run_count=0 の初期状態では空）
```

### 規約

- メタデータ部分（冒頭のリスト）はプログラムから読み書きする
- `## 未決事項` はチェックボックス形式。全て `[x]` になったら scoped への遷移が可能
- `## 実行プロンプト` は scoped 時に生成され、approved 時にディスパッチャーに渡される
- `## 実行履歴` はジョブ実行後に結果を追記する。各エントリは `### Run N` ヘッダの下に、日時・結果（成功/失敗）・終了コード・要約を記載する
- 各セクションは空でもヘッダを残す（パース容易性のため）

---

## 4. プロジェクト定義 (`projects/{project_id}.json`)

1プロジェクト1ファイル。プロジェクトの構成を定義する。

### スキーマ

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `project_id` | string | Yes | プロジェクトの識別子（ファイル名と一致） |
| `name` | string | Yes | プロジェクト名 |
| `description` | string | No | プロジェクトの説明 |
| `repositories` | array | No | 関連するリポジトリURLの配列 |
| `working_directory` | string | No | ディスパッチャーが使用する作業ディレクトリ（未設定の場合 manual プロジェクト扱い） |
| `sandbox_profile` | string | No | サンドボックスプロファイル ID（デフォルト: `"default"`）。ファイルベース（`sandbox-profiles/{id}.json`）または組み込みプロファイル（`default`, `unrestricted`）を参照 |
| `env` | object | No | サンドボックス内で設定する環境変数。値は plain string または `{"pass": "entry"}` 形式（後者は `pass show` で解決）。`.env` ファイルより低優先度 |
| `orchestration` | object | No | オーケストレーションポリシー。未設定の場合は手動フロー（従来動作） |

### 例

```json
{
  "project_id": "ubs-mgmt-tool",
  "name": "UBS管理ツール",
  "description": "データ統合基盤の開発",
  "repositories": [
    "https://github.com/example/ubs-mgmt-tool"
  ],
  "working_directory": "/home/nosen/src/github.com/example/ubs-mgmt-tool",
  "sandbox_profile": "restricted-default",
  "env": {
    "ATL_SITE": "ubs",
    "GITHUB_TOKEN": {"pass": "github/fine-grained-pat"}
  }
}
```

### `orchestration`

タスク実行の自動オーケストレーションポリシーを定義する。
未設定の場合は手動フロー（従来動作）。

| フィールド | 型 | デフォルト | 説明 |
|---|---|---|---|
| `auto_approve` | boolean | `false` | `scoped` → `approved` を自動遷移するか |
| `require_first_approval` | boolean | `true` | 初回（`run_count=0`）のみ手動承認を要求。`false` の場合は初回から自動承認 |
| `auto_retry` | boolean | `false` | 評価ジョブの RETRY 判定時に自動で再精査→再実行するか |
| `max_runs_per_generation` | integer | `5` | 1世代あたりの最大実行回数。超過時は `aborted` に遷移 |

**オーケストレーション有効時のフロー**:
1. 実行ジョブ完了 → 評価ジョブを自動ディスパッチ
2. 評価ジョブ完了 → verdict に応じて次のアクションを自動実行:
   - PASS → `done`
   - RETRY → 精査ジョブ → scoped → auto_approve → 実行ジョブ（ループ）
   - BLOCKED → `needs_input`（ユーザ介入待ち）
   - ABORT → `aborted`
3. 精査ジョブ完了 → `scoped` かつ auto_approve 条件を満たせば自動で実行ジョブをディスパッチ

```json
{
  "orchestration": {
    "auto_approve": true,
    "require_first_approval": true,
    "auto_retry": true,
    "max_runs_per_generation": 5
  }
}
```

### `env`

サンドボックス内で設定する環境変数をキーバリューで定義する。

| 値の形式 | 説明 |
|---|---|
| `"plain string"` | そのまま環境変数に設定 |
| `{"pass": "entry/path"}` | `pass show entry/path` で解決し、出力の1行目を使用 |

**優先順位**: project env < `.env` ファイル（`.env` がオーバーライド）。
ディスパッチャーは project env を先に `--env-file` で渡し、`.env` を後に渡す。
bwrap の `--setenv` は後勝ちのため、`.env` の値が優先される。


## 4.1. サンドボックスプロファイル

サンドボックスの構成（FS バインド、Proxy プロファイル、認証情報スコープ）を定義する。

### 組み込みプロファイル

コード内に定義された組み込みプロファイル。ファイルベースのプロファイルが見つからない場合に使用される。

| ID | proxy_profile | 説明 |
|---|---|---|
| `default` | `"full"` | ネットワーク保護あり。netns + proxy 経由。通常のコーディングタスク用 |
| `unrestricted` | `null` | ネットワーク保護なし。ホストネットワーク直接。ブラウザオートメーション等 |

### ファイルベースプロファイル (`sandbox-profiles/{profile_id}.json`)

### スキーマ

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `profile_id` | string | Yes | プロファイル識別子（ファイル名と一致） |
| `proxy_profile` | string or null | Yes | 参照する Proxy プロファイル ID。設定ありの場合はネットワーク保護あり（netns + proxy）。`null` の場合はホストネットワーク直接 |
| `credential_profile` | string | No | 参照する Credential プロファイル ID。ファイルベース（`credential-profiles/{id}.json`）または組み込み（`full-access`, `none`）を参照 |
| `allowed_credentials` | string or array | No | **Deprecated**: `credential_profile` を使用すること。後方互換性のため残存。直接指定した場合は `credential_profile` より優先される |
| `extra_binds` | array | No | ベースマウントに追加する bind mount のリスト |

### `extra_binds` 要素

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `source` | string | Yes | ソースパス（`$HOME` 変数使用可） |
| `target` | string | Yes | ターゲットパス（`$HOME` 変数使用可） |
| `mode` | string | No | `"rw"`（デフォルト）or `"ro"` |

### 例

```json
{
  "profile_id": "restricted-default",
  "proxy_profile": "default",
  "credential_profile": "full-access",
  "extra_binds": [
    {"source": "$HOME/.nuget", "target": "$HOME/.nuget", "mode": "rw"},
    {"source": "$HOME/.dotnet", "target": "$HOME/.dotnet", "mode": "rw"},
    {"source": "$HOME/.azcopy", "target": "$HOME/.azcopy", "mode": "rw"},
    {"source": "$HOME/.cache", "target": "$HOME/.cache", "mode": "rw"},
    {"source": "$HOME/go", "target": "$HOME/go", "mode": "rw"}
  ]
}
```

```json
{
  "profile_id": "unrestricted-browser",
  "proxy_profile": null,
  "credential_profile": "full-access",
  "extra_binds": [
    {"source": "$HOME/.local", "target": "$HOME/.local", "mode": "rw"},
    {"source": "$HOME/.claude.json", "target": "$HOME/.claude.json", "mode": "rw"},
    {"source": "$HOME/.cache", "target": "$HOME/.cache", "mode": "rw"},
    {"source": "$HOME/.volta", "target": "$HOME/.volta", "mode": "ro"},
    {"source": "/opt/google/chrome", "target": "/opt/google/chrome", "mode": "ro"},
    {"source": "$HOME/go", "target": "$HOME/go", "mode": "rw"}
  ]
}
```


## 4.2. クレデンシャルプロファイル

サンドボックス内のジョブがアクセス可能な認証情報のスコープを定義する。
サンドボックスプロファイルの `credential_profile` フィールドから ID で参照される。

### 組み込みプロファイル

| ID | allowed_credentials | 説明 |
|---|---|---|
| `full-access` | `"*"` | 全 pass エントリ許可 |
| `none` | `[]` | アクセス不可 |

### ファイルベースプロファイル (`credential-profiles/{credential_profile_id}.json`)

### スキーマ

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `credential_profile_id` | string | Yes | プロファイル識別子（ファイル名と一致） |
| `description` | string | No | プロファイルの説明 |
| `allowed_credentials` | string or array | Yes | `"*"`（全 pass エントリ許可）or エントリパスの配列 |

### 解決優先順位

サンドボックスプロファイルの `allowed_credentials` 解決は以下の優先順位で行われる:

1. `allowed_credentials` が直接存在 → そのまま使用（後方互換）
2. `credential_profile` が指定 → credential profile を読み込んで `allowed_credentials` を返す
3. どちらも未指定 → `"*"`（デフォルト全許可）

### 例

```json
{
  "credential_profile_id": "dev-tools",
  "description": "開発ツール用クレデンシャル",
  "allowed_credentials": [
    "jira/api-token",
    "bitbucket/app-password",
    "azure/devops-pat"
  ]
}
```

---

## 5. 収集 JSONL スキーマ

収集スクリプト（`scripts/fetch-*.sh`）が stdout に出力する形式。
1行1タスクの JSONL（JSON Lines）形式。

### フィールド定義

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `datasource_id` | string | Yes | データソースの識別子 |
| `remote_id` | string | Yes | データソース内でのタスク一意識別子 |
| `title` | string | Yes | タスクタイトル |
| `status` | string | No | 収集スクリプトが出力してもよいが、sync-tasks.py では無視する（保存しない） |
| `due_date` | string | No | 期日（YYYY-MM-DD） |
| `url` | string | No | タスクのURL |
| `project_key` | string | No | データソース内のプロジェクト識別子（自動振り分けに使用） |

### 例

```jsonl
{"datasource_id":"jira","remote_id":"UBS-101","title":"API実装","project_key":"UBS","url":"https://jira.example.com/browse/UBS-101","due_date":"2026-03-15"}
{"datasource_id":"jira","remote_id":"UBS-102","title":"認証機能の追加","project_key":"UBS","url":"https://jira.example.com/browse/UBS-102","due_date":"2026-03-20"}
{"datasource_id":"ms-todo","remote_id":"abc123","title":"書類提出","project_key":"個人タスク"}
```

### 規約

- 収集スクリプトは完了済みタスクを出力しない（未完了タスクのみ出力）
- `sync_mode=full` のデータソース: JSONL に含まれないが `tasks/index.jsonl` に存在するタスクは「消失タスク」として扱い、インデックスと Markdown ファイルから削除する
- `sync_mode=append` のデータソース: 消失検出をスキップし、新規追加と既存更新のみ行う。sync 実行時に `done` タスクを GC する
- フィールドが存在しない場合はデフォルト値を使用（`null`）

---

## 6. ディスパッチャージョブ状態（インメモリ）

ディスパッチャーサーバがインメモリで管理するジョブ状態。
ファイルへの永続化は行わない（サーバ再起動時にリセット）。
ソケット: `$XDG_RUNTIME_DIR/my-tasks-dispatch/dispatcher.sock`

### ジョブフィールド

| フィールド | 型 | 説明 |
|---|---|---|
| `dispatch_id` | string | 識別子（`{project_id}-{連番}` 形式、例: `bo-1`） |
| `project_id` | string | プロジェクト ID |
| `task_id` | string\|null | タスク ID（`run --task` の場合） |
| `status` | string | `queued` \| `running` \| `done` \| `failed` |
| `pid` | integer\|null | 子プロセスの PID |
| `exit_code` | integer\|null | 終了コード |
| `started_at` | string\|null | 開始日時（ISO 8601形式） |
| `finished_at` | string\|null | 終了日時（ISO 8601形式） |

### status レスポンス例

```json
{"ok": true, "jobs": [
  {"dispatch_id": "bo-1", "project_id": "bo", "task_id": "20260301-001", "status": "running", "pid": 12345, "exit_code": null, "started_at": "2026-03-01T10:00:00+09:00", "finished_at": null},
  {"dispatch_id": "bo-2", "project_id": "bo", "task_id": null, "status": "queued", "pid": null, "exit_code": null, "started_at": null, "finished_at": null}
]}
```
