# 操作リファレンス

タスク管理スキルが提供する11のオペレーションの詳細手順。

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

   # 特定データソースのみ処理（他データソースのタスクには影響しない）
   python3 ~/.claude/skills/my-tasks/scripts/sync-tasks.py \
     --repo ~/.local/share/my-tasks \
     --input /tmp/my-tasks-sync.jsonl \
     --datasource jira,ms-todo
   ```

3. スクリプトのレポートを確認:
   - `auto_assigned`: `project_mapping` でプロジェクトが特定できたタスク
   - `needs_review`: プロジェクトが特定できなかったタスク → ユーザーに確認
   - `vanished`: 消失タスク（full モード: インデックスと Markdown から削除済み）
   - `gc`: GC で除去された done タスク（append モード）

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
   - Outlook: `msgraph mail update --message-id {remote_id} --is-read true`
   - Gmail: `google mail modify {remote_id} --remove-label UNREAD --account {alias}`
     （google-cli に `modify` コマンド未実装の場合はスキップ）

5. アクション対象のメールを JSONL に出力し `sync-tasks.py` でタスク化:
   ```bash
   python3 ~/.claude/skills/my-tasks/scripts/sync-tasks.py \
     --repo ~/.local/share/my-tasks \
     --input /tmp/mail-triage.jsonl \
     --datasource mail-outlook,mail-gmail-nvt,mail-gmail-qzl
   ```
   `--datasource` でメール系のみ指定し、JIRA/To Do のタスクに影響を与えない

### 注意

- メールトリアージはエージェント対話が必要なため `fetch-all.sh` には含めない
- Gmail の既読化（`google mail modify`）は google-cli への機能追加が前提

---

## 3. タスク精査

`reshaping` タスクの精査、および全回答済み `needs_clarification` タスクの再精査を行う。
`refine.py` を使い、**1タスク1ジョブ**としてディスパッチャーに投入する。
各ジョブは専用のコンテキストでタスクを精査し、`scoped` 遷移時には実行プロンプトも同時に生成する。

### 手順

1. `refine.py` を実行してジョブを投入:
   ```bash
   # 全 reshaping タスクを精査ジョブとして投入
   python3 ~/.claude/skills/my-tasks/scripts/refine.py \
     --repo ~/.local/share/my-tasks

   # 特定タスクのみ
   python3 ~/.claude/skills/my-tasks/scripts/refine.py \
     --repo ~/.local/share/my-tasks \
     --task 20260301-001

   # 全回答済み needs_clarification タスクも含める
   python3 ~/.claude/skills/my-tasks/scripts/refine.py \
     --repo ~/.local/share/my-tasks \
     --include-clarified

   # ドライラン（プロンプトを表示するだけで投入しない）
   python3 ~/.claude/skills/my-tasks/scripts/refine.py \
     --repo ~/.local/share/my-tasks \
     --dry-run
   ```

2. ジョブ完了を待機（必要に応じて）:
   ```bash
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py status
   ```

3. 結果を確認:
   - `needs_clarification` に遷移したタスク → ユーザーに質問を提示し、回答を収集
   - `scoped` に遷移したタスク → 実行プロンプトが生成済み。操作5（承認）へ

4. ユーザーが `needs_clarification` タスクの質問に回答したら:
   - 回答済みの項目を `[x]` に更新し、回答を追記
   - 全項目が `[x]` になったら `refine.py --include-clarified` で再精査

### 精査ジョブの動作

各ジョブは以下を実行する（`refine.py` がプロンプトを自動組み立て）:

1. タスク md とプロジェクト定義を読み込み
2. 必要に応じて作業ディレクトリ配下のソースコードを調査
3. 未決事項を分析:
   - **未決事項がある場合**: `## 未決事項` にチェックボックス形式で質問を記載し、`needs_clarification` に遷移
   - **未決事項がない場合**: `## 概要`、`## 事前条件`、`## 達成条件`、`## 完了時アクション` を記載し、`scoped` に遷移。`## 実行プロンプト` も同時に生成
4. `tasks/{id}.md` と `tasks/index.jsonl` を更新

### 達成条件の記述ルール

