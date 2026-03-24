# my-tasks-skill v2 設計書

## 背景と動機

v1 の運用を通じて以下が判明した:

- タスクの粒度は開発・事務ともに小さい（GitHub flow の1PR 程度）
- evaluate はほぼ失敗しない（達成条件の齟齬がほぼない）
- Task → Generation → Lifecycle → Phase → Job の5層構造は過剰
- Lifecycle のステートマシン（planning → planned → phase_executing → phase_evaluating → suspend → done）の複雑さに対してリターンが少ない
- Plan は対話的なフィードバックと微調整が必要なことが多く、非対話実行は不向き

これらの知見を踏まえ、オーケストレーション層を大幅に簡素化する。

## 設計原則

1. **1層構造**: Task のみ。Generation / Lifecycle / Phase を廃止
2. **Dispatcher は汎用ジョブランナー**: プロンプト構築はスキル側、実行環境（sandbox / worktree / host-cmd）は Dispatcher 側
3. **Plan は常に対話的**: ダッシュボードから対話セッションで実施。承認フロー不要
4. **Feedback はグループ化**: 収集タイミングごとにグループ化し、差分を識別可能に
5. **境界の明確化**: my-tasks-skill のスコープは「タスク → PR」。レビュー・マージ・デプロイは外側

---

## タスクモデル

### ステータス

```
pending → in_progress → done
                      → aborted
```

- `pending`: データソースから取り込まれた状態、または Plan/Dispatch 待ち
- `in_progress`: ジョブ実行中、Plan 中、Feedback 待ちなど、作業が進行中の状態
- `done`: 完了
- `aborted`: 中止

再オープン: done タスクと同じ remote_id が datasource から再出現した場合、新規タスクとして作成する（v1 の generation インクリメントは廃止）。

### タスクインデックス (`tasks/index.jsonl`)

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `id` | string | Yes | タスク ID（`YYYYMMDD-NNN` 形式） |
| `remote_id` | string | No | データソース内での一意識別子 |
| `datasource_id` | string | Yes | データソースの識別子 |
| `title` | string | Yes | タスクタイトル |
| `status` | string | Yes | `pending` / `in_progress` / `done` / `aborted` |
| `project_id` | string | No | 紐づくプロジェクト ID |

v1 からの削除: `lifecycle_id`, `run_count`, `generation`

### タスク YAML (`tasks/{id}.yaml`)

v1 の context YAML（ランタイム）とタスク YAML（リポジトリ）を統合。タスク YAML が唯一の真実の源。

| フィールド | 型 | 説明 |
|---|---|---|
| `id` | string | タスク ID |
| `remote_id` | string | データソース内での一意識別子 |
| `datasource_id` | string | データソースの識別子 |
| `project_id` | string | プロジェクト ID |
| `title` | string | タスクタイトル |
| `status` | string | ステータス |
| `description` | string | タスクの説明（Plan で精査） |
| `preconditions` | array | 事前条件リスト |
| `acceptance_criteria` | array | 達成条件リスト |
| `completion_actions` | array | 完了時アクションリスト |
| `execute_prompt` | string | 実行プロンプト（Plan で生成） |
| `pr_url` | string | PR の URL |
| `branch` | string | 作業ブランチ名 |
| `dispatch_id` | string | 現在のジョブの dispatch_id |
| `session_id` | string | Claude Code セッション ID（UUID 形式。Resume で再開に使用） |
| `feedback` | array | フィードバック（グループ化、下記参照） |
| `feedback_cursor` | object | ソースごとの最終取得タイムスタンプ |
| `history` | array | 実行履歴 |

v1 からの削除: `generation`, `open_questions`, `execute_prompt` のセマンティクス変更（phases 用 → 単一プロンプト）
v1 からの統合: context YAML の `meta`, `phases`, `previous_generations` を廃止

### フィードバック構造

フィードバックは収集タイミングごとにグループ化する。

