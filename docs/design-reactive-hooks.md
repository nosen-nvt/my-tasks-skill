# リアクティブフック設計

## 背景と動機

### 課題 1: CI/CD 自動化

タスクの Dispatch で作成された PR に対して、以下の自動化を実現したい:

- **CI/CD 監視**: Actions の状態をタスクデータに反映
- **CI/CD 修正**: Actions 失敗時に自動修正ジョブを起動
- **PR レビュー**: Actions 成功後にレビューエージェントを起動

当初 GitHub webhook での実現を検討したが、webhook はリポジトリ/Organization 単位であり、
個人ツールのスコープに合わない。代わりに、タスクデータの変化に反応するフック機構で実現する。

### 課題 2: オーケストレーションの統一

現行の orchestrator は Plan/Dispatch/Feedback/Resume/Complete/Abort を個別のメソッドとして実装しており、
CI/CD 自動化を追加すると新たなメソッドやコードパスが増える。

全ての操作を「アクション → 状態変更 → フック評価」の統一モデルで表現することで、
既存操作と新規自動化を同じ仕組みで扱えるようにする。

## 設計原則

1. **ボタン = dispatch(action)**: ダッシュボードのボタンはアクションを送るだけ。状態をどう変えるかは知らない
2. **Reducer = 状態遷移の一元管理**: 全ての状態変更は reducer を通る。無効なアクションは reject
3. **フック = 副作用**: 状態変更後に条件を満たすフックが起動。フックが追加のアクションを dispatch できる
4. **フックの冪等性はフック自身が担保**: 基盤はフックの実行状態を追跡しない。フックは起動されたら自分で「やるべきことがあるか」を判断する
5. **React の useReducer + useEffect のアナロジー**: データは state、ボタンは dispatch、フックは useEffect

---

## アーキテクチャ

### プロセス構成

現行の dispatcher を orchestrator に統合し、常駐プロセスを増やさない。

```
現行:  Dashboard ──socket──→ Dispatcher (常駐)
新規:  Dashboard ──socket──→ Orchestrator (常駐)
```

```
┌─────────────────────┐         ┌──────────────────────────────┐
│  Dashboard          │         │  Orchestrator (常駐プロセス)   │
│  (Web UI + API)     │──sock──→│                              │
│  - 静的ファイル配信  │         │  ┌─────────┐                 │
│  - タスク表示        │         │  │ Reducer  │ 状態遷移の定義  │
│                     │         │  └────┬─────┘                │
│                     │         │       ▼                      │
│                     │         │  ┌──────────────┐            │
│                     │         │  │ Hook         │ 条件評価    │
│                     │         │  │ Evaluator    │ → フック起動 │
│                     │         │  └────┬─────────┘            │
│                     │         │       ▼                      │
│                     │         │  ┌──────────────┐            │
│                     │         │  │ Job Executor │ sandbox 実行│
│                     │         │  │ (旧dispatcher)│ tmux 管理  │
│                     │         │  └──────────────┘            │
└─────────────────────┘         └──────────────────────────────┘

CLI / フックスクリプト ──sock──→ Orchestrator
```

Orchestrator は以下を統合した単一の常駐プロセス:
- **Reducer**: アクションを受け取り、タスクデータを更新
- **Hook Evaluator**: タスクデータ変更後にフックを評価・起動
- **Job Executor**: サンドボックス内でのジョブ実行（旧 dispatcher の機能）
- **tmux 管理**: 対話セッションの管理
- **プロセス監視**: script フックのサブプロセス管理

### データフロー

```
Dashboard / CLI / フックスクリプト
        │
        │  dispatch(action) via Unix socket
        ▼
┌─────────────────┐
│  Reducer         │  (state, action) → new_state
│                  │  無効なアクションは reject
└────────┬────────┘
         │  state 変更を永続化
         ▼
┌─────────────────┐
│  Hook Evaluator  │  条件マッチするフックを起動
│                  │  フックは自身で冪等性を担保
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  フック           │  副作用を実行
│  (script/builtin)│  必要に応じて dispatch(action) で基盤に戻る
└─────────────────┘
```

