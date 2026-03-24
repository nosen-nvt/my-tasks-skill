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
| `site_mapping` | object | No | `project_key` → サイト名のマッピング（Jira 用。フィードバック収集時にサイトを特定するために使用） |
| `operations` | object | No | データソース側でのタスク操作コマンド定義 |

### `sync_mode`

データソースの同期モードを指定する。

| 値 | 説明 |
|---|---|
| `"full"` | 全量同期。JSONL に含まれないタスクを「消失」として検出・削除する。JIRA・To Do などの状態型データソース向け |
| `"append"` | 追記同期。消失検出をスキップし、新規追加と既存更新のみ行う。sync 実行時に `done` タスクを GC（インデックス除去 + YAML 削除）する。メールなどのイベント型データソース向け |

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
      "command": "msgraph patch \"/me/messages/{remote_id}\" --body '{\"isRead\": true}'"
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
      "description": "メールを既読にする",
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
      "description": "メールを既読にする",
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

### ステータス定義

```
pending → in_progress → done
                      → aborted
```

| ステータス | 意味 |
|-----------|------|
| `pending` | データソースから取り込まれた初期状態、または Plan/Dispatch 待ち |
| `in_progress` | ジョブ実行中、Plan 中、Feedback 待ちなど、作業が進行中の状態 |
| `done` | 完了 |
| `aborted` | 中止 |

### 遷移ルール

- Dispatch 時: `pending` → `in_progress`
- ジョブ完了・手動完了: `in_progress` → `done`
- 中止: `in_progress` → `aborted`
- 再オープン: done タスクと同じ `remote_id` が再出現 → 新規タスクとして作成

### ID 生成規則

- 形式: `YYYYMMDD-NNN`（日付 + 当日の連番、3桁ゼロ埋め）
- 例: `20260301-001`, `20260301-002`, ...`20260301-999`
- 日をまたいだ場合、連番はリセットされる

### 例

```jsonl
{"id":"20260301-001","remote_id":"UBS-101","datasource_id":"jira","title":"API実装","status":"pending","project_id":"ubs-mgmt-tool"}
{"id":"20260301-002","remote_id":"abc123","datasource_id":"ms-todo","title":"書類提出","status":"in_progress","project_id":""}
```

---

## 3. タスク実体 (`tasks/{id}.yaml`)

1タスク1ファイル。タスクの詳細情報を YAML 形式で保持する。
タスク YAML が唯一の真実の源（Single Source of Truth）。

### スキーマ

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `id` | string | Yes | タスク ID（`YYYYMMDD-NNN` 形式） |
| `remote_id` | string | No | データソース内でのタスク一意識別子 |
| `datasource_id` | string | Yes | データソースの識別子 |
| `project_id` | string | No | 紐づくプロジェクトの ID |
| `title` | string | Yes | タスクタイトル |
| `status` | string | Yes | タスクステータス |
| `description` | string | No | タスクの説明（Plan で精査） |
| `preconditions` | array | No | タスク実行の事前条件リスト |
| `acceptance_criteria` | array | No | タスクの達成条件リスト |
| `completion_actions` | array | No | タスク完了後に実行するアクションリスト |
| `execute_prompt` | string | No | 実行プロンプト（Plan で生成） |
| `pr_url` | string | No | PR の URL |
| `branch` | string | No | 作業ブランチ名 |
| `dispatch_id` | string | No | 現在のジョブの dispatch_id |
| `session_id` | string | No | Claude Code セッション ID（UUID 形式。Resume で再開に使用） |
| `feedback` | array | No | フィードバック（グループ化、下記参照） |
| `feedback_cursor` | object | No | ソースごとの最終取得タイムスタンプ（重複取得を防ぐ） |
| `history` | array | No | 実行履歴 |

### `feedback` 構造（FeedbackGroup）

フィードバックは収集タイミングごとにグループ化する。

| フィールド | 型 | 説明 |
|---|---|---|
| `collected_at` | string | 収集実行時のタイムスタンプ（ISO 8601）。グループ識別子として機能 |
| `items` | array | フィードバック項目の配列 |

### `feedback.items` 要素（FeedbackItem）

