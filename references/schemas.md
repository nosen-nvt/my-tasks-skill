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
| `lifecycle_id` | string | No | 関連する Lifecycle ID（dispatch 済みの場合） |
| `run_count` | integer | No | 実行回数（デフォルト: `0`）。sync-tasks.py が初期値を設定 |
| `generation` | integer | No | 再オープン世代（デフォルト: `1`）。done タスクが同じ remote_id で再度取り込まれた際にインクリメントする |

### ステータス定義

```
pending → in_progress → done
                      → aborted

※ done → sync で同じ remote_id が再出現 → pending（再オープン、generation++）
```

| ステータス | 意味 |
|-----------|------|
| `pending` | データソースから取り込まれた初期状態 |
| `in_progress` | Lifecycle にディスパッチ済み（Lifecycle 側の suspend 中も含む） |
| `done` | 完了 |
| `aborted` | 中止 |

### 遷移ルール補足

- dispatch 時: `pending` → `in_progress`
- Lifecycle done(DONE): `in_progress` → `done`
- Lifecycle done(ABORT/max_runs): `in_progress` → `aborted`
- `done` → `pending`: 同じ remote_id のタスクが再度取り込まれたとき（再オープン、`generation` をインクリメント）
- **manual プロジェクト**: プロジェクト定義に `working_directory` がないプロジェクトは manual 扱い。Lifecycle を経由せず、メインセッションで直接処理する。完了時アクション（操作9）は通常通り実行する

### ID 生成規則

- 形式: `YYYYMMDD-NNN`（日付 + 当日の連番、3桁ゼロ埋め）
- 例: `20260301-001`, `20260301-002`, ...`20260301-999`
- 日をまたいだ場合、連番はリセットされる

### 例

```jsonl
{"id":"20260301-001","remote_id":"UBS-101","datasource_id":"jira","title":"API実装","status":"pending","project_id":"ubs-mgmt-tool","generation":1}
{"id":"20260301-002","remote_id":"abc123","datasource_id":"ms-todo","title":"書類提出","status":"in_progress","project_id":"","generation":1}
```

---

## 3. タスク実体 (`tasks/{id}.yaml`)

1タスク1ファイル。タスクの詳細情報を YAML 形式で保持する。

### スキーマ

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `id` | string | Yes | タスク ID（`YYYYMMDD-NNN` 形式） |
| `remote_id` | string | No | データソース内でのタスク一意識別子 |
| `datasource_id` | string | Yes | データソースの識別子 |
| `project_id` | string | No | 紐づくプロジェクトの ID |
| `title` | string | Yes | タスクタイトル |
| `status` | string | Yes | タスクステータス |
| `generation` | integer | No | 再オープン世代（デフォルト: `1`） |
| `description` | string | No | タスクの説明（sync-tasks.py による初期生成時は空） |
| `open_questions` | array | No | 未決事項リスト（精査フェーズで生成） |
| `preconditions` | array | No | タスク実行の事前条件リスト |
| `acceptance_criteria` | array | No | タスクの達成条件リスト |
| `completion_actions` | array | No | タスク完了後に実行するアクションリスト |
| `execute_prompt` | string | No | 計画ジョブが生成する実行プロンプト |
| `history` | array | No | 実行履歴（再オープン時に前世代サマリを追加） |
| `feedback` | array | No | フィードバック項目リスト（Jira コメント、PR コメント、ユーザー入力） |
| `feedback_cursor` | object | No | ソースごとの最終取得タイムスタンプ（重複取得を防ぐ） |
| `pr_url` | string | No | 関連する Bitbucket PR の URL（PR フィードバック収集に使用） |

### `history` 要素

| フィールド | 型 | 説明 |
|---|---|---|
| `generation` | integer | 世代番号 |
| `summary` | string | その世代の実行サマリ |

### `feedback` 要素

| フィールド | 型 | 説明 |
|---|---|---|
| `source` | string | フィードバックソース: `jira_comment` \| `bitbucket_pr` \| `user` |
| `author` | string | フィードバックの著者（`user` ソースの場合は省略） |
| `timestamp` | string | フィードバックの日時（ISO 8601） |
| `body` | string | フィードバックの内容 |
| `generation` | integer | どの世代に対するフィードバックか |

