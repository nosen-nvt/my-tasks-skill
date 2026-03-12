# 操作リファレンス

タスク管理スキルが提供するオペレーションの詳細手順。

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
   - `reopened`: done から再オープンされたタスク
   - `project_assigned`: `project_mapping` でプロジェクトが特定できたタスク
   - `project_unassigned`: プロジェクトが特定できなかったタスク（dispatch 時に判定）
   - `vanished`: 消失タスク（full モード: インデックスと Markdown から削除済み）
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

## 3. dispatch（ライフサイクル開始）

ジョブチェーン（Lifecycle）を開始する。タスク情報の解決とステータス更新はスキル側（呼び出し元）が行い、dispatcher にはコンテキストとして送信する。
Lifecycle はタスク管理の知識を持たない純粋なジョブオーケストレータ。

### 手順

1. タスク情報を読み込む（`tasks/index.jsonl` + `tasks/{id}.md`）
2. タスクステータスを `in_progress` に更新（`tasks/index.jsonl` と `tasks/{id}.md`）
3. ライフサイクルを開始:
   ```bash
   # タスクをディスパッチ（スキル側でタスク解決後に呼び出す）
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher dispatch \
     --project bo --prompt "タスクタイトル" --context-file /path/to/task.md

   # lifecycle_id を外部指定（タスクとのトレーサビリティ確保）
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher dispatch \
     --project bo --prompt "タスクタイトル" --context-file /path/to/task.md \
     --lifecycle-id "20260312-001-g1"

   # タスクなしの直接投入（プロンプトから最小コンテキストを自動生成し精査を実行）
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher dispatch \
     --project bo --prompt "バグを修正して"

   # --project 省略時はプロンプトから自動判定を試みる
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher dispatch \
     --prompt "バグを修正して" --context-file /path/to/task.md
   ```

4. ステータスを確認:
   ```bash
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher status
   ```

### Lifecycle ステートマシン

ステータス値: `reshaping`, `running`, `evaluating`, `suspend`, `done`

```
dispatch → reshaping → 精査ジョブ完了
                         ├── [scoped + auto_approve] → running → 実行ジョブ → evaluating → 評価ジョブ
                         │                                                        ├── PASS → done
                         │                                                        ├── RETRY → reshaping（ループ）
                         │                                                        ├── BLOCKED → suspend (needs_input)
                         │                                                        └── ABORT → done
                         ├── [scoped + 手動承認が必要] → suspend (approval_required)
                         ├── [needs_input] → suspend (needs_input)
                         └── [reshaping（問題なし）] → done
```

auto_approve の判定ロジック:
- `orchestration.auto_approve = false` → 常に手動承認
- `orchestration.require_first_approval = true`（デフォルト）かつ `run_count = 0` → 初回は手動承認
- それ以外 → 自動承認

### タスクステータス遷移（スキル側で管理）

dispatch 時の `pending → in_progress` はスキル側で実行する（dispatcher CLI はタスク管理を行わない）。

```
dispatch 時（スキル側）: pending → in_progress
Lifecycle done(PASS):    in_progress → done
Lifecycle done(ABORT/max_runs): in_progress → aborted
```

### suspend 理由

| 理由 | 説明 | resume 時の動作 |
|------|------|----------------|
| `approval_required` | 手動承認が必要 | 実行ジョブをディスパッチ |
| `needs_input` | ユーザ入力が必要 | 再精査ジョブをディスパッチ |
| `project_confirmation` | プロジェクト判定の確認 | 指定プロジェクトで精査開始 |

### 精査ジョブの動作

精査ジョブは以下を実行する:

1. コンテキストファイル（`.context.md`）とプロジェクト定義を読み込み
2. 必要に応じて作業ディレクトリ配下のソースコードを調査
3. 未決事項を分析:
   - **未決事項がある場合**: `## 未決事項` にチェックボックス形式で質問を記載し、`needs_input` に遷移
   - **未決事項がない場合**: `## 概要`、`## 事前条件`、`## 達成条件`、`## 完了時アクション` を記載し、`scoped` に遷移。`## 実行プロンプト` も同時に生成
4. コンテキストファイルを更新

### 達成条件の記述ルール

- `manual` 以外のプロジェクトのタスクは AI エージェントが実装・実行する前提
- **達成条件は AI エージェント自身がローカルで検証可能な内容** にすること
- OK: ファイル内容の確認、YAML/JSON のパース検証、テスト実行（`dotnet test`, `npm test` 等）、ビルド成功、`az pipelines run` + 結果確認
- NG: ブラウザでの手動確認、外部サービスの目視確認など、エージェントが実行できない操作

### 再精査（`run_count > 0`）

ジョブ実行後に `reshaping` に戻ったタスク（`run_count > 0`）は、実行履歴を踏まえた再精査が行われる:
- `## 実行履歴` セクションの内容（成功/失敗、結果要約）を参照
- レビュー指摘や不具合があれば、達成条件・実行プロンプトを修正して `scoped` に遷移
- 問題がなければ、ユーザーに完了確認を促す

### manual プロジェクト

`working_directory` が未設定のプロジェクトは manual 扱い。精査ジョブはスキップされ、メインセッションで直接処理する:
- `done` に直接遷移
- 完了時アクション（データソース側のステータス更新等）は通常通り実行する

---

## 4. resume（ライフサイクル再開）

suspend 中のライフサイクルを再開する。

### 手順

1. suspend 中のライフサイクルを確認:
   ```bash
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher status
   ```

2. `needs_input` の場合: コンテキストファイルの未決事項に回答し `[x]` に更新

3. ライフサイクルを再開:
   ```bash
   # 通常の再開（needs_input 回答済み / approval_required 承認）
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher resume --id lc-1

   # コンテキスト更新付きの再開（更新済みファイルを渡す）
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher resume --id lc-1 --context-file /path/to/updated.md

   # プロジェクト確認の場合（project_confirmation）
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher resume --id lc-1 --project correct-project-id
   ```

---

## 5. ステータス確認

タスク一覧やジョブ状況を表示する。

### タスク一覧

1. `tasks/index.jsonl` を読み込み、ステータス別に集計・表示

2. 必要に応じて特定タスクの `tasks/{id}.md` の詳細を表示

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

## 6. タスク操作（データソース側）

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

## 7. 設定管理

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

## 8. 対話セッション

ジョブ管理の対象外で対話セッションを起動する。

### 手順

```bash
python3 ~/.claude/skills/my-tasks/scripts/dispatcher open --project bo [--session main]

# サンドボックスプロファイルを上書きして対話セッションを起動
python3 ~/.claude/skills/my-tasks/scripts/dispatcher open --project bo --sandbox-profile unrestricted
```