| フィールド | 型 | 説明 |
|---|---|---|
| `source` | string | フィードバックソース: `github_pr` \| `github_pr_review` \| `jira_comment` \| `bitbucket_pr` \| `user` |
| `author` | string | フィードバックの著者（`user` ソースの場合は省略可） |
| `timestamp` | string | フィードバックの日時（ISO 8601） |
| `body` | string | フィードバックの内容 |

### `feedback_cursor`

| フィールド | 型 | 説明 |
|---|---|---|
| `jira_comment` | string | Jira コメントの最終取得タイムスタンプ |
| `bitbucket_pr` | string | Bitbucket PR コメントの最終取得タイムスタンプ |
| `github_pr` | string | GitHub PR コメントの最終取得タイムスタンプ |

### `history` 要素

| フィールド | 型 | 説明 |
|---|---|---|
| `dispatch_id` | string | ジョブの dispatch_id |
| `started_at` | string | 開始日時（ISO 8601） |
| `finished_at` | string | 終了日時（ISO 8601） |
| `exit_code` | integer | 終了コード |
| `summary` | string | 実行サマリ |

### 例

```yaml
id: "20260301-001"
remote_id: "UBS-101"
datasource_id: jira
project_id: ubs-mgmt-tool
title: API実装
status: pending
description: ""
preconditions: []
acceptance_criteria: []
completion_actions: []
execute_prompt: ""
pr_url: ""
branch: ""
dispatch_id: ""
session_id: ""
feedback: []
feedback_cursor: {}
history: []
```

### Plan 後の例

```yaml
id: "20260301-001"
remote_id: "UBS-101"
datasource_id: jira
project_id: ubs-mgmt-tool
title: API実装
status: pending
description: |
  API エンドポイントの実装。CRUD 操作を提供する。
preconditions:
  - データベーススキーマが定義済みであること
acceptance_criteria:
  - API が正常にレスポンスを返すこと
  - dotnet test が全件パスすること
completion_actions:
  - jira issue move UBS-101 "Done" --site urbanb
execute_prompt: |
  UBS管理ツールの API エンドポイントを実装してください。
  ...
pr_url: ""
branch: ""
dispatch_id: ""
session_id: ""
feedback: []
feedback_cursor: {}
history: []
```

### Dispatch 後（Feedback 収集済み）の例

```yaml
id: "20260301-001"
remote_id: "UBS-101"
datasource_id: jira
project_id: ubs-mgmt-tool
title: API実装
status: in_progress
description: |
  API エンドポイントの実装。CRUD 操作を提供する。
preconditions:
  - データベーススキーマが定義済みであること
acceptance_criteria:
  - API が正常にレスポンスを返すこと
  - dotnet test が全件パスすること
completion_actions:
  - jira issue move UBS-101 "Done" --site urbanb
execute_prompt: |
  UBS管理ツールの API エンドポイントを実装してください。
  ...
pr_url: "https://github.com/example/ubs-mgmt-tool/pull/42"
branch: "task/20260301-001"
dispatch_id: "ubs-mgmt-tool-3"
session_id: "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
feedback:
  - collected_at: "2026-03-24T10:30:00+09:00"
    items:
      - source: "github_pr"
        author: "reviewer-a"
        timestamp: "2026-03-24T10:25:00+09:00"
        body: "エラーハンドリングが不足しています"
      - source: "github_pr_review"
        author: "reviewer-b"
        timestamp: "2026-03-24T10:28:00+09:00"
        body: "[CHANGES_REQUESTED] テストケースを追加してください"
  - collected_at: "2026-03-24T15:00:00+09:00"
    items:
      - source: "github_pr"
        author: "reviewer-a"
        timestamp: "2026-03-24T14:55:00+09:00"
        body: "まだこの部分が..."
feedback_cursor:
  github_pr: "2026-03-24T14:55:00+09:00"
history:
  - dispatch_id: "ubs-mgmt-tool-3"
    started_at: "2026-03-24T10:00:00+09:00"
    finished_at: "2026-03-24T10:15:00+09:00"
    exit_code: 0
    summary: "API エンドポイント実装完了、PR作成済み"
  - dispatch_id: "ubs-mgmt-tool-5"
    started_at: "2026-03-24T16:00:00+09:00"
    finished_at: "2026-03-24T16:10:00+09:00"
    exit_code: 0
    summary: "レビュー指摘対応（fb-20260324T1500）"
```