```yaml
feedback:
  - collected_at: "2026-03-24T10:30:00+09:00"
    items:
      - source: "github_pr"
        author: "reviewer-a"
        timestamp: "2026-03-24T10:25:00+09:00"
        body: "エラーハンドリングが不足..."
      - source: "github_pr_review"
        author: "reviewer-b"
        timestamp: "2026-03-24T10:28:00+09:00"
        body: "[CHANGES_REQUESTED] テストケースを追加して..."
  - collected_at: "2026-03-24T15:00:00+09:00"
    items:
      - source: "github_pr"
        author: "reviewer-a"
        timestamp: "2026-03-24T14:55:00+09:00"
        body: "まだこの部分が..."
```

- `collected_at`: 収集実行時のタイムスタンプ（グループ識別子として機能）
- Dispatch 時に `collected_at` を指定することで、対象フィードバックグループを特定
- `feedback_cursor` は従来通りソースごとのタイムスタンプで重複排除

### 実行履歴

```yaml
history:
  - dispatch_id: "bo-3"
    started_at: "2026-03-24T10:00:00+09:00"
    finished_at: "2026-03-24T10:15:00+09:00"
    exit_code: 0
    summary: "API エンドポイント実装完了、PR作成済み"
  - dispatch_id: "bo-5"
    started_at: "2026-03-24T16:00:00+09:00"
    finished_at: "2026-03-24T16:10:00+09:00"
    exit_code: 0
    summary: "レビュー指摘対応（fb-20260324T1500）"
```

---

## オーケストレーション

Task に対する4つの操作で構成。全てスキル側（対話セッション or ダッシュボード）が起点。

### Plan

対話セッションで実行。タスク YAML を精査・補完し、実行プロンプトを生成する。

**フロー**:
1. ダッシュボードの Plan ボタン → `open` コマンドで対話セッション起動
2. 対話セッション内で:
   - タスク YAML を読み込み
   - 作業ディレクトリのソースコードを調査
   - ユーザーと対話しながら計画を立案
   - タスク YAML の `description`, `preconditions`, `acceptance_criteria`, `execute_prompt` を更新
3. セッション終了 → ダッシュボードに戻る

**成果物**: タスク YAML の `execute_prompt` フィールドに実行プロンプトが書き込まれた状態

### Dispatch

非対話ジョブとして実行。タスク YAML の `execute_prompt` を Dispatcher に渡す。

**フロー**:
1. ダッシュボードの Dispatch ボタン（または スキルの対話操作）
2. スキル側:
   - タスク YAML から `execute_prompt` を読み込み
   - タスクステータスを `in_progress` に更新
   - UUID 形式の `session_id` を生成
   - Dispatcher の `run` コマンドにプロンプトと `session_id` を渡す（ファイルまたは標準入力）
   - 返却された `dispatch_id` と `session_id` をタスク YAML に記録
3. Dispatcher 側:
   - sandbox 環境を構築（sandbox_profile 解決、worktree 作成、host-cmd broker 登録）
   - `claude -p "{prompt}" --session-id "{session_id}" --append-system-prompt "{環境情報}"` を実行
   - ジョブ完了時に exit code を返却

**プロンプトの受け渡し**:
- 標準入力: `echo "{prompt}" | dispatcher run --project bo`
- ファイル: `dispatcher run --project bo --prompt-file /path/to/prompt.md`

プロンプトが大きくなる可能性があるため、ファイル渡しをサポートする。

### Resume

Dispatch したジョブの Claude Code セッションを再開し、同一コンテキスト上でちょっとした手直しを行う。

**フロー**:
1. ジョブ完了後、結果を確認
2. タスク YAML の `session_id` を使い、`claude --resume "{session_id}"` でセッションを再開
3. 同一コンテキスト（会話履歴・ファイル状態）を引き継いだ状態で対話的に修正
4. セッション終了

**用途**: コミットメッセージの修正、軽微なバグ修正、PR の説明文更新など。
同一セッションのコンテキスト内で対応できる範囲の調整に限定する。
それ以上の修正が必要な場合は、PR にコメントして Feedback フローに回す。

