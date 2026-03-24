# 操作リファレンス

タスク管理スキルが提供するオペレーションの詳細手順。

---

## 1. タスク収集

全データソースからタスクを収集し、`tasks/index.jsonl` + `tasks/*.yaml` を更新する。

### 手順

1. `scripts/fetch-all.sh` を実行し JSONL を取得:
   ```bash
   ~/.local/share/my-tasks/scripts/fetch-all.sh > /tmp/my-tasks-sync.jsonl
   ```

2. `sync-tasks.py` を実行してタスクインデックスと YAML を更新:
   ```bash
   # 全データソース（fetch-all.sh の出力に含まれる全データソースを処理）
   python3 ~/.claude/skills/my-tasks/scripts/sync-tasks.py \
     --repo ~/.local/share/my-tasks \
     --input /tmp/my-tasks-sync.jsonl

   # 特定データソースのみ処理（同期処理は指定データソースに限定される）
   # ※ done タスクの GC は全データソース横断で実行される
   python3 ~/.claude/skills/my-tasks/scripts/sync-tasks.py \
     --repo ~/.local/share/my-tasks \
     --input /tmp/my-tasks-sync.jsonl \
     --datasource jira,ms-todo
   ```

3. スクリプトのレポートを確認:
   - `added`: 新規追加されたタスク
   - `updated`: タイトルが更新されたタスク
   - `project_assigned`: `project_mapping` でプロジェクトが特定できたタスク
   - `project_unassigned`: プロジェクトが特定できなかったタスク（dispatch 時に判定）
   - `vanished`: 消失タスク（full モード: インデックスと YAML から削除済み）
   - `gc`: GC で除去された done タスク（全データソース横断で実行）

4. 一時ファイルを削除:
   ```bash
   rm /tmp/my-tasks-sync.jsonl
   ```

5. commit + push（datasources/ や projects/ に変更がある場合のみ）:
   ```bash
   cd ~/.local/share/my-tasks
   git add .
   git commit -m "sync: update tasks from all datasources"
   git push
   ```

---

## 2. メールトリアージ

全メールアカウント（Outlook + Gmail）の未読メールを一括トリアージし、アクションアイテムをタスク化する。

### 手順

1. 全メールデータソースを列挙:
   ```bash
   # datasources/ から type=mail のファイルを取得
   grep -l '"type": "mail"' ~/.local/share/my-tasks/datasources/*.json
   ```

2. 各アカウントの未読メールを取得:
   - **Outlook**: `msgraph mail list --unread --top 50`
   - **Gmail**: `google mail list --label UNREAD --max 50 --json --account {alias}`

3. エージェントが各メールをトリアージ:
   - 件名・送信者・スニペットから要否を判断
   - 必要に応じて `get` コマンドで本文を取得:
     - Outlook: `msgraph mail get --message-id {id}`
     - Gmail: `google mail get {id} --account {alias}`

4. 非アクション対象を既読化（datasource の `operations.mark_read` を使用）:
   - Outlook: `msgraph patch "/me/messages/{remote_id}" --body '{"isRead": true}'`
   - Gmail: `google mail modify {remote_id} --remove-label UNREAD --account {alias}`

5. アクション対象のメールを JSONL に出力し `sync-tasks.py` でタスク化:
   ```bash
   python3 ~/.claude/skills/my-tasks/scripts/sync-tasks.py \
     --repo ~/.local/share/my-tasks \
     --input /tmp/mail-triage.jsonl \
     --datasource mail-outlook,mail-gmail-nvt,mail-gmail-qzl
   ```
   `--datasource` でメール系のみ指定し、同期処理（追加・更新・消失検出）を限定する。ただし done タスクの GC は全データソース横断で実行される

### 注意

- メールトリアージはエージェント対話が必要なため `fetch-all.sh` には含めない

---

## 3. Plan（対話的タスク精査）

ダッシュボードから対話セッションを起動し、タスクを精査・計画する。
成果物はタスク YAML の `execute_prompt` フィールド。

### 手順

1. タスク情報を読み込む（`tasks/index.jsonl` + `tasks/{id}.yaml`）

2. ディスパッチャーの `open` コマンドで対話セッションを起動:
   ```bash
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher open --project bo
   ```

3. 対話セッション内で:
   - タスク YAML を読み込み
   - 作業ディレクトリのソースコードを調査
   - ユーザーと対話しながら計画を立案
   - タスク YAML の以下のフィールドを更新:
     - `description`: タスクの説明
     - `preconditions`: 事前条件リスト
     - `acceptance_criteria`: 達成条件リスト
     - `execute_prompt`: 実行プロンプト

4. セッション終了 → ダッシュボードに戻る

### 達成条件の記述ルール

- `manual` 以外のプロジェクトのタスクは AI エージェントが実装・実行する前提
- **達成条件は AI エージェント自身がローカルで検証可能な内容** にすること
- OK: ファイル内容の確認、YAML/JSON のパース検証、テスト実行（`dotnet test`, `npm test` 等）、ビルド成功
- NG: ブラウザでの手動確認、外部サービスの目視確認など、エージェントが実行できない操作

### manual プロジェクト

`working_directory` が未設定のプロジェクトは manual 扱い。Plan をスキップし、メインセッションで直接処理する:
- `done` に直接遷移
- 完了時アクション（データソース側のステータス更新等）は通常通り実行する

---

## 4. Dispatch（ジョブ実行）

タスク YAML の `execute_prompt` をディスパッチャーの `run` に渡してジョブを実行する。

