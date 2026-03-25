# タスク状態遷移モデル v3

## 背景

v2 では `pending | in_progress | done | aborted` の 4 状態でタスクを管理していたが、
`in_progress` が「Plan 中」「ジョブ実行中」「ジョブ完了・レビュー待ち」を兼ねており、
並行タスクの進捗が判別できない問題があった。

本設計では状態を細分化し、各アクションと状態遷移を 1:1 で対応させる。

---

## 状態定義

| ステータス | 説明 |
|-----------|------|
| `pending` | データソースから取り込まれた直後。未着手 |
| `planning` | Plan セッションを開始済み。精査・計画中 |
| `executing` | ジョブ実行中 |
| `in_review` | ジョブ完了。結果確認・レビュー待ち |
| `done` | 完了 |
| `aborted` | 中止 |

```
pending ──Plan──→ planning ──Dispatch──→ executing ──(auto)──→ in_review ──Done──→ done
  │                  │                      │                      │
  │                  │                      │             Feedback──→ executing
  │                  │                      │                      │
  └──Abort─→ aborted └──Abort─→ aborted    └──Abort─→ aborted     └──Abort─→ aborted
```

### sync 時の初期ステータス

- `execute_prompt` なし → `pending`
- `execute_prompt` あり → `planning`（Plan 済みとして扱う）

---

## アクションと状態遷移

| アクション | 遷移 | 前提条件 | 備考 |
|-----------|------|---------|------|
| **Plan** | `pending → planning` | project_id 存在 | 対話セッション起動 |
| **Plan** (再) | `planning → planning` | | 再精査 |
| **Dispatch** | `planning → executing` | execute_prompt 存在, project_id 存在 | ジョブ実行開始 |
| **(auto)** | `executing → in_review` | ジョブ完了 (done/failed) | orchestrator callback |
| **Feedback** | `in_review → executing` | pr_url 存在 | フィードバック収集 → 再 Dispatch |
| **Done** | `in_review → done` | | |
| **Abort** | `pending/planning/executing/in_review → aborted` | | executing からは Job kill も実行 |
| **Resume** | (遷移なし) | `in_review`, session_id 存在 | セッション再開操作 |

### 廃止するアクション

- **Open**: 状態マッピングが曖昧で、実行結果の予測が困難なため廃止

---

## ダッシュボード: ボタン表示条件

### pending

| ボタン | 条件 | スタイル |
|--------|------|---------|
| Plan | project_id 存在 | primary |
| Abort | 常時 | danger |

### planning

| ボタン | 条件 | スタイル |
|--------|------|---------|
| Plan | 常時 | default (再精査) |
| Dispatch | execute_prompt 存在, project_id 存在 | primary |
| Abort | 常時 | danger |

### executing

| ボタン | 条件 | スタイル |
|--------|------|---------|
| Abort | 常時 | danger |

### in_review

| ボタン | 条件 | スタイル |
|--------|------|---------|
| Resume | session_id 存在 | primary |
| Feedback | pr_url 存在 | default |
| Done | 常時 | success |
| Abort | 常時 | danger |

### done / aborted

ボタンなし。

---

## 表示ステータス

フロントエンドでは `executing` 状態のタスクについて、ジョブの状態に応じた表示を行う:

| task.status | ジョブ状態 | 表示 |
|-------------|-----------|------|
| `pending` | - | pending |
| `planning` | - | planning |
| `executing` | running/queued | executing |
| `in_review` | - | in_review |
| `done` | - | done |
| `aborted` | - | aborted |

v2 では `in_progress` + `taskDisplayStatus()` で running を出し分けていたが、
v3 では `executing` 状態自体がジョブ実行中を意味するため、出し分けは不要。

---

## 自動遷移: executing → in_review

ジョブ完了時に自動でタスクステータスを `in_review` に更新する。

### 実装方式: Executor → Orchestrator Callback

1. `executor.py` のジョブ完了処理で `orchestrator.on_job_completed(dispatch_id)` を呼ぶ
2. orchestrator は dispatch_id からタスクを特定し、`executing → in_review` に遷移
3. `job_type: "run"` のジョブのみ状態遷移を発火（レビュー等の副作用ジョブは除外）

