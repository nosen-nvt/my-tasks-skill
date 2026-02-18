# タスク管理スキル仕様書

## 概要

Claude Code のグローバルスキルとして、個人のタスク管理リポジトリを運用するためのスキル。
複数の外部データソース（JIRA、Microsoft To Do 等）からタスクを収集し、プロジェクト・マイルストーン単位で管理する。

タスク管理リポジトリは人間が直接読み書きするのではなく、Claude Code（エージェント）が読み書きする前提で設計する。
ファイル形式はすべて JSON。git はデータの永続化・同期手段として利用する。

## スキルの配置

- グローバルスキルとして `~/.claude/skills/my-tasks/` に配置
- タスク管理リポジトリとは独立しており、どのプロジェクトからでも利用可能

## タスク管理リポジトリ

### 配置先

`~/.local/share/my-tasks/`

### ディレクトリ構成

```
~/.local/share/my-tasks/
├── datasources/
│   ├── jira.json
│   └── ms-todo.json
├── projects/
│   ├── project-a.json
│   └── project-b.json
├── tasks/
│   ├── jira.json           # JIRA から取得したタスク実体
│   └── ms-todo.json        # Microsoft To Do から取得したタスク実体
├── daily/
│   ├── 2026-02-18.json
│   └── ...
└── scripts/
    ├── fetch-all.sh        # 全データソース一括取得
    ├── fetch-jira.sh
    └── fetch-ms-todo.sh
```

### データソース定義 (`datasources/*.json`)

1データソース1ファイル。

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

- `project_mapping`: JSONL の `project_key` 値（完全一致）から `projects/` 配下のプロジェクトIDへのマッピング
- `operations`: データソース側でタスクを操作するためのコマンド例と説明。外部サービス操作用の別スキルを参照して記述する。

### タスクストア (`tasks/*.json`)

1データソース1ファイル。データソースから取得したタスクの実体を保持する。
プロジェクトのマイルストーンや日次ゴールからは `datasource_id/remote_id` の複合キーで参照し、タスクの詳細はこのファイルから取得する。

```json
{
  "datasource_id": "jira",
  "updated_at": "2026-02-18T09:00:00+09:00",
  "tasks": [
    {
      "remote_id": "UBS-101",
      "title": "API実装",
      "status": "in_progress",
      "due_date": "2026-03-15",
      "url": "https://jira.example.com/browse/UBS-101",
      "project_key": "UBS"
    },
    {
      "remote_id": "UBS-102",
      "title": "認証機能の追加",
      "status": "pending",
      "due_date": "2026-03-20",
      "url": "https://jira.example.com/browse/UBS-102",
      "project_key": "UBS"
    },
    {
      "remote_id": "UBS-200",
      "title": "ドキュメント整備",
      "status": "done",
      "due_date": null,
      "url": "https://jira.example.com/browse/UBS-200",
      "project_key": "UBS"
    }
  ]
}
```

- タスク最新化時にデータソースから取得した内容で更新する
- 消失タスク（データソースから消えたタスク）は `status` を `done` に変更し、作業ログとして残す
- `updated_at` で最終同期日時を記録する

### プロジェクト定義 (`projects/*.json`)

1プロジェクト1ファイル。

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

- すべてのタスクはいずれかのマイルストーンに属する
- マイルストーン指定がないタスクは `_default` マイルストーンに紐づける
- タスクは `datasource_id/remote_id` の複合キーで参照する。詳細は `tasks/*.json` を参照

### 日次ゴール (`daily/YYYY-MM-DD.json`)