### 手順

1. タスク情報を読み込む（`tasks/index.jsonl` + `tasks/{id}.yaml`）

2. `execute_prompt` が存在することを確認（空なら先に Plan を実行）

3. タスクステータスを `in_progress` に更新（`tasks/index.jsonl` と `tasks/{id}.yaml`）

4. UUID 形式の `session_id` を生成

5. ディスパッチャーの `run` コマンドでジョブを実行:
   ```bash
   # プロンプトファイル渡し
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher run \
     --project bo --prompt-file /tmp/prompt-20260301-001.md \
     --session-id "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

   # 標準入力渡し
   echo "{execute_prompt}" | python3 ~/.claude/skills/my-tasks/scripts/dispatcher run \
     --project bo --session-id "a1b2c3d4-..."
   ```

6. 返却された `dispatch_id` と `session_id` をタスク YAML に記録:
   - `dispatch_id`: ジョブの識別に使用
   - `session_id`: Resume でのセッション再開に使用

7. ステータスを確認:
   ```bash
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher status
   ```

8. ジョブ完了後、結果に応じてタスクステータスを更新:
   - 成功 → `done`（完了時アクション実行）
   - 失敗・中止 → `aborted`
   - 軽微な修正が必要 → Resume（操作5）へ
   - レビュー指摘あり → Feedback（操作6）へ

---

## 5. Resume（セッション再開）

Dispatch したジョブの Claude Code セッションを再開し、同一コンテキスト上で軽微な修正を行う。

### 手順

1. タスク YAML から `session_id` を取得

2. ディスパッチャーの `resume` コマンドでセッションを再開:
   ```bash
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher resume \
     --project bo --session-id "a1b2c3d4-..."
   ```

3. tmux ウィンドウで `claude --resume "{session_id}"` が対話モードで起動される

4. 同一コンテキスト（会話履歴・ファイル状態）を引き継いだ状態で対話的に修正

5. セッション終了

### 用途

- コミットメッセージの修正
- 軽微なバグ修正
- PR の説明文更新

同一セッションのコンテキスト内で対応できる範囲の調整に限定する。
それ以上の修正が必要な場合は、PR にコメントして Feedback フロー（操作6）に回す。

---

## 6. Feedback（フィードバック収集・対応）

フィードバックを収集し、対応ジョブを Dispatch する。

### フィードバック収集

```bash
# 全ソースから収集
python3 ~/.claude/skills/my-tasks/scripts/collect-feedback.py \
  --repo ~/.local/share/my-tasks --id 20260301-001

# 特定ソースのみ
python3 ~/.claude/skills/my-tasks/scripts/collect-feedback.py \
  --repo ~/.local/share/my-tasks --id 20260301-001 --source github

python3 ~/.claude/skills/my-tasks/scripts/collect-feedback.py \
  --repo ~/.local/share/my-tasks --id 20260301-001 --source jira
```

スクリプトは以下を実行する:
1. タスク YAML から `remote_id`, `datasource_id`, `pr_url`, `feedback_cursor` を取得
2. 各ソースから `feedback_cursor` 以降の新規コメントを収集
3. 収集結果をタスク YAML の `feedback` に新しいグループ（`collected_at` + `items[]`）として追加
4. `feedback_cursor` を更新
5. JSON レポートを stdout に出力

### 手動フィードバック追加

```bash
python3 ~/.claude/skills/my-tasks/scripts/add-feedback.py \
  --repo ~/.local/share/my-tasks --id 20260301-001 \
  --body "ログレベルも変更してください"
```

### フィードバック対応ジョブの実行

1. フィードバック収集後、最新のグループ（最新の `collected_at`）を特定
2. 元の `execute_prompt` + フィードバック内容から対応プロンプトを構築
3. Dispatch（操作4）と同じ手順でジョブを実行

### 繰り返し

Feedback は複数回実行可能。毎回新しいグループが追加され、ジョブには最新グループの `collected_at` を渡すことで「今回対応すべきフィードバック」を識別できる。

### 前提条件

- Jira フィードバック: datasource JSON に `site_mapping`（プロジェクトキー → サイト名）が設定されていること
- PR フィードバック: タスク YAML に `pr_url` が設定されていること

---

## 7. ステータス確認

タスク一覧やジョブ状況を表示する。

### タスク一覧

1. `tasks/index.jsonl` を読み込み、ステータス別に集計・表示

2. 必要に応じて特定タスクの `tasks/{id}.yaml` の詳細を表示

### ジョブ状況

1. ディスパッチャーにステータスを問い合わせ:
   ```bash
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher status
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher status --json
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

4. 操作後、必要に応じてタスク収集を実行（操作1）してリポジトリに反映

---

## 9. 設定管理

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
   - プロジェクト ID、名前、説明、作業ディレクトリ、サンドボックスプロファイル

2. `projects/{project_id}.json` を作成（`schemas.md` 参照）

3. commit + push

### プロジェクト更新

1. 既存の `projects/{project_id}.json` を読み込み

2. 変更内容を適用（sandbox_profile 変更、working_directory 変更等）

3. commit + push

---

## 10. 対話セッション

ジョブ管理の対象外で対話セッションを起動する。

### 手順

```bash
# 通常の対話セッション
python3 ~/.claude/skills/my-tasks/scripts/dispatcher open --project bo [--session main]

# worktree を指定して対話セッションを起動（Resume 対応）
python3 ~/.claude/skills/my-tasks/scripts/dispatcher open --project bo --worktree /path/to/worktree
```