---

## Reducer

### アクション定義

| アクション | 発行元 | 説明 |
|-----------|--------|------|
| `plan` | ダッシュボード | Plan 開始 |
| `dispatch` | ダッシュボード | ジョブ実行開始 |
| `request_resume` | ダッシュボード | Resume 要求 |
| `request_feedback` | ダッシュボード | Feedback 要求 |
| `done` | ダッシュボード | 完了 |
| `abort` | ダッシュボード | 中止 |
| `job_completed` | dispatch_job フック | ジョブ完了 |
| `feedback_collected` | feedback_collector フック | フィードバック収集完了、再 Dispatch へ |
| `clear_resume` | resume_session フック | Resume セッション終了 |
| `update_field` | フックスクリプト | タスクデータのフィールド更新（汎用） |

### 状態遷移表

```python
def reduce(task: TaskData, action: Action) -> TaskData | Error:
    match (task.status, action.type):
        # --- ダッシュボード操作 ---
        case ("pending", "plan"):
            task.status = "planning"

        case ("planning", "plan"):
            pass  # planning のまま（再精査）

        case ("planning", "dispatch"):
            task.status = "executing"

        case ("in_review", "request_resume"):
            task.resume_requested = True

        case ("in_review", "request_feedback"):
            task.feedback_requested = True

        case ("in_review", "done"):
            task.status = "done"

        case (_, "abort"):
            task.status = "aborted"

        # --- フックからのアクション ---
        case ("executing", "job_completed"):
            task.status = "in_review"

        case ("in_review", "feedback_collected"):
            task.status = "executing"
            task.feedback_requested = False

        case (_, "clear_resume"):
            task.resume_requested = False

        case (_, "update_field"):
            setattr(task, action.field, action.value)

        case _:
            return Error(f"無効なアクション: {action.type} (現在: {task.status})")
```

Reducer は純粋関数。テスト可能で、全遷移パターンを網羅的に検証できる。

---

## フック

### フック定義

```python
@dataclass
class Hook:
    id: str                # 一意識別子
    type: str              # "builtin" | "script" | "dispatch"
    when: dict             # 事前条件（タスクデータに対する条件式）
    command: str = ""      # type=script の場合のコマンド
    prompt_file: str = ""  # type=dispatch の場合のプロンプトファイル
```

### 事前条件の評価

```python
"when": {
    "status": "in_review",           # 完全一致
    "pr_url": {"not_empty": true},   # 非空
    "actions_status": {"eq": "failure"}  # 値比較
}
```

Hook Evaluator はタスクデータ変更のたびに全フックの `when` 条件を評価し、
条件一致したフックを起動する。

### 冪等性: フック自身の責務

基盤はフックの実行状態を追跡しない（hook_state のような仕組みは持たない）。
代わりに、各フックが起動時に「自分が今やるべきことがあるか」を自分で判断する。

条件が一致するたびにフックは起動されるが、フック内部で冪等性を担保する:

| フック | 冪等性チェック | 二重起動時の動作 |
|--------|-------------|----------------|
| plan_session | tmux 窓 `plan-{task_id}` が存在するか | 存在すれば何もしない |
| dispatch_job | task_id に紐づく running/queued ジョブがあるか | あれば何もしない |
| resume_session | tmux 窓 `resume-{task_id}` が存在するか | 存在すれば何もしない |
| abort_cleanup | running ジョブがあるか | なければ何もしない |
| feedback_collector | フィードバック収集中フラグ（PID ファイル等） | 実行中なら何もしない |
| ci_monitor | 監視プロセスが実行中か（PID ファイル等） | 実行中なら何もしない |
| ci_fix | task_id に紐づく running 修正ジョブがあるか | あれば何もしない |
| review_* | task_id に紐づく running レビュージョブがあるか | あれば何もしない |