1日1ファイル。

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
  ],
  "review": {
    "summary": "UBS-101 は完了。UBS-102 はAPI仕様の確認待ちでブロック中。",
    "notes": "明日の朝にAPI仕様をチームに確認する"
  }
}
```

- `goals`: プロジェクト・マイルストーン単位でタスクを参照。タスクの詳細は `tasks/*.json` から取得
- `review`: 1日の振り返り。完了/未完了の概要と気づきを記録

### 収集スクリプト (`scripts/`)

#### 規約

- `fetch-all.sh`: 全データソースの収集スクリプトを順に実行し、結果を stdout に出力
- 各データソース別スクリプト（`fetch-jira.sh` 等）: 引数不要。自分の担当分の全未完了タスクを取得し、JSONL を stdout に出力

#### JSONL スキーマ

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `datasource_id` | string | Yes | データソースの識別子 |
| `remote_id` | string | Yes | データソース内でのタスク一意識別子 |
| `title` | string | Yes | タスクタイトル |
| `status` | string | No | `pending`（デフォルト）, `in_progress`, `done` |
| `due_date` | string | No | 期日（YYYY-MM-DD） |
| `url` | string | No | タスクのURL |
| `project_key` | string | No | データソース内のプロジェクト識別子（自動振り分けに使用） |

```jsonl
{"datasource_id":"jira","remote_id":"UBS-101","title":"API実装","status":"in_progress","project_key":"UBS","url":"https://jira.example.com/browse/UBS-101"}
{"datasource_id":"ms-todo","remote_id":"abc123","title":"書類提出","status":"pending","project_key":"個人タスク"}
```

## スキルが提供する操作

ユーザーは自然言語で指示する。以下は主な操作とその処理内容。

### 1. リポジトリ初期化

**新規作成:**
1. `~/.local/share/my-tasks/` にディレクトリ構成を作成
2. `git init`
3. リモートリポジトリの URL をユーザーに確認。まだ無い場合は `gh repo create` でプライベートリポジトリを作成
4. リモートを設定し、初回 commit + push

**クローン:**
1. リモートリポジトリの URL をユーザーに確認
2. `~/.local/share/my-tasks/` に `git clone`

### 2. データソース追加

1. データソースID、説明、収集スクリプトパスをユーザーに確認
2. プロジェクトマッピングルールを設定（後から追加可能）
3. データソース側の操作コマンドを設定（外部スキルを参照して記述）
4. `datasources/{datasource_id}.json` を作成
5. 対応する収集スクリプトの雛形を `scripts/` に作成
6. `fetch-all.sh` に新しいスクリプトの呼び出しを追加
7. commit + push

### 3. プロジェクト追加

1. プロジェクトID、名前、説明、関連リポジトリをユーザーに確認
2. `_default` マイルストーンを含む `projects/{project_id}.json` を作成
3. commit + push

### 4. マイルストーン追加

1. 対象プロジェクト、マイルストーン名、ゴール、期日をユーザーに確認
2. プロジェクトJSON にマイルストーンを追加
3. commit + push

### 5. タスク最新化

1. `scripts/fetch-all.sh` を実行し JSONL を取得
2. `tasks/*.json`（タスクストア）を更新:
   - **新規タスク**: タスクストアに追加
   - **既存タスク**: データソース側の内容（title, status, due_date, url）で上書き更新
   - **消失タスク**: 今回のJSONLに含まれない既存タスクは `status` を `done` に変更（作業ログとして残す）
3. 新規タスクのプロジェクト割り当て:
   - データソース定義の `project_mapping` で自動振り分け
   - マッチしない場合はプロジェクトの内容（説明、マイルストーン）に照らして適切なプロジェクトを提案し、ユーザーに確認
   - ユーザーは提案を承認、別プロジェクトを選択、または未割り当て（`_default` マイルストーン）を選択できる
4. プロジェクトJSON のマイルストーン内タスク参照を更新
5. JSONL を破棄
6. commit + push

### 6. 日次ゴール設定

1. 全プロジェクトの未完了タスクを `tasks/*.json` から参照
2. マイルストーンの期日を考慮し、今日取り組むべきタスクを提案
3. ユーザーと対話してタスクを選定
4. `daily/YYYY-MM-DD.json` を作成
5. commit + push

### 7. 日次ふりかえり

1. 当日の日次ゴールを読み込み
2. データソースからタスクを再取得し、タスクストアを最新化
3. 完了/未完了の状況をユーザーに提示
4. ユーザーと対話して振り返りコメントをまとめる
5. 日次ゴールJSON の `review` を更新
6. commit + push

### 8. タスク操作（データソース側）

1. ユーザーが変更したいタスクと操作内容を確認
2. データソース定義の `operations` を参照し、対応するコマンドを実行
3. 実行後、タスク最新化を実行してリポジトリに反映

### 9. 参照系

- **プロジェクト状況確認**: プロジェクトのマイルストーン進捗、未完了タスク一覧を表示（タスク詳細は `tasks/*.json` から取得）
- **日次ゴール確認**: 当日のゴールと進捗を表示
- **タスク検索**: プロジェクト横断で `tasks/*.json` を検索

## git 操作ポリシー

- リポジトリに変更を加える操作はすべて、操作完了後に自動で `git add . && git commit && git push` を実行する
- コミットメッセージは操作内容を簡潔に記述（例: `sync: update tasks from all datasources`, `daily: set goals for 2026-02-18`）
- git は手軽な分散DBとして利用しており、コミット粒度を意識する必要はない

## ステータスの管理方針

- ステータスの Source of Truth は常にデータソース側
- リポジトリ側でステータスを独自に変更しない
- ステータスを変更したい場合は、データソース定義の `operations` に記載されたコマンドを使ってデータソース側を更新し、その後タスク最新化で反映する
