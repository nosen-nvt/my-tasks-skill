# 操作リファレンス

タスク管理スキルが提供する10のオペレーションの詳細手順。

---

## 1. タスク収集

全データソースからタスクを収集し、`tasks/index.jsonl` + `tasks/*.md` を更新する。

### 手順

1. `scripts/fetch-all.sh` を実行し JSONL を取得:
   ```bash
   ~/.local/share/my-tasks/scripts/fetch-all.sh > /tmp/my-tasks-sync.jsonl
   ```

2. `sync-tasks.py` を実行してタスクインデックスと Markdown を更新:
   ```bash
   python3 ~/.claude/skills/my-tasks/scripts/sync-tasks.py \
     --repo ~/.local/share/my-tasks \
     --input /tmp/my-tasks-sync.jsonl
   ```

3. スクリプトのレポートを確認:
   - `auto_assigned`: `project_mapping` でプロジェクトが特定できたタスク
   - `needs_review`: プロジェクトが特定できなかったタスク → ユーザーに確認
   - `vanished`: 消失タスク（インデックスと Markdown から削除済み）

4. 要確認タスクに対してユーザーと対話:
   - プロジェクト一覧をコンテキストとして提示
   - 割り当て先プロジェクトをユーザーが選択
   - `tasks/index.jsonl` と `tasks/{id}.md` の `project_id` を更新

5. 一時ファイルを削除:
   ```bash
   rm /tmp/my-tasks-sync.jsonl
   ```

6. commit + push（datasources/ や projects/ に変更がある場合のみ）:
   ```bash
   cd ~/.local/share/my-tasks
   git add .
   git commit -m "sync: update tasks from all datasources"
   git push
   ```

---

## 2. メールトリアージ

メールデータソースからアクションアイテムを収集する。

### 手順

1. メールデータソースの収集スクリプトを実行（fetch-all.sh の一部として、またはメールのみ個別に）

2. 収集スクリプトが担う処理:
   - 未読メールを取得
   - エージェントがアクション不要と判断したメールを既読にする
   - アクション要のメールを JSONL 形式で出力

3. 出力された JSONL を sync-tasks.py に渡してタスク化（操作1と同じフロー）

---

## 3. タスク精査

`pending` タスクに対して質問リストを生成し、`needs_clarification` に遷移する。

### 手順

1. `tasks/index.jsonl` から `status=pending` のタスクを一覧（`deferred` タスクは精査対象外）

2. 対象タスクの `tasks/{id}.md` を読み込み

3. タスクの内容を分析し、「未決事項」セクションに質問リストを生成:
   ```markdown
   ## 未決事項

   - [ ] 認証方式は OAuth2 でよいか？
   - [ ] テストカバレッジの目標は？
   ```

4. `index.jsonl` と `.md` の status を `needs_clarification` に更新

5. ユーザーに質問を提示し、回答を収集:
   - 回答済みの項目を `[x]` に更新し、回答を追記
   - 全項目が `[x]` になったら `scoped` に遷移可能

---

## 4. プロンプト生成

`scoped` タスクの実行プロンプトを生成する。

### 手順

1. `tasks/index.jsonl` から `status=scoped` のタスクを一覧

2. 対象タスクの `tasks/{id}.md` を読み込み

3. 未決事項の回答、事前条件、達成条件を元に実行プロンプトを生成

4. `## 実行プロンプト` セクションにプロンプトを書き込み

---

## 5. プロンプト承認

生成されたプロンプトをユーザーに提示し、承認を得る。

### 手順

1. `tasks/index.jsonl` から `status=scoped` のタスクを一覧

2. `tasks/{id}.md` の `## 実行プロンプト` セクションをユーザーに提示

3. ユーザーが承認したら `index.jsonl` と `.md` の status を `approved` に更新

---

## 6. タスク実行

`approved` タスクをディスパッチャー経由で実行する。

### 手順

1. `tasks/index.jsonl` から `status=approved` のタスクを一覧（または特定のタスク ID を指定）

2. ディスパッチャーにジョブを投入:
   ```bash
   # タスク ID 指定（index.jsonl + .md から自動読み取り）
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py run --task 20260301-001

   # プロジェクト ID + stdin プロンプト指定
   echo "..." | python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py run --project bo
   ```

3. `index.jsonl` と `.md` の status を `running` に更新

### 対話セッション

ジョブ管理の対象外で対話セッションを起動する場合:
```bash
python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py open --project bo [--session main]
```

---

## 7. ステータス確認