この方式の利点:
- タスクデータの全てがアクションで決まる原則を維持
- 「回復が必要なもの」と「二重実行を避けるべきもの」をフックごとに適切に扱える
- 基盤のコードが単純（条件一致 → 起動、以上）

### ビルトインフック

共通パターンを組み込みフックとして提供する。

| ID | 条件 | 動作 |
|----|------|------|
| `plan_session` | status=planning | tmux 窓が存在しなければ Plan セッションを開く。`plan_session_id` があれば resume |
| `dispatch_job` | status=executing | running ジョブがなければ dispatch。完了を polling し、`job_completed` アクションを dispatch。プロンプト: 未対応フィードバックがあればフィードバック対応プロンプト、なければ execute_prompt |
| `resume_session` | resume_requested=true | tmux 窓が存在しなければ Resume セッションを開く。窓を polling し、閉じたら `clear_resume` を dispatch |
| `abort_cleanup` | status=aborted | running ジョブがあれば kill |
| `feedback_collector` | feedback_requested=true | フィードバック収集、タスクデータに追記、`feedback_collected` を dispatch |

### プロジェクト定義フック

プロジェクト設定（`projects/{id}.json`）で定義。

```json
{
  "hooks": [
    {
      "id": "ci_monitor",
      "type": "script",
      "command": "scripts/hooks/ci-monitor.sh {task_id}",
      "when": {
        "status": "in_review",
        "pr_url": {"not_empty": true},
        "actions_status": {"not_eq": "success"}
      }
    },
    {
      "id": "ci_fix",
      "type": "dispatch",
      "prompt_file": "prompts/ci-fix.md",
      "when": {
        "status": "in_review",
        "actions_status": {"eq": "failure"}
      }
    },
    {
      "id": "review_security",
      "type": "dispatch",
      "prompt_file": "prompts/review-security.md",
      "when": {
        "status": "in_review",
        "actions_status": {"eq": "success"}
      }
    },
    {
      "id": "review_quality",
      "type": "dispatch",
      "prompt_file": "prompts/review-quality.md",
      "when": {
        "status": "in_review",
        "actions_status": {"eq": "success"}
      }
    }
  ]
}
```

---

## フック実行タイプ

| type | 実行方法 | 用途 |
|------|---------|------|
| `builtin` | Orchestrator 内で直接実行 | Plan/Dispatch/Resume 等の組み込み操作 |
| `script` | ホスト側でシェルスクリプトを spawn | CI/CD 監視、データ更新。AI 不要な処理 |
| `dispatch` | Job Executor 経由で AI ジョブを実行 | CI/CD 修正、PR レビュー。AI が必要な処理 |

script フックと dispatch フックの違い:
- script: ホスト側で直接実行。タスクデータの読み書き、CLI コマンド実行が可能
- dispatch: サンドボックス内で claude を実行。`related_jobs` で追跡

### dispatch フックのプロンプト構築

dispatch フックの `prompt_file` はテンプレート。タスクデータのフィールドをプレースホルダとして参照できる:

```markdown
# CI/CD 修正

PR: {pr_url}
ブランチ: {branch}

## Actions エラー
{actions_error}

上記のエラーを修正し、コミット & push してください。
```

`actions_error` は ci_monitor スクリプトが `update_field` アクションでタスクデータに書き込む。
dispatch フックはタスクデータからプレースホルダを展開してプロンプトを構築する。

---

## タスクデータモデルの拡張

```yaml
# 既存フィールド
status: "in_review"
dispatch_id: "bo-3"
session_id: "uuid..."
pr_url: "https://..."
related_jobs: ["review-bo-1"]

# 新規フィールド
plan_session_id: "uuid..."       # Plan セッションの Claude セッション ID（resume 用）
resume_requested: false           # Resume フラグ
feedback_requested: false         # Feedback フラグ
actions_status: "success"         # CI/CD の状態 (pending|running|success|failure)
actions_error: ""                 # CI/CD 失敗時のエラー詳細（ci_monitor が書き込む）
```

注: hook_state フィールドは持たない。フックの冪等性はフック自身が担保する。

---

## シナリオ