**セッション ID の管理**:
- Dispatch 時に UUID 形式の `session_id` を生成し、`claude --session-id` で指定
- タスク YAML に `session_id` を記録
- Resume 時に `claude --resume "{session_id}"` で再開

### Feedback

フィードバックを収集し、対応ジョブを Dispatch する。

**フロー**:
1. フィードバック収集（`collect-feedback.py`）
   - PR コメント（GitHub / Bitbucket）、Jira コメントを収集
   - `feedback_cursor` 以降の新規コメントを抽出
   - 収集結果をタスク YAML の `feedback` に新しいグループとして追加
2. フィードバック対応プロンプトの生成
   - 今回のフィードバックグループ（最新の `collected_at`）を特定
   - 元の `execute_prompt` + フィードバック内容から対応プロンプトを構築
3. Dispatcher の `run` にプロンプトを渡してジョブ実行

**繰り返し**: Feedback は複数回実行可能。毎回新しいグループが追加され、ジョブには最新グループの `collected_at` を渡すことで「今回対応すべきフィードバック」を識別できる。

---

## Dispatcher

### 責務

- ジョブの実行管理（queued → running → done/failed）
- sandbox 環境の構築（プロファイル解決、bwrap 引数構築）
- worktree の管理（作成・クリーンアップ）
- host command broker のトークン管理
- `claude` コマンドの組み立てと実行
- 対話セッションの tmux 管理

### 責務外

- プロンプトの構築（スキル側）
- タスクのステータス管理（スキル側）
- 実行結果の評価（廃止。hooks または外部エージェントで対応）

### コマンド

| コマンド | フィールド | 説明 |
|---------|-----------|------|
| `run` | `project_id`, `prompt`(stdin/file), `session_id?` | ジョブを実行 |
| `resume` | `project_id`, `session_id` | 完了済みセッションを tmux で再開 |
| `open` | `project_id`, `session?`, `worktree?` | 対話セッションを tmux で開く |
| `status` | | 全ジョブのステータスを返す |
| `cancel` | `dispatch_id` | キュー内ジョブを取消 |
| `kill` | `dispatch_id` | 実行中ジョブを強制停止 |
| `kill-all` | | 全ジョブを強制停止 |
| `wait` | `dispatch_id` | ジョブ完了まで接続を保持 |
| `log` | `dispatch_id` | ジョブログを表示（CLI のみ） |

v1 からの削除: `dispatch`（Lifecycle 用コマンド）
v1 からの変更: `resume` を再定義（Lifecycle 再開 → セッション再開）
v1 からの変更: `run` がプロンプトを stdin またはファイルで受け取る（`--prompt-file` オプション追加）、`--session-id` オプション追加
v1 からの変更: `open` に `--worktree` オプション追加

### `run` の実行フロー

1. リクエスト受信（project_id + prompt + session_id?）
2. プロジェクト設定から sandbox_profile、working_directory、host_commands 等を解決
3. dispatch_id を生成
4. session_id が未指定の場合は UUID を生成
5. worktree を作成（git リポジトリの場合）
6. 環境情報の system prompt を構築（sandbox 制約、認証情報、ホストコマンド等）
7. `claude -p "{prompt}" --session-id "{session_id}" --append-system-prompt "{環境情報}"` を subprocess で実行
8. 完了時に exit code + session_id を返却

### `resume` の実行フロー

1. リクエスト受信（project_id + session_id）
2. プロジェクト設定から sandbox_profile 等を解決
3. tmux セッションにウィンドウを作成
4. `sandbox ... -- claude --resume "{session_id}"` を実行（対話モード）

### `open` の実行フロー

1. リクエスト受信（project_id + session? + worktree?）
2. プロジェクト設定から sandbox_profile 等を解決
3. tmux セッションにウィンドウを作成
4. worktree 指定がある場合はそのパスを working_directory として使用
5. `sandbox ... -- claude` を実行

### ジョブ状態

