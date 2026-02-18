# スキーマリファレンス

## 1. データソース定義 (`datasources/{datasource_id}.json`)

1データソース1ファイル。データソースの設定と操作コマンドを定義する。

### スキーマ

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `datasource_id` | string | Yes | データソースの識別子（ファイル名と一致） |
| `description` | string | Yes | データソースの説明 |
| `script` | string | Yes | 収集スクリプトのパス（リポジトリルートからの相対パス） |
| `project_mapping` | object | No | `project_key` → `project_id` のマッピング |
| `operations` | object | No | データソース側でのタスク操作コマンド定義 |

### `project_mapping`

JSONL の `project_key` 値（完全一致）から `projects/` 配下のプロジェクトIDへのマッピング。
タスク最新化時に新規タスクの自動割り当てに使用する。

### `operations`

データソース側でタスクを操作するためのコマンド例と説明。
操作名をキーとし、`description`（説明）と `command`（実行コマンドテンプレート）を持つ。
コマンドテンプレートの `{変数名}` はエージェントが実際の値で置換して実行する。

### 例

```json
{
  "datasource_id": "jira",
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
    },
    "assign": {
      "description": "担当者を変更する",
      "command": "jira issue assign {remote_id} {assignee}"
    },
    "create": {
      "description": "新規タスクを作成する",
      "command": "jira issue create --project {project_key} --type Task --summary \"{title}\""
    }
  }
}
```

---

## 2. タスクストア (`tasks/{datasource_id}.json`)

1データソース1ファイル。データソースから取得したタスクの実体を保持する。

### スキーマ

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `datasource_id` | string | Yes | データソースの識別子（ファイル名と一致） |
| `updated_at` | string | Yes | 最終同期日時（ISO 8601形式） |
| `tasks` | array | Yes | タスクエントリの配列 |

### タスクエントリのスキーマ

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `remote_id` | string | Yes | データソース内でのタスク一意識別子 |
| `title` | string | Yes | タスクタイトル |
| `due_date` | string\|null | No | 期日（YYYY-MM-DD形式）、なければ `null` |
| `url` | string\|null | No | タスクのURL |
| `project_key` | string\|null | No | データソース内のプロジェクト識別子 |

### 例

```json
{
  "datasource_id": "jira",
  "updated_at": "2026-02-18T09:00:00+09:00",
  "tasks": [
    {
      "remote_id": "UBS-101",
      "title": "API実装",
      "due_date": "2026-03-15",
      "url": "https://jira.example.com/browse/UBS-101",
      "project_key": "UBS"
    },
    {
      "remote_id": "UBS-102",
      "title": "認証機能の追加",
      "due_date": "2026-03-20",
      "url": "https://jira.example.com/browse/UBS-102",
      "project_key": "UBS"
    }
  ]
}
```

---

## 3. プロジェクト定義 (`projects/{project_id}.json`)

1プロジェクト1ファイル。プロジェクトとマイルストーンの構成を定義する。

### スキーマ

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `project_id` | string | Yes | プロジェクトの識別子（ファイル名と一致） |
| `name` | string | Yes | プロジェクト名 |
| `description` | string | No | プロジェクトの説明 |
| `repositories` | array | No | 関連するリポジトリURLの配列 |
| `milestones` | array | Yes | マイルストーンの配列（`_default` を必ず含む） |

### マイルストーンのスキーマ

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `milestone_id` | string | Yes | マイルストーンの識別子 |
| `name` | string | Yes | マイルストーン名 |
| `goal` | string | No | マイルストーンのゴール説明 |
| `due_date` | string\|null | No | 期日（YYYY-MM-DD形式）、なければ `null` |
| `tasks` | array | Yes | タスク参照の配列 |

### タスク参照のスキーマ

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `ref` | string | Yes | `datasource_id/remote_id` 形式の複合キー |

### 規約

- すべてのタスクはいずれかのマイルストーンに属する
- マイルストーン指定がないタスクは `_default` マイルストーンに紐づける
- `_default` マイルストーンは必ず存在しなければならない

### 例

```json
{
  "project_id": "ubs-mgmt-tool",
  "name": "UBS管理ツール",
  "description": "データ統合基盤の開発",
  "repositories": [
    "https://github.com/example/ubs-mgmt-tool"
  ],
  "milestones": [
    {
      "milestone_id": "v1-release",
      "name": "v1.0 リリース",
      "goal": "基本機能の実装完了",
      "due_date": "2026-03-31",
      "tasks": [
        { "ref": "jira/UBS-101" },
        { "ref": "jira/UBS-102" }
      ]
    },
    {
      "milestone_id": "_default",
      "name": "未分類",
      "goal": "",
      "due_date": null,
      "tasks": [
        { "ref": "jira/UBS-200" }
      ]
    }
  ]
}
```

---

## 4. 日次ゴール (`daily/YYYY-MM-DD.json`)

1日1ファイル。その日の作業ゴールを記録する。

### スキーマ

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `date` | string | Yes | 日付（YYYY-MM-DD形式） |
| `goals` | array | Yes | ゴールエントリの配列 |

### ゴールエントリのスキーマ

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `project_id` | string | Yes | プロジェクトの識別子 |
| `milestone_id` | string | Yes | マイルストーンの識別子 |
| `tasks` | array | Yes | タスク参照の配列（`ref` キーを持つオブジェクト） |

### 例

```json
{
  "date": "2026-02-18",
  "goals": [
    {
      "project_id": "ubs-mgmt-tool",
      "milestone_id": "v1-release",
      "tasks": [
        { "ref": "jira/UBS-101" },
        { "ref": "jira/UBS-102" }
      ]
    }
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
- JSONL に含まれないが `tasks/*.json` に存在するタスクは「消失タスク」として扱い、タスクストアから削除する。プロジェクトと日次ゴールの参照も削除する
- フィールドが存在しない場合はデフォルト値を使用（`null`）