### Plan → Dispatch → CI/CD 修正 → レビュー

```
1. ユーザーが Plan ボタンを押す
   → dispatch({type: "plan"})
   → reducer: status: pending → planning
   → hook 評価: plan_session 条件一致 → 起動
   → plan_session: tmux 窓なし → Plan セッション起動

2. ユーザーが Plan セッションで execute_prompt を記入、窓を閉じる
   → plan_session: 窓を polling → 閉じたことを検知（フック終了）
   → 次回の hook 評価で plan_session 再起動 → tmux 窓なし → 再度開く
     （ユーザーが再び閉じれば同じ。Dispatch するまでこのサイクル）

3. ユーザーが Dispatch ボタンを押す
   → dispatch({type: "dispatch"})
   → reducer: status: planning → executing
   → hook 評価: dispatch_job 条件一致 → 起動
   → dispatch_job: running ジョブなし → ジョブ dispatch

4. ジョブ完了（PR 作成済み）
   → dispatch_job: polling で完了検知
   → dispatch({type: "job_completed"})
   → reducer: status: executing → in_review
   → hook 評価:
     - ci_monitor: 条件一致 → 起動 → PID ファイルなし → 監視開始

5. CI/CD 監視が Actions 失敗を検知
   → dispatch({type: "update_field", field: "actions_status", value: "failure"})
   → hook 評価:
     - ci_fix: 条件一致 → 起動 → running 修正ジョブなし → 修正ジョブ dispatch
     - ci_monitor: 条件一致 → 起動 → PID ファイルあり → 何もしない

6. CI/CD 修正ジョブが push → Actions 再実行
   → ci_monitor: actions_status を "running" に更新
   → dispatch({type: "update_field", field: "actions_status", value: "running"})
   → hook 評価: 条件一致するフックなし

7. CI/CD 監視が Actions 成功を検知
   → dispatch({type: "update_field", field: "actions_status", value: "success"})
   → hook 評価:
     - review_security: 条件一致 → 起動 → running レビュージョブなし → dispatch
     - review_quality: 条件一致 → 起動 → running レビュージョブなし → dispatch（並行）
     - ci_monitor: 条件不一致 (actions_status=success) → 起動されない → 自然に終了

8. レビュー完了
   → レビューコメントが PR に投稿される
   → ユーザーが確認し、Done ボタンまたは Feedback ボタンを押す
```

### Resume

```
1. ユーザーが Resume ボタンを押す
   → dispatch({type: "request_resume"})
   → reducer: resume_requested = true
   → hook 評価: resume_session 条件一致 → 起動
   → resume_session: tmux 窓なし → Resume セッション起動

2. ユーザーがセッションで作業、窓を閉じる
   → resume_session: tmux 窓を polling → 閉じたことを検知
   → dispatch({type: "clear_resume"})
   → reducer: resume_requested = false
```

### Feedback

```
1. ユーザーが Feedback ボタンを押す
   → dispatch({type: "request_feedback"})
   → reducer: feedback_requested = true
   → hook 評価: feedback_collector 条件一致 → 起動

2. feedback_collector: PR コメント/Jira コメントを収集、タスクデータに追記
   → dispatch({type: "feedback_collected"})
   → reducer: status: in_review → executing, feedback_requested = false
   → hook 評価: dispatch_job 条件一致 → 起動
   → dispatch_job: running ジョブなし → フィードバック対応ジョブ dispatch

3. ジョブ完了
   → dispatch({type: "job_completed"})
   → reducer: status: executing → in_review
   → hook 評価: ci_monitor 等が再発動
```

---

## 無限ループ防止

フックが dispatch(action) → reducer → フック評価 → 同じフック起動、というループに陥るリスクがある。

防止策:
1. **フックの冪等性**: フック自身が「やることがない」と判断すれば即座に終了。副作用なし → アクション dispatch なし → ループしない
2. **再帰深度制限**: dispatch → 評価 → dispatch の連鎖に上限を設ける（例: 深度 5）
3. **ci_fix のリトライ上限**: プロジェクト定義で `max_retries` を設定可能にする。上限到達時はユーザーに通知