### `feedback_cursor`

| フィールド | 型 | 説明 |
|---|---|---|
| `jira_comment` | string | Jira コメントの最終取得タイムスタンプ |
| `bitbucket_pr` | string | Bitbucket PR コメントの最終取得タイムスタンプ |

### 例

```yaml
id: "20260301-001"
remote_id: "UBS-101"
datasource_id: jira
project_id: ubs-mgmt-tool
title: API実装
status: pending
generation: 1
description: ""
open_questions: []
preconditions: []
acceptance_criteria: []
completion_actions: []
execute_prompt: ""
history: []
feedback: []
feedback_cursor: {}
```

### 再オープン後の例

```yaml
id: "20260301-001"
remote_id: "UBS-101"
datasource_id: jira
project_id: ubs-mgmt-tool
title: API実装
status: pending
generation: 2
description: API エンドポイントの実装
open_questions: []
preconditions: []
acceptance_criteria:
  - API が正常にレスポンスを返すこと
completion_actions: []
execute_prompt: ""
history:
  - generation: 1
    summary: "Phase 1-3 完了。API 実装済み"
feedback:
  - source: jira_comment
    author: "レビュアー名"
    timestamp: "2026-03-14T10:00:00+09:00"
    body: "エラーハンドリングが不十分です。NullReferenceException の対策を追加してください"
    generation: 1
feedback_cursor:
  jira_comment: "2026-03-14T10:00:00+09:00"
```

### 規約

