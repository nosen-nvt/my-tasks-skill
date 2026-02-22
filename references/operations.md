# 操作リファレンス

タスク管理スキルが提供する8つの操作の詳細手順。

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
   - **既存タスク**: title, due_date, url を上書き更新
   - **消失タスク**: タスクストアから削除し、プロジェクトと日次ゴールの参照も削除（JSONL に含まれない既存タスク）
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

2. 全プロジェクトの `projects/*.json` を読み込み、タスクの一覧を構築:
   - `tasks/*.json` から各タスクの詳細を取得（タスクストアに存在する = 残っているタスク）

3. マイルストーンの期日を考慮して優先度を算出し、今日取り組むべきタスクを提案:
   - 期日が近いマイルストーンのタスクを優先

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

## 7. タスク操作（データソース側）

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

## 8. 参照系

### プロジェクト状況確認

1. 対象プロジェクトの `projects/{project_id}.json` を読み込み

2. 各マイルストーンについて:
   - マイルストーン名・ゴール・期日を表示
   - タスク参照 (`ref`) から `tasks/*.json` を参照してタスク詳細を取得
   - タスク数を集計

3. 全体の進捗をマイルストーン単位で表示

### 日次ゴール確認

1. 当日の `daily/YYYY-MM-DD.json` を読み込み（存在しない場合はその旨を伝える）

2. ゴールのタスクの詳細を `tasks/*.json` から取得

3. ゴールの進捗を表示

### タスク検索

1. ユーザーの検索条件を確認（キーワード・プロジェクト・マイルストーン等）

2. `tasks/*.json` をすべて読み込み、条件に合致するタスクを絞り込み

3. 結果をプロジェクト・マイルストーン情報（`projects/*.json` を参照）と併せて表示

---

## 9. タスクディスパッチ

デイリーゴールのタスクを tmux 上で複数の Claude Code セッションとして並列実行する。

### 前提条件

- 日次ゴールが設定済み（操作6）
- ディスパッチ対象プロジェクトの `projects/{project_id}.json` に `working_directory` が設定されている
- `tmux` がインストールされている

### 手順

1. 日次ゴールを設定（操作6）し、今日取り組むタスクを確定

2. 必要に応じてプロジェクト定義に `working_directory` を設定:
   ```bash
   # projects/{project_id}.json に "working_directory": "/path/to/project" を追加
   ```

3. ディスパッチ開始:
   ```bash
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py start \
     --repo ~/.local/share/my-tasks \
     [--max-slots 3] \
     [--command "sandbox claude"]
   ```

4. 別ターミナルから監視:
   ```bash
   # ステータス確認
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py status

   # tmux ウィンドウ一覧
   tmux list-windows -t dispatch
   ```

5. 必要に応じてタスク追加:
   ```bash
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py add \
     --ref jira/UBS-103 \
     --working-dir /path/to/project \
     [--note "補足指示"]
   ```

6. 全タスク完了後、JSON レポートが stdout に出力される

7. 停止・強制終了:
   ```bash
   # 新規タスクの起動を停止（実行中セッションは継続）
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py stop

   # tmux セッションごと強制終了
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py kill
   ```