```
queued → running → done
                 → failed
```

インメモリ管理。永続化なし（v1 と同じ）。

### Job データモデル

| フィールド | 型 | 説明 |
|---|---|---|
| `dispatch_id` | string | `{project_id}-{連番}` |
| `project_id` | string | プロジェクト ID |
| `status` | string | `queued` / `running` / `done` / `failed` |
| `pid` | int? | 子プロセス PID |
| `exit_code` | int? | 終了コード |
| `working_dir` | string | 作業ディレクトリ（worktree パス） |
| `branch` | string? | worktree のブランチ名 |
| `started_at` | string? | 開始日時 |
| `finished_at` | string? | 終了日時 |

v1 からの削除: `job_type`, `lifecycle_id`, `run`, `prompt`, `sandbox_profile_id`, `env_files`, `host_commands`, `extra_binds`（内部実装の詳細はデータモデルに露出させない）

### system prompt（Dispatcher が構築する環境情報部分）

```
あなたはサンドボックス環境で実行されています。

実行環境:
- 作業ディレクトリ: {working_directory}
- ネットワーク: {保護あり (netns + proxy) | ホストネットワーク直接}

制約事項 (ネットワーク保護あり):
- ネットワーク: GitHub/Bitbucket SSH と HTTP プロキシ経由の HTTPS のみ利用可能
- ファイル: 作業ディレクトリ内のファイルのみ変更可能

認証情報:
- `pass show <entry>` で認証情報を取得できます
  {allowed_patterns}

ホストコマンド:
- 以下のコマンドがホスト側で実行されます:
  {host_commands}

作業が完了したら、変更をコミットしてください。
```

v1 からの削除: 結果ファイルセクション（result.json 廃止）、ジョブタイプ別テンプレート

---

## Sandbox

### 変更点

Claude への依存をなくし、コマンドは常に引数で渡す。

```bash
# v1: sandbox が claude を暗黙的に起動
sandbox --sandbox-profile restricted-default --working-dir /path/to/repo

# v2: 実行するコマンドを明示的に指定
sandbox --sandbox-profile restricted-default --working-dir /path/to/repo -- claude -p "..."
sandbox --sandbox-profile restricted-default --working-dir /path/to/repo -- bash -c "npm test"
```

sandbox は純粋なプロセス隔離ツールになる。何を実行するかは呼び出し側が決める。

---

## プロジェクト定義

### スキーマ変更

| フィールド | 変更 | 説明 |
|---|---|---|
| `orchestration` | **削除** | auto_approve / require_first_approval / max_runs_per_generation は全て不要 |
| `worktree` | **維持** | `enabled` と `branch_template` でブランチ自動導出を制御。remote_id の形式がデータソース依存のため、テンプレートはプロジェクト定義に必要 |
| その他 | 維持 | project_id, name, description, repositories, working_directory, sandbox_profile, env, extra_binds, host_commands |

---

## スキルオペレーション

### 1. タスク収集
変更なし。fetch-all.sh → sync-tasks.py のパイプライン。
ただし generation 関連のロジックを削除（再オープンは新規タスク作成）。

### 2. メールトリアージ
変更なし。

### 3. Plan（新規）
ダッシュボードから対話セッションを起動し、タスクを精査・計画する。
成果物はタスク YAML の `execute_prompt`。

### 4. Dispatch（簡素化）
タスク YAML の `execute_prompt` を Dispatcher の `run` に渡す。
v1 の Lifecycle 開始 → 計画 → 承認 → 実行 → 評価のチェーンが、単一の `run` 呼び出しに。

### 5. Resume（新規）
`open` で対話セッションを起動し、ジョブ結果に対する軽微な修正を行う。

### 6. Feedback（簡素化）
フィードバック収集 → グループ化 → 対応ジョブ Dispatch。
v1 と比べ、generation の管理が不要。グループの `collected_at` で差分を識別。

### 7. ステータス確認
変更なし（Lifecycle 表示が消えてジョブ一覧のみに）。