- 全フィールドはプログラムから読み書きする（`yaml.safe_load` / `yaml.dump`）
- `open_questions` は精査フェーズで計画ジョブが未決事項を洗い出す
- `execute_prompt` は計画ジョブが生成し、承認後にディスパッチャーに渡される
- `history` はフェーズ実行後に結果を追記する

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
  },
  "extra_binds": [
    {"source": "$HOME/.project-cache", "target": "$HOME/.project-cache", "mode": "rw"}
  ],
  "host_commands": [
    {"name": "az", "path": "/usr/bin/az", "allowed_patterns": ["pipelines run *", "pipelines runs show *"]}
  ]
}
```

### `orchestration`

タスク実行の自動オーケストレーションポリシーを定義する。
未設定の場合は手動フロー（従来動作）。

| フィールド | 型 | デフォルト | 説明 |
|---|---|---|---|
| `auto_approve` | boolean | `false` | `planned` → 自動で実行を開始するか |
| `require_first_approval` | boolean | `true` | 初回（`run_count=0`）のみ手動承認を要求。`false` の場合は初回から自動承認 |
| `max_runs_per_generation` | integer | `5` | 1世代あたりの最大フェーズ実行回数。超過時は `aborted` に遷移 |

**オーケストレーション有効時のフロー**:
1. 計画ジョブ完了 → `planned` かつ auto_approve 条件を満たせば自動で実行ジョブをディスパッチ
2. 実行ジョブ完了 → 評価ジョブを自動ディスパッチ
3. 評価ジョブ完了 → verdict に応じて次のアクションを自動実行:
   - DONE → `done`
   - NEXT_PHASE → 次フェーズの実行ジョブをディスパッチ
   - SUSPEND → `suspend (agent_review)`（ユーザ介入待ち）
   - ABORT → `aborted`

```json
{
  "orchestration": {
    "auto_approve": true,
    "require_first_approval": true,
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

### `extra_binds`

プロジェクト固有の追加 bind mount を定義する。サンドボックスプロファイルの `extra_binds` の**後**に追加される（プロファイル + プロジェクトのマージ）。要素のスキーマはサンドボックスプロファイルの `extra_binds` と同一。

### `host_commands`

プロジェクト固有の追加ホストコマンドを定義する。サンドボックスプロファイルの `host_commands` の**後**に追加される（プロファイル + プロジェクトのマージ）。要素のスキーマはサンドボックスプロファイルの `host_commands` と同一。


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
- `sync_mode=full` のデータソース: JSONL に含まれないが `tasks/index.jsonl` に存在するタスクは「消失タスク」として扱い、インデックスと YAML ファイルから削除する
- `sync_mode=append` のデータソース: 消失検出をスキップし、新規追加と既存更新のみ行う。sync 実行時に `done` タスクを GC する
- フィールドが存在しない場合はデフォルト値を使用（`null`）

---

## 6. Lifecycle（ライフサイクル状態）

ジョブチェーンの状態を管理するステートマシン。タスク管理の知識を持たない純粋なジョブオーケストレータ。
永続化: `$XDG_RUNTIME_DIR/my-tasks-dispatch/lifecycles.jsonl`
コンテキスト: `$XDG_RUNTIME_DIR/my-tasks-dispatch/{lifecycle_id}.context.yaml`

### フィールド

| フィールド | 型 | 説明 |
|---|---|---|
| `lifecycle_id` | string | 識別子。内部生成時は `lc-{連番}` 形式、外部指定時は任意文字列（例: `{task_id}-g{generation}`） |
| `project_id` | string | プロジェクト ID |
| `prompt` | string | 元の依頼内容 |
| `context_path` | string | コンテキストファイルの絶対パス（直接投入時は空） |
| `status` | string | `planning` \| `planned` \| `phase_executing` \| `phase_evaluating` \| `suspend` \| `done` \| `aborted` |
| `suspend_reason` | string\|null | suspend 時の理由: `needs_input`, `approval_required`, `agent_review` |
| `phases` | array | フェーズ定義の配列（コンテキスト YAML の phases を反映） |
| `current_phase` | integer | 現在実行中のフェーズインデックス（0始まり） |
| `run_count` | integer | 実行回数 |
| `max_runs` | integer | 最大実行回数 |
| `current_dispatch_id` | string\|null | 現在のサブジョブの dispatch_id |
| `created_at` | string | 作成日時（ISO 8601） |
| `updated_at` | string | 更新日時（ISO 8601） |

### 例

```jsonl
{"lifecycle_id":"lc-1","project_id":"bo","prompt":"API実装","context_path":"/run/user/1000/my-tasks-dispatch/lc-1.context.yaml","status":"phase_executing","suspend_reason":null,"phases":[{"goal":"API エンドポイント実装","status":"running"},{"goal":"テスト追加","status":"pending"}],"current_phase":0,"run_count":0,"max_runs":5,"current_dispatch_id":"bo-3","created_at":"2026-03-01T10:00:00+09:00","updated_at":"2026-03-01T10:05:00+09:00"}
{"lifecycle_id":"lc-2","project_id":"ubs","prompt":"バグ修正","context_path":"/run/user/1000/my-tasks-dispatch/lc-2.context.yaml","status":"suspend","suspend_reason":"needs_input","phases":[{"goal":"原因調査","status":"done"},{"goal":"修正実装","status":"pending"}],"current_phase":1,"run_count":1,"max_runs":5,"current_dispatch_id":null,"created_at":"2026-03-01T09:00:00+09:00","updated_at":"2026-03-01T09:30:00+09:00"}
```

---

## 7. 結果ファイル

ジョブ完了時にジョブが書き出す結果ファイル。Lifecycle ステートマシンが次の状態を決定するために使用する。
パス: `$XDG_RUNTIME_DIR/my-tasks-dispatch/{dispatch_id}.result.json`

### 計画ジョブ (plan)

```json
{"next_status": "planned"}
{"next_status": "needs_input"}
```

### 評価ジョブ (evaluate)

```json
{"verdict": "DONE", "phase_summary": "全達成条件を確認済み"}
{"verdict": "NEXT_PHASE", "phase_summary": "フェーズ1完了、次フェーズへ進行"}
{"verdict": "SUSPEND", "phase_summary": "API キーが必要"}
{"verdict": "ABORT", "phase_summary": "前提条件が誤り"}
```

### 実行ジョブ (execute)

結果ファイル不要。exit code で判定（0=成功、非0=失敗）。

---

## 8. ディスパッチャージョブ状態（インメモリ）

ディスパッチャーサーバがインメモリで管理するジョブ状態。
ファイルへの永続化は行わない（サーバ再起動時にリセット）。
ソケット: `$XDG_RUNTIME_DIR/my-tasks-dispatch/dispatcher.sock`

### ジョブフィールド

| フィールド | 型 | 説明 |
|---|---|---|
| `dispatch_id` | string | 識別子（`{project_id}-{連番}` 形式、例: `bo-1`） |
| `project_id` | string | プロジェクト ID |
| `job_type` | string | `execute` \| `evaluate` \| `plan` |
| `lifecycle_id` | string\|null | 関連する Lifecycle ID |
| `run` | integer\|null | ライフサイクル内のフェーズインデックス（0始まり） |
| `status` | string | `queued` \| `running` \| `done` \| `failed` |
| `pid` | integer\|null | 子プロセスの PID |
| `exit_code` | integer\|null | 終了コード |
| `started_at` | string\|null | 開始日時（ISO 8601形式） |
| `finished_at` | string\|null | 終了日時（ISO 8601形式） |

### status レスポンス例

```json
{"ok": true, "jobs": [
  {"dispatch_id": "bo-1", "project_id": "bo", "job_type": "execute", "status": "running", "pid": 12345, "exit_code": null, "started_at": "2026-03-01T10:00:00+09:00", "finished_at": null},
  {"dispatch_id": "bo-2", "project_id": "bo", "job_type": "plan", "status": "queued", "pid": null, "exit_code": null, "started_at": null, "finished_at": null}
], "lifecycles": [
  {"lifecycle_id": "lc-1", "project_id": "bo", "prompt": "API実装", "context_path": "/run/user/1000/my-tasks-dispatch/lc-1.context.yaml", "status": "phase_executing", "suspend_reason": null, "phases": [{"goal": "API エンドポイント実装", "status": "running"}], "current_phase": 0, "run_count": 0, "max_runs": 5, "current_dispatch_id": "bo-1", "created_at": "2026-03-01T10:00:00+09:00", "updated_at": "2026-03-01T10:05:00+09:00"}
]}
```

---

## 9. コンテキスト YAML スキーマ

ライフサイクルのコンテキスト情報を YAML 形式で保持する。
パス: `$XDG_RUNTIME_DIR/my-tasks-dispatch/{lifecycle_id}.context.yaml`

### スキーマ

| フィールド | 型 | 説明 |
|---|---|---|
| `meta` | object | タスクのメタ情報 |
| `meta.task_id` | string | タスク ID |
| `meta.remote_id` | string | データソース内でのタスク一意識別子 |
| `meta.datasource_id` | string | データソースの識別子 |
| `meta.project_id` | string | プロジェクト ID |
| `meta.generation` | integer | 再オープン世代 |
| `description` | string | タスクの説明文 |
| `preconditions` | array | タスク実行の事前条件リスト |
| `acceptance_criteria` | array | タスクの達成条件リスト |
| `open_questions` | array | 未決事項リスト |
| `completion_actions` | array | タスク完了後に実行するアクションリスト |
| `phases` | array | フェーズ定義の配列 |
| `execute_prompt` | string | 実行ジョブに渡すプロンプト |
| `previous_generations` | array | 過去世代の実行履歴 |
| `feedback` | array | レビュアー・ユーザーからのフィードバックリスト（タスク YAML の `feedback` と同一構造） |

### `phases` 要素

| フィールド | 型 | 説明 |
|---|---|---|
| `goal` | string | フェーズの目標 |
| `criteria` | string | フェーズの完了判定基準 |
| `status` | string | `pending` \| `running` \| `done` \| `skipped` |
| `summary` | string\|null | フェーズ完了時のサマリ |

### 例

```yaml
meta:
  task_id: ""
  remote_id: ""
  datasource_id: ""
  project_id: ""
  generation: 1
description: |
  タスクの説明文
preconditions: []
acceptance_criteria: []
open_questions: []
completion_actions: []
phases:
  - goal: "フェーズの目標"
    criteria: "完了判定基準"
    status: "pending"  # pending|running|done|skipped
    summary: null
execute_prompt: ""
previous_generations: []
feedback: []
```