- `manual` 以外のプロジェクトのタスクは AI エージェントが実装・実行する前提
- **達成条件は AI エージェント自身がローカルで検証可能な内容** にすること
- OK: ファイル内容の確認、YAML/JSON のパース検証、テスト実行（`dotnet test`, `npm test` 等）、ビルド成功、`az pipelines run` + 結果確認
- NG: ブラウザでの手動確認、外部サービスの目視確認など、エージェントが実行できない操作

### 再精査（`run_count > 0`）

ジョブ実行後に `reshaping` に戻ったタスク（`run_count > 0`）は、実行履歴を踏まえた再精査が行われる:
- `## 実行履歴` セクションの内容（成功/失敗、結果要約）を参照
- レビュー指摘や不具合があれば、達成条件・実行プロンプトを修正して `scoped` に遷移
- 問題がなければ、ユーザーに完了確認を促す（操作9 へ）

### manual プロジェクト

`working_directory` が未設定のプロジェクトは manual 扱い。`refine.py` はスキップし、メインセッションで直接処理する:
- `scoped` / `approved` / `running` をスキップし `done` に直接遷移
- 完了時アクション（操作9）を実行する

---

## 4. プロンプト再生成

`scoped` タスクの実行プロンプトを再生成する。通常は操作3（精査）で自動生成されるため、このステップは修正が必要な場合にのみ使用する。

### 手順

1. `tasks/index.jsonl` から `status=scoped` のタスクを一覧

2. 対象タスクの `tasks/{id}.md` を読み込み

3. 未決事項の回答、事前条件、達成条件を元に実行プロンプトを再生成

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

   # サンドボックスプロファイルを上書き指定（例: unrestricted で調査タスクを実行）
   python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py run --task 20260301-001 --sandbox-profile unrestricted
   ```

3. `index.jsonl` と `.md` の status を `running` に更新

### ジョブ完了後の処理

ジョブ完了（成功・失敗問わず）後、タスクを `reshaping` に戻す:

1. ディスパッチャーのジョブ結果を確認（`dispatcher.py status` または `dispatcher.py log --id {dispatch_id}`）

2. `tasks/{id}.md` の `## 実行履歴` セクションに結果を追記:
   ```markdown
   ### Run {run_count + 1}

   - 日時: {finished_at}
   - 結果: {成功 or 失敗}
   - 終了コード: {exit_code}
   - 要約: （ジョブログから主要な結果を要約）
   ```

3. `index.jsonl` の `run_count` をインクリメント

4. `index.jsonl` と `.md` の status を `reshaping` に更新

### 対話セッション

ジョブ管理の対象外で対話セッションを起動する場合:
```bash
python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py open --project bo [--session main]

# サンドボックスプロファイルを上書きして対話セッションを起動
python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py open --project bo --sandbox-profile unrestricted
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

## 9. 完了確認・完了時アクション

`reshaping` タスク（`run_count > 0`）の結果を確認し、完了と判断した場合に `done` に遷移させ、後処理を実行する。

### 手順

1. `tasks/index.jsonl` から `status=reshaping` かつ `run_count > 0` のタスクを一覧

2. 各タスクの `tasks/{id}.md` の `## 実行履歴` セクションを確認し、ユーザーに結果を提示

3. ユーザーが完了を確認した場合:
   a. `## 完了時アクション` セクションに記載されたアクションを実行:
      - データソース側のステータス更新（操作8を活用）
      - PR の作成
      - 通知の送信
      - etc.
   b. `index.jsonl` と `.md` の status を `done` に更新

4. ユーザーが再作業が必要と判断した場合:
   - タスクは `reshaping` のまま。操作3（タスク精査）で再精査 → `scoped` → 操作5 → 操作6 のフローを繰り返す

5. 完了後、必要に応じてタスク収集（操作1）を実行して GC させる

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

## 11. 精査対象選択

`pending` タスクを `reshaping`（精査対象）にする。ユーザが明示的に精査対象を選択するステップ。

### 手順（pending → reshaping）

1. 対象タスクの `tasks/index.jsonl` エントリを確認し、`status` が `pending` であることを確認

2. `index.jsonl` の当該エントリの `status` を `reshaping` に更新

3. `tasks/{id}.md` の `- Status:` 行を `reshaping` に更新

### 制約

- `pending` → `reshaping` 遷移は `status=pending` のタスクのみ可
- git commit は不要（tasks/ は gitignore 対象）

### 例

- 「これを精査して」「次にやる」→ `pending` → `reshaping`（その後、操作3 タスク精査を実行）
