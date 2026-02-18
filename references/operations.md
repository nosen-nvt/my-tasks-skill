# 操作リファレンス

タスク管理スキルが提供する9つの操作の詳細手順。

---

## 1. リポジトリ初期化

### 新規作成

1. `~/.local/share/my-tasks/` にディレクトリ構成を作成:
   ```bash
   mkdir -p ~/.local/share/my-tasks/{datasources,projects,tasks,daily,scripts}
   ```

2. `git init`:
   ```bash
   cd ~/.local/share/my-tasks && git init
   ```

3. リモートリポジトリの URL をユーザーに確認:
   - まだない場合は `gh repo create` でプライベートリポジトリを作成することを提案:
     ```bash
     gh repo create my-tasks --private --source ~/.local/share/my-tasks
     ```
   - 既存 URL がある場合:
     ```bash
     git remote add origin {url}
     ```

4. `fetch-all.sh` の雛形を作成:
   ```bash
   #!/bin/bash
   set -euo pipefail
   SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
   # 各データソースの収集スクリプトをここに追加
   ```
   ```bash
   chmod +x ~/.local/share/my-tasks/scripts/fetch-all.sh
   ```

5. 初回 commit + push:
   ```bash
   cd ~/.local/share/my-tasks
   git add .
   git commit -m "init: initialize my-tasks repository"
   git push -u origin main
   ```

### クローン

1. リモートリポジトリの URL をユーザーに確認

2. `~/.local/share/` に `git clone`:
   ```bash
   git clone {url} ~/.local/share/my-tasks
   ```

3. 収集スクリプトに実行権限を付与:
   ```bash
   chmod +x ~/.local/share/my-tasks/scripts/*.sh
   ```

---

## 2. データソース追加

1. ユーザーに以下を確認:
   - データソースID（例: `jira`, `ms-todo`）
   - データソースの説明
   - 収集スクリプトのパス（リポジトリルートからの相対パス、例: `scripts/fetch-jira.sh`）

2. `datasources/{datasource_id}.json` を作成（`schemas.md` のスキーマを参照）:
   ```json
   {
     "datasource_id": "{id}",
     "description": "{説明}",
     "script": "scripts/fetch-{id}.sh",
     "project_mapping": {},
     "operations": {}
   }
   ```

3. 収集スクリプトの雛形を `scripts/fetch-{datasource_id}.sh` に作成:
   ```bash
   #!/bin/bash
   # {datasource_id} の未完了タスクを JSONL 形式で stdout に出力する
   # TODO: 実際の取得ロジックを実装してください
   set -euo pipefail

   # 例（JSONL 形式で出力）:
   # echo '{"datasource_id":"{datasource_id}","remote_id":"TASK-1","title":"タスク名","status":"pending"}'
   ```
   ```bash
   chmod +x ~/.local/share/my-tasks/scripts/fetch-{datasource_id}.sh
   ```

4. `scripts/fetch-all.sh` に新しいスクリプトの呼び出しを追記:
   ```bash
   bash "$SCRIPT_DIR/fetch-{datasource_id}.sh"
   ```

5. プロジェクトマッピングルールの設定を提案（後から追加も可能）

6. データソース側の操作コマンドを設定（外部スキルを参照して記述）

7. commit + push:
   ```bash
   cd ~/.local/share/my-tasks
   git add .
   git commit -m "feat: add datasource {datasource_id}"
   git push
   ```

---

## 3. プロジェクト追加

1. ユーザーに以下を確認:
   - プロジェクトID（例: `ubs-mgmt-tool`）
   - プロジェクト名（例: `UBS管理ツール`）
   - プロジェクトの説明（任意）
   - 関連するリポジトリURL（任意、複数可）

2. `_default` マイルストーンを含む `projects/{project_id}.json` を作成（`schemas.md` のスキーマを参照）:
   ```json
   {
     "project_id": "{project_id}",
     "name": "{name}",
     "description": "{description}",
     "repositories": [],
     "milestones": [
       {
         "milestone_id": "_default",
         "name": "未分類",
         "goal": "",
         "due_date": null,
         "tasks": []
       }
     ]
   }
   ```

3. commit + push:
   ```bash
   cd ~/.local/share/my-tasks
   git add .
   git commit -m "feat: add project {project_id}"
   git push
   ```

---

## 4. マイルストーン追加

1. ユーザーに以下を確認:
   - 対象プロジェクト（既存プロジェクト一覧を表示して選択）
   - マイルストーン名（例: `v1.0 リリース`）
   - マイルストーンID（例: `v1-release`）
   - ゴールの説明（任意）
   - 期日（YYYY-MM-DD形式、任意）

2. `projects/{project_id}.json` の `milestones` 配列に新しいマイルストーンを追加:
   ```json
   {
     "milestone_id": "{milestone_id}",
     "name": "{name}",
     "goal": "{goal}",
     "due_date": "{due_date}",
     "tasks": []
   }
   ```
   **注意**: `_default` マイルストーンは末尾に保持すること

3. commit + push:
   ```bash
   cd ~/.local/share/my-tasks
   git add .
   git commit -m "feat: add milestone {milestone_id} to {project_id}"
   git push
   ```

---

## 5. タスク最新化

1. `scripts/fetch-all.sh` を実行し JSONL を取得:
   ```bash
   ~/.local/share/my-tasks/scripts/fetch-all.sh > /tmp/my-tasks-sync.jsonl
   ```