---

## 基盤の実装

### Orchestrator（常駐プロセス）

現行の dispatcher を統合した単一の常駐プロセス。

```python
class Orchestrator:
    """Reducer + Hook Evaluator + Job Executor を統合した常駐プロセス。"""

    # --- Reducer ---
    def dispatch_action(self, task_id: str, action: Action) -> Result: ...

    # --- Hook Evaluator ---
    def evaluate_hooks(self, task_id: str, old: TaskData, new: TaskData): ...

    # --- Job Executor (旧 dispatcher) ---
    async def execute_job(self, job: Job): ...
    async def kill_job(self, dispatch_id: str): ...

    # --- tmux 管理 ---
    def open_tmux_session(self, ...): ...
    def check_tmux_window(self, window_name: str) -> bool: ...

    # --- Unix socket サーバー ---
    async def handle_client(self, reader, writer): ...
```

### エントリポイント

```python
def dispatch_action(self, task_id: str, action: Action) -> Result:
    task_data = load_task_data(task_id)

    # Reducer
    new_task_data = reduce(task_data, action)
    if isinstance(new_task_data, Error):
        return new_task_data

    # 永続化
    save_task_data(task_id, new_task_data)

    # フック評価（条件一致 → 起動、フックが冪等性を担保）
    self.evaluate_hooks(task_id, old=task_data, new=new_task_data)

    return Ok()
```

### CLI インターフェース

フックスクリプトから Orchestrator にアクションを送る CLI:

```bash
# ステータスアクション
task-dispatch <task_id> plan
task-dispatch <task_id> abort

# フィールド更新
task-dispatch <task_id> update_field --field actions_status --value success
```

CLI は Unix socket 経由で Orchestrator にアクションを送信する。

### ダッシュボード API

```python
@app.post("/api/tasks/{task_id}/action/{action_type}")
async def api_action(task_id: str, action_type: str) -> JSONResponse:
    result = await orchestrator_send({
        "command": "dispatch_action",
        "task_id": task_id,
        "action": {"type": action_type},
    })
    return JSONResponse(result)
```

Dashboard は Orchestrator に対して Unix socket でアクションを送信する。
現行の dashboard → dispatcher 通信と同じパターン。

---

## 移行

### 現行構成

```
my-tasks-dispatcher.service  →  scripts/dispatcher/ (常駐)
my-tasks-dashboard.service   →  scripts/dashboard/  (常駐)
```

### 新構成

```
my-tasks-orchestrator.service →  scripts/orchestrator/ (常駐、dispatcher を統合)
my-tasks-dashboard.service    →  scripts/dashboard/    (常駐、薄い Web UI)
```

### 移行手順

1. `scripts/orchestrator/` を新規作成。dispatcher のコード（executor, models, tmux, sandbox_exec 等）を移動
2. Reducer + Hook Evaluator を orchestrator に追加
3. ビルトインフックを実装（plan_session, dispatch_job, resume_session, abort_cleanup, feedback_collector）
4. Dashboard の API を orchestrator へのアクション送信に書き換え
5. CLI ツール（task-dispatch）を作成
6. systemd ユニットを更新（dispatcher → orchestrator）
7. 現行の `lib/orchestrator.py` を廃止

---

## 実装順序

1. **Orchestrator 基盤**: dispatch_action, reducer, hook evaluator, Unix socket サーバー
2. **dispatcher 統合**: executor, models, tmux, sandbox_exec を orchestrator に移動
3. **ビルトインフック**: plan_session, dispatch_job, resume_session, abort_cleanup, feedback_collector
4. **タスクデータ拡張**: plan_session_id, resume_requested, feedback_requested, actions_status, actions_error
5. **ダッシュボード書き換え**: 各ボタンを dispatch(action) に変更
6. **CLI ツール**: task-dispatch コマンド
7. **プロジェクト定義フック**: ci_monitor, ci_fix, review