タスク一覧やジョブ状況を表示する。

### タスク一覧

1. `tasks/index.jsonl` を読み込み、ステータス別に集計・表示

2. 必要に応じて特定タスクの `tasks/{id}.md` の詳細を表示

### ジョブ状況

1. ディスパッチャーにステータスを問い合わせ:
   ```bash
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py status
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py status --json
   ```

### タスク検索

1. ユーザーの検索条件を確認（キーワード・プロジェクト・ステータス等）

2. `tasks/index.jsonl` をフィルタリングして結果を表示

---

## 8. タスク操作（データソース側）

データソース側のタスクを操作する（ステータス変更、担当者変更、新規作成等）。

### 手順

1. ユーザーが変更したいタスクと操作内容を確認:
   - 対象タスクの ID（`tasks/index.jsonl` で特定）
   - 操作の種類

2. タスクの `datasource_id` から `datasources/{datasource_id}.json` を読み込み、`operations` を参照

3. 対応する操作の `command` テンプレートに実際の値を埋め込んで実行:
   ```bash
   # 例: JIRA のステータス更新
   atl jira issue update --key UBS-101 --status "Done" --site urbanb
   ```

4. 操作後、タスク収集を実行（操作1）してリポジトリに反映

---

## 9. 完了時アクション

`done` タスクの後処理を実行する。

### 手順

1. `tasks/index.jsonl` から `status=done` のタスクを一覧

2. 各タスクの `tasks/{id}.md` の `## 完了時アクション` セクションを確認

3. 記載されたアクションを実行:
   - データソース側のステータス更新（操作8を活用）
   - PR の作成
   - 通知の送信
   - etc.

4. アクション完了後、必要に応じてタスク収集（操作1）を実行

---

## 10. 設定管理

プロジェクト・データソースの CRUD を行う。

### リポジトリ初期化

#### 新規作成

1. ディレクトリ構成を作成:
   ```bash
   mkdir -p ~/.local/share/my-tasks/{datasources,projects,tasks,scripts}
   ```

2. `.gitignore` を作成:
   ```bash
   echo "tasks/" > ~/.local/share/my-tasks/.gitignore
   ```

3. `git init` + リモート設定:
   ```bash
   cd ~/.local/share/my-tasks && git init
   gh repo create my-tasks --private --source ~/.local/share/my-tasks
   ```

4. `fetch-all.sh` の雛形を作成

5. 初回 commit + push

#### クローン

1. `git clone {url} ~/.local/share/my-tasks`
2. 収集スクリプトに実行権限を付与

### データソース追加

1. ユーザーに以下を確認:
   - データソース ID、種別（`jira`, `todo`, `mail`）、説明、収集スクリプトパス

2. `datasources/{datasource_id}.json` を作成（`schemas.md` 参照）

3. 収集スクリプトの雛形を作成

4. `scripts/fetch-all.sh` に呼び出しを追記

5. commit + push

### プロジェクト追加

1. ユーザーに以下を確認:
   - プロジェクト ID、名前、説明、作業ディレクトリ、サンドボックスモード

2. `projects/{project_id}.json` を作成（`schemas.md` 参照）

3. commit + push

### プロジェクト更新

1. 既存の `projects/{project_id}.json` を読み込み

2. 変更内容を適用（sandbox_mode 変更、working_directory 変更等）

3. commit + push

---

## 11. タスク先送り / 先送り取消

`pending` タスクを `deferred`（先送り）にする、またはその取り消しを行う。

### 先送り（pending → deferred）

1. 対象タスクの `tasks/index.jsonl` エントリを確認し、`status` が `pending` であることを確認

2. `index.jsonl` の当該エントリの `status` を `deferred` に更新

3. `tasks/{id}.md` の `- Status:` 行を `deferred` に更新

### 先送り取消（deferred → pending）

1. 対象タスクの `tasks/index.jsonl` エントリを確認し、`status` が `deferred` であることを確認

2. `index.jsonl` の当該エントリの `status` を `pending` に更新

3. `tasks/{id}.md` の `- Status:` 行を `pending` に更新

### 制約

- `pending` → `deferred` 遷移は `status=pending` のタスクのみ可
- `deferred` からは `pending` にのみ戻せる（`needs_clarification` への直接遷移は不可）
- git commit は不要（tasks/ は gitignore 対象）

### 例

- 「[タスク名] を先送りして」→ `pending` → `deferred`
- 「やっぱりやる」「pending に戻して」→ `deferred` → `pending`