### 8. タスク操作
変更なし。

### 9. 設定管理
プロジェクト定義から `orchestration` フィールドを削除。`worktree` フィールドは維持。

### 10. 対話セッション
`open` に `--worktree` オプション追加（Resume 対応）。

---

## ダッシュボード

### タスクに対するアクション

| ボタン | 操作 | 条件 |
|--------|------|------|
| Plan | 対話セッション起動 → 計画立案 | status = pending, execute_prompt が空 |
| Dispatch | `run` ジョブ実行 | execute_prompt が存在 |
| Resume | セッション再開（`claude --resume`） | session_id が存在、直前のジョブが完了 |
| Feedback | 収集 → 対応ジョブ実行 | pr_url が存在 |
| Open | 対話セッション起動 | いつでも |
| Done | status → done | いつでも |
| Abort | status → aborted | いつでも |

### ジョブ状態表示

Lifecycle 表示を廃止し、ジョブ一覧（running / queued / done / failed）のみ表示。
タスクとジョブの紐付けは `dispatch_id` で参照。

---

## 廃止される概念・ファイル

| 概念/ファイル | 理由 |
|---|---|
| Lifecycle | オーケストレーションの簡素化 |
| Phase | タスク粒度が小さく不要 |
| Generation | Feedback グループで代替 |
| result.json | evaluate 廃止、exit code で判定 |
| context YAML (ランタイム) | タスク YAML に統合 |
| lifecycles.jsonl | Lifecycle 廃止 |
| job_type (plan/execute/evaluate) | 全て単一の run ジョブ |
| orchestration (プロジェクト定義) | 自動承認フロー廃止 |
| PLAN_TEMPLATE 等のプロンプトテンプレート | Plan が対話的になるため不要 |
| suspend / suspend_reason | Lifecycle 廃止 |

---

## 残課題（スコープ外）

### レビューエージェント
PR に対してレビューエージェントを走らせる。GitHub webhook → レビューエージェント → Feedback として my-tasks-skill にフィード。

### 外側のループ
タスク分解 → my-tasks-skill へフィード → PR レビュー完了検知 → Feedback 指示 → PR マージを制御するエージェント。my-tasks-skill は「タスク → PR」の境界に集中し、上位の制御は外部に委ねる。

### hooks による完了基準チェック
PR 作成やテスト作成の確認を Claude Code の hooks で実現する。プロジェクトごとに異なるワークフローを適用可能。本プロジェクトのスコープ外とし、各プロジェクト側で設定する。

---

## 移行

### 方針

一気に書き直す。実行中のタスクは存在しないことを前提とする。

### 手順

1. Dispatcher の書き直し
   - lifecycle.py 削除
   - models.py: Lifecycle / job_type 関連を削除、Job を簡素化
   - executor.py: system prompt 構築を環境情報のみに限定
   - server.py: dispatch / resume コマンド削除、run のプロンプト受け渡し変更
   - prompt.py: 環境情報テンプレートのみに簡素化
   - open コマンドに worktree オプション追加

2. スキル側の書き直し
   - SKILL.md: オペレーション定義を更新
   - references/operations.md: 手順を全面更新
   - references/schemas.md: スキーマを全面更新
   - references/architecture.md: アーキテクチャを更新
   - references/dispatcher-design.md: Dispatcher 設計を全面更新

3. スクリプトの更新
   - sync-tasks.py: generation ロジック削除
   - collect-feedback.py: グループ化対応
   - add-feedback.py: グループ化対応
   - reopen-task.py: 削除（generation 廃止）
   - migrate-generations.py: 削除
   - lib/task_store.py: generation 関連削除、feedback 構造変更

4. ダッシュボードの更新
   - Lifecycle 表示の削除
   - Plan / Dispatch / Resume / Feedback ボタンの実装
   - ジョブ一覧表示の簡素化

5. Sandbox の更新
   - コマンド引数渡し対応

6. タスクデータの移行
   - 全タスクを再収集（tasks/ を削除して fetch → sync）