### タスク特定

現行ではジョブからタスクへの逆引きがない。以下のいずれかで解決:

- **A**: Job に `task_id` フィールドを追加（推奨）
- **B**: タスク一覧から `dispatch_id` で検索

---

## 関連ジョブ (related_jobs)

タスクの状態遷移に影響しないが、タスクに関連するジョブ（PR レビュー等）を追跡する。

### 用途

- PR レビューエージェントのジョブ
- 将来の CI 修正ジョブ（webhook 起動）など

### データモデル

```yaml
# tasks/{id}.yaml
dispatch_id: "bo-3"          # メインジョブ（状態遷移に影響）
related_jobs:                 # 関連ジョブ（状態遷移に影響しない）
  - "review-bo-1"
  - "review-bo-2"
```

### ダッシュボード表示

- `in_review` 状態で `related_jobs` に running ジョブがある場合、警告を表示
- 例: "レビューエージェントが作業中です (review-bo-1)"
- Feedback ボタンはブロックしない（ユーザーの判断に委ねる）

### ジョブ分類

| job_type | 状態遷移 | 記録先 | トリガー |
|----------|---------|--------|---------|
| `run` | あり (`executing → in_review`) | `task.dispatch_id` | Plan/Dispatch/Feedback |
| `review` | なし | `task.related_jobs` | Webhook (PR push) |

---

## オーケストレーション層の分離

### 動機

- 現行: オーケストレーションロジックが `dashboard/app.py` に埋め込まれている
- 将来: GitHub webhook ハンドラからも同じオーケストレーションロジックを呼ぶ必要がある
- `lib/orchestrator.py` に切り出し、dashboard と webhook の両方から利用可能にする

### 構造

```
scripts/lib/
├── task_store.py          # データ CRUD（既存）
├── orchestrator.py        # 状態遷移 + アクション実行（新規）
│   ├── validate_transition(task_id, action) → bool
│   ├── plan(task_id) → result
│   ├── dispatch(task_id) → result
│   ├── feedback(task_id) → result
│   ├── complete(task_id) → result
│   ├── abort(task_id) → result
│   └── on_job_completed(dispatch_id, job_type) → result
└── worktree.py            # Worktree 操作（既存）

scripts/dashboard/
└── app.py                 # HTTP エンドポイント（orchestrator の薄いラッパー）

scripts/webhook/            # 将来
└── handler.py             # GitHub webhook → orchestrator or dispatcher
```

### app.py の変更

各エンドポイントは以下のパターンに簡素化:

```python
@app.post("/api/tasks/{task_id}/dispatch")
async def api_dispatch(task_id: str) -> JSONResponse:
    result = await orchestrator.dispatch(task_id)
    return JSONResponse(result)
```

### Webhook ハンドラ（将来）

```python
# CI 失敗 → orchestrator 経由（状態遷移あり）
def on_actions_failure(payload):
    task = find_task_by_pr_url(payload["pr_url"])
    orchestrator.feedback(task.id, source="ci_failure")

# PR push → dispatcher 直接（状態遷移なし、related_jobs に追記）
def on_pr_push(payload):
    task = find_task_by_pr_url(payload["pr_url"])
    result = dispatcher_send({"command": "run", "job_type": "review", ...})
    task.related_jobs.append(result["dispatch_id"])
```

---

## 実装順序

1. **lib/orchestrator.py 作成**: app.py からロジックを抽出
2. **状態遷移の更新**: 4 状態 → 5 状態、ボタン表示条件の変更
3. **自動遷移 (executing → in_review)**: executor callback の追加
4. **related_jobs フィールド追加**: TaskData に追加、ダッシュボード表示
5. **Open アクション廃止**: エンドポイント・ボタン削除
6. **(将来) webhook ハンドラ**: GitHub webhook → orchestrator / dispatcher

ステップ 1-5 は既存機能の範囲内で完結し、webhook 未実装でも動作する。