### 規約

- 全フィールドはプログラムから読み書きする（`yaml.safe_load` / `yaml.dump`）
- `execute_prompt` は Plan（対話セッション）で生成し、Dispatch 時にディスパッチャーに渡される
- `history` はジョブ完了後に結果を追記する

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
| `extra_binds` | array | No | プロジェクト固有の追加 bind mount。サンドボックスプロファイルの `extra_binds` に追加される |
| `host_commands` | array | No | プロジェクト固有の追加ホストコマンド。サンドボックスプロファイルの `host_commands` に追加される |

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
  },
  "extra_binds": [
    {"source": "$HOME/.project-cache", "target": "$HOME/.project-cache", "mode": "rw"}
  ],
  "host_commands": [
    {"name": "az", "path": "/usr/bin/az", "allowed_patterns": ["pipelines run *", "pipelines runs show *"]}
  ]
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

### `extra_binds`

プロジェクト固有の追加 bind mount を定義する。サンドボックスプロファイルの `extra_binds` の**後**に追加される（プロファイル + プロジェクトのマージ）。要素のスキーマはサンドボックスプロファイルの `extra_binds` と同一。

### `host_commands`

プロジェクト固有の追加ホストコマンドを定義する。サンドボックスプロファイルの `host_commands` の**後**に追加される（プロファイル + プロジェクトのマージ）。要素のスキーマはサンドボックスプロファイルの `host_commands` と同一。


## 4.1. サンドボックスプロファイル

サンドボックスの構成（FS バインド、Proxy プロファイル、ホストコマンド）を定義する。

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
| `host_commands` | array | No | ホスト側で実行可能なコマンドのリスト。サンドボックス内の `/usr/bin/{name}` に host-cmd シムが bind される |
| `extra_binds` | array | No | ベースマウントに追加する bind mount のリスト |

### `host_commands` 要素

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `name` | string | Yes | コマンド名（サンドボックス内の `/usr/bin/{name}` に bind される） |
| `path` | string | Yes | ホスト側のコマンドパス（例: `/usr/bin/pass`） |
| `allowed_patterns` | string or array | No | `"*"`（全引数許可）or fnmatch パターンの配列（例: `["show jira/*"]`） |
| `allow_stdin` | boolean | No | stdin 入力を許可するか（デフォルト: `false`） |

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
  "host_commands": [
    {"name": "pass", "path": "/usr/bin/pass", "allowed_patterns": "*", "allow_stdin": true}
  ],
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
  "host_commands": [
    {"name": "pass", "path": "/usr/bin/pass", "allowed_patterns": "*", "allow_stdin": true}
  ],
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
- `sync_mode=full` のデータソース: JSONL に含まれないが `tasks/index.jsonl` に存在するタスクは「消失タスク」として扱い、インデックスと YAML ファイルから削除する
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
| `status` | string | `queued` \| `running` \| `done` \| `failed` |
| `pid` | integer\|null | 子プロセスの PID |
| `exit_code` | integer\|null | 終了コード |
| `working_dir` | string | 作業ディレクトリ（worktree パス） |
| `branch` | string\|null | worktree のブランチ名 |
| `started_at` | string\|null | 開始日時（ISO 8601形式） |
| `finished_at` | string\|null | 終了日時（ISO 8601形式） |

### status レスポンス例

```json
{"ok": true, "jobs": [
  {"dispatch_id": "bo-1", "project_id": "bo", "status": "running", "pid": 12345, "exit_code": null, "working_dir": "/tmp/wt/bo-1", "branch": "task/20260301-001", "started_at": "2026-03-01T10:00:00+09:00", "finished_at": null},
  {"dispatch_id": "bo-2", "project_id": "bo", "status": "queued", "pid": null, "exit_code": null, "working_dir": "", "branch": null, "started_at": null, "finished_at": null}
]}
```