2. `sync-tasks.py` を実行してタスクストアを更新:
   ```bash
   python3 ~/.claude/skills/my-tasks/scripts/sync-tasks.py \
     --repo ~/.local/share/my-tasks \
     --input /tmp/my-tasks-sync.jsonl
   ```
   スクリプトは以下を処理する:
   - **新規タスク**: タスクストアに追加
   - **既存タスク**: title, status, due_date, url を上書き更新
   - **消失タスク**: `status` を `done` に変更（JSONL に含まれない既存タスク）
   - `updated_at` を現在時刻で更新

3. スクリプトのレポートを確認:
   - 自動割り当て成功: `project_mapping` でプロジェクトが特定できたタスク → `projects/*.json` の `_default` マイルストーンに追加
   - 要確認: プロジェクトが特定できなかったタスク → ユーザーに提案して確認

4. 要確認タスクに対してユーザーと対話:
   - プロジェクト定義（名前・説明・マイルストーン）をコンテキストとして提示
   - 割り当て先プロジェクト・マイルストーンをユーザーが選択
   - または「未割り当て」（`_default` マイルストーン）を選択

5. プロジェクトJSONのマイルストーン内タスク参照を更新（新規タスクの割り当て結果を反映）

6. 一時ファイルを削除:
   ```bash
   rm /tmp/my-tasks-sync.jsonl
   ```

7. commit + push:
   ```bash
   cd ~/.local/share/my-tasks
   git add .
   git commit -m "sync: update tasks from all datasources"
   git push
   ```

---

## 6. 日次ゴール設定

1. 今日の日付を確認し、`daily/YYYY-MM-DD.json` が既に存在するか確認

2. 全プロジェクトの `projects/*.json` を読み込み、未完了タスクの一覧を構築:
   - `tasks/*.json` から各タスクの詳細を取得
   - `status` が `done` のタスクは除外

3. マイルストーンの期日を考慮して優先度を算出し、今日取り組むべきタスクを提案:
   - 期日が近いマイルストーンのタスクを優先
   - `in_progress` のタスクを優先
   - 前日の振り返り（前日の `daily/` ファイルの `review`）があれば参照

4. ユーザーと対話してタスクを選定:
   - 提案リストを表示（プロジェクト・マイルストーン単位）
   - ユーザーが追加・削除・変更を指示できる

5. `daily/YYYY-MM-DD.json` を作成（`schemas.md` のスキーマを参照）

6. commit + push:
   ```bash
   cd ~/.local/share/my-tasks
   git add .
   git commit -m "daily: set goals for {YYYY-MM-DD}"
   git push
   ```

---

## 7. 日次ふりかえり

1. 当日の `daily/YYYY-MM-DD.json` を読み込み、ゴールのタスク一覧を取得

2. タスク最新化を実行（操作5を参照）してタスクストアを更新

3. ゴールのタスクの最新状態（status）を取得し、完了/未完了を整理:
   - `done` → 完了
   - `in_progress` または `pending` → 未完了

4. ユーザーに状況を提示:
   - 完了したタスクの一覧
   - 未完了のタスクと理由（ユーザーに確認）

5. ユーザーと対話して振り返りコメントをまとめる:
   - 1日の作業サマリ
   - ブロッカーや気づき
   - 翌日への申し送り

6. 日次ゴールJSONの `review` を更新:
   ```json
   {
     "summary": "{完了/未完了の概要}",
     "notes": "{気づきや申し送り}"
   }
   ```

7. commit + push:
   ```bash
   cd ~/.local/share/my-tasks
   git add .
   git commit -m "daily: review for {YYYY-MM-DD}"
   git push
   ```

---

## 8. タスク操作（データソース側）

1. ユーザーが変更したいタスクと操作内容を確認:
   - 対象タスクの `datasource_id/remote_id`
   - 操作の種類（ステータス変更、担当者変更、新規作成 等）

2. 対象データソースの `datasources/{datasource_id}.json` を読み込み、`operations` を参照

3. 対応する操作の `command` テンプレートに実際の値を埋め込んで実行:
   ```bash
   # 例: JIRAのステータス更新
   jira issue move UBS-101 "Done"
   ```

4. 実行後、タスク最新化を実行（操作5を参照）してリポジトリに反映

5. commit + push（タスク最新化の commit に含まれる）

---

## 9. 参照系

### プロジェクト状況確認

1. 対象プロジェクトの `projects/{project_id}.json` を読み込み

2. 各マイルストーンについて:
   - マイルストーン名・ゴール・期日を表示
   - タスク参照 (`ref`) から `tasks/*.json` を参照してタスク詳細を取得
   - 完了タスク数 / 全タスク数を集計

3. 全体の進捗をマイルストーン単位で表示

### 日次ゴール確認

1. 当日の `daily/YYYY-MM-DD.json` を読み込み（存在しない場合はその旨を伝える）

2. ゴールのタスクの詳細を `tasks/*.json` から取得

3. 現在のタスクステータスと併せてゴールの進捗を表示

### タスク検索

1. ユーザーの検索条件を確認（キーワード・プロジェクト・ステータス等）

2. `tasks/*.json` をすべて読み込み、条件に合致するタスクを絞り込み

3. 結果をプロジェクト・マイルストーン情報（`projects/*.json` を参照）と併せて表示
