# Lifecycle 移行計画

dispatcher のオーケストレーションを Lifecycle ステートマシンに置き換え、
ユーザ向けオペレーションを 11 → 6 に削減する。

## 設計方針

### 現状の課題

- 11 オペレーションを暗記して使いこなす必要がある
- タスクステータス（pending/reshaping/scoped/approved/running/evaluating/done 等）の遷移をユーザが手動で駆動
- orchestration は execute 完了後のみ自動化しており、初回精査→承認→実行は手動

### 目標

- ユーザ操作: `dispatch` (開始) と `resume` (中断からの続行) の2操作でタスクライフサイクルを駆動
- dispatcher 内のステートマシン (Lifecycle) が精査→承認→実行→評価→ループを自動制御
- 各ジョブは結果を JSON ファイルで出力し、dispatcher は単純なプログラムで次の状態を決定

### 統廃合されるオペレーション

| # | 現オペレーション | 新方式 |
|---|---|---|
| 3 | タスク精査 | dispatch に統合 |
| 4 | プロンプト再生成 | 廃止（精査ジョブが常に生成） |
| 5 | プロンプト承認 | dispatch 内で自動承認 or suspend |
| 6 | タスク実行 | dispatch に統合 |
| 9 | 完了確認・完了時アクション | 評価ジョブに統合 |
| 11 | 精査対象選択 | dispatch に統合 |

### 残存オペレーション (6つ)

1. タスク収集
2. メールトリアージ
3. dispatch (新) — ライフサイクル開始
4. resume (新) — suspend からの続行
5. ステータス確認 (ライフサイクル表示に変更)
6. タスク操作 (データソース側)
7. 設定管理

※ 「完了時アクション」は評価ジョブの PASS 判定時に自動実行する形で統合

---

## データモデル

### Lifecycle

```python
@dataclass
class Lifecycle:
    lifecycle_id: str              # "lc-{seq}" (グローバル連番)
    task_id: str | None            # tasks/index.jsonl のタスク ID（直接投入時は None）
    project_id: str                # プロジェクト ID
    prompt: str                    # 元の依頼内容（タスク起点ならタスクタイトル+概要）
    status: str                    # reshaping | running | evaluating | suspend | done
    suspend_reason: str | None     # suspend 時の理由 ("needs_input", "approval_required")
    run_count: int                 # 実行回数
    max_runs: int                  # 最大実行回数
    current_dispatch_id: str | None  # 現在のサブジョブの dispatch_id
    created_at: str
    updated_at: str
```

### Job (既存) への追加フィールド

```python
@dataclass
class Job:
    ...
    lifecycle_id: str | None = None  # 所属する Lifecycle（None = open 等の単発ジョブ）
```

### 結果ファイル

ジョブ完了時にジョブが書き出す結果ファイル。
パス: `$XDG_RUNTIME_DIR/my-tasks-dispatch/{dispatch_id}.result.json`

各ジョブタイプの出力フォーマット:

```json
// 精査ジョブ (refine)
{"next_status": "scoped"}
{"next_status": "needs_input"}
{"next_status": "reshaping"}  // run_count > 0 で問題なしの場合（完了確認待ち）

// 実行ジョブ (execute)
// → 結果ファイル不要。exit code で判定（0=成功、非0=失敗）

// 評価ジョブ (evaluate)
{"verdict": "PASS", "summary": "全達成条件を確認済み"}
{"verdict": "RETRY", "summary": "テストが2件失敗"}
{"verdict": "BLOCKED", "summary": "API キーが必要"}
{"verdict": "ABORT", "summary": "前提条件が誤り"}
```

### ステートマシン

```
dispatch (task_id or prompt)
  │
  ├── project_id 未確定 → LLM 判定 → 確定
  │
  ▼
reshaping ─── 精査ジョブ dispatch
  │
  ├── result: "scoped"
  │     │
  │     ├── auto_approve 条件を満たす → running
  │     │                                │
  │     │                          実行ジョブ dispatch
  │     │                                │
  │     │                                ▼
  │     │                           evaluating
  │     │                                │
  │     │                          評価ジョブ dispatch
  │     │                                │
  │     │                ┌──────────┬─────┴────┬──────────┐
  │     │                ▼          ▼          ▼          ▼
  │     │              PASS      RETRY     BLOCKED     ABORT
  │     │               │          │          │          │
  │     │               ▼          ▼          ▼          ▼
  │     │             done     reshaping   suspend     done
  │     │                      (ループ)   (needs_    (aborted
  │     │                                  input)     flag)
  │     │
  │     └── auto_approve 条件を満たさない → suspend (approval_required)
  │                                           │
  │                                       resume (承認)
  │                                           │
  │                                           ▼
  │                                        running (実行ジョブ dispatch)
  │
  ├── result: "needs_input" → suspend (needs_input)
  │                              │
  │                          resume (ユーザ回答)
  │                              │
  │                              ▼
  │                          reshaping (再精査ジョブ dispatch)
  │
  └── result: "reshaping" (run_count > 0, 問題なし)
        │
        └── done (自動完了)
```

### Lifecycle 永続化

ファイル: `$XDG_RUNTIME_DIR/my-tasks-dispatch/lifecycles.jsonl`

- 状態変更のたびに全件書き出し（件数が少ないため問題なし）
- サーバ起動時にロードし、中断中 (suspend) のライフサイクルを復元
- done のライフサイクルは一定時間後に GC

---

## Phase 1: 結果ファイル規約の導入

### 目的

ジョブが結果を JSON ファイルで出力する仕組みを導入し、
dispatcher がファイルを読んで次の状態を決定できるようにする。
Phase 2 以降で Lifecycle がこの仕組みを使う。

### 変更対象

- `scripts/dispatcher.py`
- 精査ジョブのプロンプトテンプレート (`scripts/refine.py`)
- 評価ジョブのプロンプトテンプレート (`scripts/dispatcher.py` 内 `EVALUATION_TEMPLATE`)

### 作業内容

#### 1.1 結果ファイルパスのヘルパー追加 (dispatcher.py)

```python
def _result_path(self, dispatch_id: str) -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/run-{os.getuid()}")
    return Path(runtime_dir) / SOCKET_DIR_NAME / f"{dispatch_id}.result.json"

def _read_result(self, dispatch_id: str) -> dict | None:
    path = self._result_path(dispatch_id)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
```

#### 1.2 システムプロンプトに結果ファイル出力指示を追加

`_build_system_prompt()` に追記:

```
ジョブ完了時、以下のパスに結果 JSON を書き出してください:
  {result_path}

精査ジョブの場合:
  {{"next_status": "scoped"}} or {{"next_status": "needs_input"}} or {{"next_status": "reshaping"}}

評価ジョブの場合:
  {{"verdict": "PASS"|"RETRY"|"BLOCKED"|"ABORT", "summary": "..."}}

実行ジョブの場合: 結果ファイルは不要です。
```

#### 1.3 各プロンプトテンプレートに結果ファイル出力指示を追加

`TRIAGE_TEMPLATE`, `RECLARIFY_TEMPLATE`, `REREFINEMENT_TEMPLATE` (refine.py) と
`EVALUATION_TEMPLATE` (dispatcher.py) に結果ファイル書き出し指示を追記。

テンプレートに `{result_file_path}` プレースホルダを追加し、
`build_prompt()` / `EVALUATION_TEMPLATE.format()` で実際のパスを埋め込む。

#### 1.4 既存オーケストレーションの段階的移行

`on_job_complete()` 内で:
1. まず結果ファイルを読み取る (`_read_result()`)
2. 結果ファイルがあればそこから判定
3. なければ従来通り index.jsonl を読み取る（フォールバック）

これにより既存動作を壊さずに段階的に移行できる。

#### 1.5 結果ファイルの GC

`cleanup_old_logs()` に `.result.json` ファイルの削除も追加。

### 完了条件

- 精査ジョブが `{dispatch_id}.result.json` を出力する
- 評価ジョブが `{dispatch_id}.result.json` を出力する
- dispatcher が結果ファイルから状態を読み取れる（フォールバック付き）
- 既存のオーケストレーションフローが動作する

---

## Phase 2: Lifecycle データモデルと永続化

### 目的

Lifecycle データ構造とその永続化機構を導入する。
この時点ではまだ既存フローとは独立。

### 変更対象

- `scripts/dispatcher.py`

### 作業内容

#### 2.1 Lifecycle dataclass の追加

```python
@dataclass
class Lifecycle:
    lifecycle_id: str
    task_id: str | None
    project_id: str
    prompt: str
    status: str = "reshaping"
    suspend_reason: str | None = None
    run_count: int = 0
    max_runs: int = 5
    current_dispatch_id: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return { ... }
```

#### 2.2 Job への lifecycle_id フィールド追加

```python
@dataclass
class Job:
    ...
    lifecycle_id: str | None = None
```

`to_dict()` にも追加。

#### 2.3 DispatchServer にライフサイクル管理を追加

```python
class DispatchServer:
    def __init__(self, ...):
        ...
        self.lifecycles: dict[str, Lifecycle] = {}
        self._lc_counter: int = 0

    def generate_lifecycle_id(self) -> str:
        self._lc_counter += 1
        return f"lc-{self._lc_counter}"
```

#### 2.4 永続化

```python
def _lifecycles_path(self) -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/run-{os.getuid()}")
    return Path(runtime_dir) / SOCKET_DIR_NAME / "lifecycles.jsonl"

def _save_lifecycles(self):
    path = self._lifecycles_path()
    with open(path, "w", encoding="utf-8") as f:
        for lc in self.lifecycles.values():
            f.write(json.dumps(lc.to_dict(), ensure_ascii=False) + "\n")

def _load_lifecycles(self):
    path = self._lifecycles_path()
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            lc = Lifecycle(**data)
            self.lifecycles[lc.lifecycle_id] = lc
            # カウンタ復元
            seq = int(lc.lifecycle_id.split("-")[1])
            self._lc_counter = max(self._lc_counter, seq)
```

サーバ起動時 (`run_server()`) に `_load_lifecycles()` を呼び出し。
`_update_lifecycle_status()` ヘルパー内で `_save_lifecycles()` を呼び出し。

### 完了条件

- Lifecycle dataclass が定義されている
- Job に lifecycle_id フィールドが追加されている
- ライフサイクルの JSONL 永続化が動作する
- サーバ再起動後に suspend 中ライフサイクルが復元される

---

## Phase 3: dispatch / resume コマンドとステートマシン

### 目的

新コマンド `dispatch` / `resume` を追加し、Lifecycle ステートマシンを実装する。
既存の `run` コマンドと `on_job_complete` オーケストレーションは共存させる。

### 変更対象

- `scripts/dispatcher.py` (サーバ側 + クライアント CLI)
- `scripts/refine.py` (プロンプトビルダーをライブラリ利用)

### 作業内容

#### 3.1 cmd_dispatch の実装

```python
async def cmd_dispatch(self, request: dict) -> dict:
    """ライフサイクルを開始する。"""
    task_id = request.get("task_id")
    project_id = request.get("project_id")
    prompt = request.get("prompt", "")

    # タスク起点の場合: index.jsonl から情報を読み込み
    if task_id:
        task_entry = _load_task_entry(self.repo_dir / "tasks", task_id)
        if not task_entry:
            return {"ok": False, "error": f"Task not found: {task_id}"}
        project_id = project_id or task_entry.get("project_id")
        prompt = prompt or task_entry.get("title", "")

        # pending → reshaping 自動遷移
        if task_entry.get("status") == "pending":
            _update_task_index(self.repo_dir / "tasks", task_id, {"status": "reshaping"})
            _update_task_md_status(self.repo_dir / "tasks", task_id, "reshaping")

    # プロジェクト未確定 → LLM 判定 (Phase 4 で実装、ここではエラー)
    if not project_id:
        return {"ok": False, "error": "project_id is required (LLM判定は Phase 4 で実装)"}

    # プロジェクト検証
    project = sandbox_exec.load_project(project_id, self.repo_dir)
    if not project:
        return {"ok": False, "error": f"Project not found: {project_id}"}
    if not project.get("working_directory"):
        return {"ok": False, "error": f"manual project: {project_id}"}

    # オーケストレーション設定を取得
    orchestration = project.get("orchestration", {})
    max_runs = orchestration.get("max_runs_per_generation", 5)

    # Lifecycle 作成
    lc = Lifecycle(
        lifecycle_id=self.generate_lifecycle_id(),
        task_id=task_id,
        project_id=project_id,
        prompt=prompt,
        max_runs=max_runs,
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    self.lifecycles[lc.lifecycle_id] = lc
    self._save_lifecycles()

    # 精査ジョブをディスパッチ
    await self._lc_dispatch_refine(lc)

    return {
        "ok": True,
        "lifecycle_id": lc.lifecycle_id,
        "dispatch_id": lc.current_dispatch_id,
        "message": f"Lifecycle started: {lc.lifecycle_id}",
    }
```

#### 3.2 cmd_resume の実装

```python
async def cmd_resume(self, request: dict) -> dict:
    """suspend 中のライフサイクルを再開する。"""
    lifecycle_id = request.get("lifecycle_id")
    lc = self.lifecycles.get(lifecycle_id)
    if not lc:
        return {"ok": False, "error": f"Lifecycle not found: {lifecycle_id}"}
    if lc.status != "suspend":
        return {"ok": False, "error": f"Lifecycle is not suspended (current: {lc.status})"}

    if lc.suspend_reason == "needs_input":
        # ユーザが質問に回答済みの前提で再精査
        self._update_lifecycle_status(lc, "reshaping")
        await self._lc_dispatch_refine(lc)
    elif lc.suspend_reason == "approval_required":
        # ユーザが承認 → 実行ジョブをディスパッチ
        self._update_lifecycle_status(lc, "running")
        await self._lc_dispatch_execute(lc)
    else:
        return {"ok": False, "error": f"Unknown suspend reason: {lc.suspend_reason}"}

    return {
        "ok": True,
        "lifecycle_id": lc.lifecycle_id,
        "dispatch_id": lc.current_dispatch_id,
        "message": f"Lifecycle resumed: {lc.lifecycle_id}",
    }
```

#### 3.3 Lifecycle ステートマシンの実装

```python
async def _on_lifecycle_job_complete(self, job: Job):
    """Lifecycle 所属ジョブの完了ハンドラ。"""
    lc = self.lifecycles.get(job.lifecycle_id)
    if not lc:
        return

    result = self._read_result(job.dispatch_id)

    if job.job_type == "refine":
        await self._lc_on_refine_complete(lc, job, result)
    elif job.job_type == "execute":
        await self._lc_on_execute_complete(lc, job, result)
    elif job.job_type == "evaluate":
        await self._lc_on_evaluate_complete(lc, job, result)

async def _lc_on_refine_complete(self, lc: Lifecycle, job: Job, result: dict | None):
    next_status = (result or {}).get("next_status")

    # フォールバック: index.jsonl から読み取り
    if not next_status and lc.task_id:
        entry = _load_task_entry(self.repo_dir / "tasks", lc.task_id)
        next_status = entry.get("status") if entry else None

    if next_status == "scoped":
        # 自動承認判定
        project = sandbox_exec.load_project(lc.project_id, self.repo_dir)
        orchestration = (project or {}).get("orchestration", {})
        if self._should_auto_approve(orchestration, lc.run_count):
            self._update_lifecycle_status(lc, "running")
            await self._lc_dispatch_execute(lc)
        else:
            lc.suspend_reason = "approval_required"
            self._update_lifecycle_status(lc, "suspend")

    elif next_status == "needs_input":
        lc.suspend_reason = "needs_input"
        self._update_lifecycle_status(lc, "suspend")

    elif next_status == "reshaping":
        # run_count > 0 で問題なし → 自動完了
        self._update_lifecycle_status(lc, "done")
        if lc.task_id:
            _update_task_index(self.repo_dir / "tasks", lc.task_id, {"status": "done"})
            _update_task_md_status(self.repo_dir / "tasks", lc.task_id, "done")

    else:
        log.error(f"Lifecycle: unexpected refine result for {lc.lifecycle_id}: {next_status}")

async def _lc_on_execute_complete(self, lc: Lifecycle, job: Job, result: dict | None):
    self._update_lifecycle_status(lc, "evaluating")
    await self._lc_dispatch_evaluate(lc, job)

async def _lc_on_evaluate_complete(self, lc: Lifecycle, job: Job, result: dict | None):
    verdict = (result or {}).get("verdict")

    # フォールバック
    if not verdict and lc.task_id:
        entry = _load_task_entry(self.repo_dir / "tasks", lc.task_id)
        status = entry.get("status") if entry else None
        verdict = {"done": "PASS", "reshaping": "RETRY",
                   "needs_input": "BLOCKED", "aborted": "ABORT"}.get(status)

    lc.run_count += 1

    if verdict == "PASS":
        self._update_lifecycle_status(lc, "done")
    elif verdict == "RETRY":
        if lc.run_count >= lc.max_runs:
            self._update_lifecycle_status(lc, "done")  # aborted
            if lc.task_id:
                _update_task_index(self.repo_dir / "tasks", lc.task_id, {"status": "aborted"})
        else:
            self._update_lifecycle_status(lc, "reshaping")
            await self._lc_dispatch_refine(lc)
    elif verdict == "BLOCKED":
        lc.suspend_reason = "needs_input"
        self._update_lifecycle_status(lc, "suspend")
    elif verdict == "ABORT":
        self._update_lifecycle_status(lc, "done")
        if lc.task_id:
            _update_task_index(self.repo_dir / "tasks", lc.task_id, {"status": "aborted"})
    else:
        log.error(f"Lifecycle: unknown verdict for {lc.lifecycle_id}: {verdict}")
```

#### 3.4 Lifecycle 用ディスパッチヘルパー

```python
async def _lc_dispatch_refine(self, lc: Lifecycle):
    """Lifecycle から精査ジョブをディスパッチ。"""
    import refine as refine_mod

    tasks_dir = self.repo_dir / "tasks"
    projects_dir = self.repo_dir / "projects"

    if lc.task_id:
        task_entry = _load_task_entry(tasks_dir, lc.task_id)
        task_md = _read_file(tasks_dir / f"{lc.task_id}.md")
        project = refine_mod.load_project(projects_dir, lc.project_id)
        if not all([task_entry, task_md, project]):
            log.error(f"Lifecycle: missing data for refine: {lc.lifecycle_id}")
            return
        prompt = refine_mod.build_prompt(task_entry, task_md, project, tasks_dir)
    else:
        # タスクなし直接投入: 精査不要、直接実行に回す
        self._update_lifecycle_status(lc, "running")
        await self._lc_dispatch_execute(lc)
        return

    dispatch_id = await self._dispatch_internal(
        project_id=lc.project_id,
        task_id=lc.task_id,
        prompt=prompt,
        job_type="refine",
        lifecycle_id=lc.lifecycle_id,
    )
    lc.current_dispatch_id = dispatch_id
    self._save_lifecycles()

async def _lc_dispatch_execute(self, lc: Lifecycle):
    """Lifecycle から実行ジョブをディスパッチ。"""
    if lc.task_id:
        prompt = read_execution_prompt(self.repo_dir, lc.task_id)
        if not prompt:
            log.error(f"Lifecycle: no execution prompt for {lc.lifecycle_id}")
            return
        # index.jsonl を approved → running に更新
        _update_task_index(self.repo_dir / "tasks", lc.task_id,
                          {"status": "approved"})
        _update_task_md_status(self.repo_dir / "tasks", lc.task_id, "approved")
    else:
        prompt = lc.prompt

    dispatch_id = await self._dispatch_internal(
        project_id=lc.project_id,
        task_id=lc.task_id,
        prompt=prompt,
        job_type="execute",
        lifecycle_id=lc.lifecycle_id,
    )
    lc.current_dispatch_id = dispatch_id
    if lc.task_id:
        _update_task_index(self.repo_dir / "tasks", lc.task_id,
                          {"status": "running"})
        _update_task_md_status(self.repo_dir / "tasks", lc.task_id, "running")
    self._save_lifecycles()

async def _lc_dispatch_evaluate(self, lc: Lifecycle, execute_job: Job):
    """Lifecycle から評価ジョブをディスパッチ。"""
    # 既存の _on_execute_complete と同じロジックで評価プロンプトを構築
    # (EVALUATION_TEMPLATE を使用)
    ...
```

#### 3.5 _dispatch_internal の拡張

`lifecycle_id` パラメータを受け取り、Job に設定する:

```python
async def _dispatch_internal(self, ..., lifecycle_id: str | None = None) -> str:
    ...
    job = Job(
        ...
        lifecycle_id=lifecycle_id,
    )
```

#### 3.6 on_job_complete の分岐

```python
async def on_job_complete(self, job: Job):
    if job.lifecycle_id:
        await self._on_lifecycle_job_complete(job)
    elif job.task_id:
        # 従来のオーケストレーション（後方互換、Phase 5 で削除）
        ...
```

#### 3.7 CLI コマンドの追加

```python
# dispatch
p_dispatch = subparsers.add_parser("dispatch", help="ライフサイクルを開始する")
p_dispatch.add_argument("--task", help="タスク ID")
p_dispatch.add_argument("--project", help="プロジェクト ID")
p_dispatch.add_argument("--prompt", help="プロンプト（省略時は stdin）")
p_dispatch.set_defaults(func=cmd_dispatch)

# resume
p_resume = subparsers.add_parser("resume", help="suspend 中のライフサイクルを再開する")
p_resume.add_argument("--id", required=True, help="ライフサイクル ID")
p_resume.set_defaults(func=cmd_resume)
```

#### 3.8 status コマンドの拡張

ライフサイクル情報も表示:

```python
async def cmd_status(self, _request: dict) -> dict:
    jobs = [j.to_dict() for j in self.jobs.values()]
    lifecycles = [lc.to_dict() for lc in self.lifecycles.values()]
    return {"ok": True, "jobs": jobs, "lifecycles": lifecycles}
```

### 完了条件

- `dispatcher.py dispatch --task 20260301-001` でライフサイクルが開始される
- 精査 → (scoped → auto_approve → 実行 → 評価) のフルループが自動動作する
- needs_input 時に suspend し、`dispatcher.py resume --id lc-1` で再開できる
- `dispatcher.py status` でライフサイクルの状態が表示される
- 従来の `run` コマンドも引き続き動作する（後方互換）

---

## Phase 4: LLM プロジェクト判定

### 目的

dispatch 時に project_id が未指定の場合、LLM で自動判定する。
sync-tasks.py の project_mapping による自動割り当てと同等の機能を
dispatcher 側に集約する。

### 変更対象

- `scripts/dispatcher.py`

### 作業内容

#### 4.1 プロジェクト分類プロンプト

```python
PROJECT_CLASSIFICATION_PROMPT = """\
以下のタスク/依頼内容を読んで、最も適切なプロジェクトを選択してください。

# タスク内容
{prompt}

# プロジェクト一覧
{projects_list}

# 出力
JSON で回答してください:
{{"project_id": "選択したプロジェクトID", "confidence": "high"|"low"}}

どのプロジェクトにも該当しない場合:
{{"project_id": null, "reason": "理由"}}
"""
```

#### 4.2 軽量 Claude 呼び出し

```python
async def _classify_project(self, prompt: str) -> str | None:
    """プロンプトからプロジェクトを判定する。"""
    projects_dir = self.repo_dir / "projects"
    projects = []
    for p in projects_dir.glob("*.json"):
        with open(p) as f:
            proj = json.load(f)
        projects.append(f"- {proj['project_id']}: {proj.get('name', '')} - {proj.get('description', '')}")

    classification_prompt = PROJECT_CLASSIFICATION_PROMPT.format(
        prompt=prompt,
        projects_list="\n".join(projects),
    )

    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", classification_prompt, "--output-format", "json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    # JSON をパースして project_id を取り出す
    ...
```

#### 4.3 cmd_dispatch への統合

```python
# project_id 未確定の場合
if not project_id:
    project_id = await self._classify_project(prompt)
    if not project_id:
        return {"ok": False, "error": "プロジェクトを判定できませんでした"}
```

#### 4.4 confidence: low の場合

`confidence: "low"` の場合は suspend して確認を求める:

```python
if classification.get("confidence") == "low":
    lc.suspend_reason = "project_confirmation"
    lc.status = "suspend"
    # ユーザに確認を求める
```

resume 時にプロジェクトを確定して続行。

### 完了条件

- `dispatch --prompt "UBS の API にバグがある"` でプロジェクトが自動判定される
- 判定不能時は suspend し、ユーザに確認を求める
- 低確信度でも suspend して確認

---

## Phase 5: クリーンアップとドキュメント更新

### 目的

旧オーケストレーションコードを削除し、ドキュメントを更新する。

### 変更対象

- `scripts/dispatcher.py`
- `scripts/refine.py`
- `SKILL.md`
- `references/operations.md`
- `references/schemas.md`
- `references/dispatcher-design.md`

### 作業内容

#### 5.1 旧オーケストレーションの削除

`on_job_complete` 内の非 Lifecycle パス（`job.lifecycle_id` が None でかつ
`job.task_id` がある場合の従来ロジック）を削除。

以下のメソッドを削除:
- `_on_execute_complete()`
- `_on_evaluate_complete()`
- `_on_refine_complete()`

Lifecycle 版 (`_lc_on_*`) のみを残す。

#### 5.2 refine.py の位置付け変更

refine.py は CLI ツールとしては廃止し、プロンプトビルダーライブラリとして残す:
- `build_prompt()`, `TRIAGE_TEMPLATE` 等のテンプレートは引き続き使用
- `dispatch_one()`, `async_main()`, `main()` は削除またはデプリケート
- `find_targets()`, `is_all_answered()` は dispatcher が直接呼ぶ

#### 5.3 タスクステータスの整理

index.jsonl のステータス定義を簡素化:
- `pending`: データソースから取り込まれた初期状態
- `active`: ライフサイクルが進行中（dispatcher が管理）
- `done`: 完了
- `aborted`: 中止

※ 詳細状態 (reshaping, running, evaluating, suspend) は Lifecycle が管理し、
status コマンドで表示する。index.jsonl には大まかな状態のみ保存。
suspend（ユーザ入力待ち）もタスク側から見ればライフサイクル進行中であり、
active で十分。ユーザ入力待ちかどうかは `dispatcher.py status` で
ライフサイクルの状態を確認する。

→ ただしこの変更は影響範囲が大きいため、Phase 5 では index.jsonl の
従来ステータスを維持し、Lifecycle の状態を別途表示する形でも可。
最終判断は Phase 3 完了後に行う。

#### 5.4 SKILL.md の更新

オペレーション一覧を更新:

```markdown
## オペレーション一覧

1. **タスク収集** - 全データソースからタスクを取得し、index.jsonl + Markdown を更新
2. **メールトリアージ** - メールデータソースからアクションアイテムを収集
3. **dispatch** - ライフサイクルを開始（精査→実行→評価を自動制御）
4. **resume** - suspend 中のライフサイクルを再開（ユーザ入力の反映）
5. **ステータス確認** - ライフサイクル・ジョブの状況表示
6. **タスク操作** - データソース側のタスクを操作（ステータス変更等）
7. **設定管理** - プロジェクト・データソースの CRUD、リポジトリ初期化
```

#### 5.5 operations.md の更新

旧オペレーション 3,4,5,6,9,11 のセクションを削除し、
dispatch / resume の手順を記載。

#### 5.6 schemas.md の更新

- Lifecycle スキーマを追加
- 結果ファイルスキーマを追加
- タスクステータス定義を更新
- orchestration フィールドの説明を更新

#### 5.7 dispatcher-design.md の更新

Lifecycle ステートマシンの設計を反映。

### 完了条件

- 旧オーケストレーションコードが削除されている
- 全ドキュメントが Lifecycle ベースに更新されている
- `run` コマンドは Lifecycle なしの単発実行として残存（open と同様）

---

## 実装順序と依存関係

```
Phase 1 (結果ファイル規約)
  │
  ▼
Phase 2 (Lifecycle データモデル)
  │
  ▼
Phase 3 (dispatch/resume + ステートマシン)  ← 最大の変更
  │
  ├── Phase 4 (LLM プロジェクト判定)  ← Phase 3 と並行可
  │
  ▼
Phase 5 (クリーンアップ)
```

- Phase 1 → 2 → 3 は順序依存（積み上げ式）
- Phase 4 は Phase 3 完了後に独立して実施可能
- Phase 5 は全 Phase 完了後

## リスクと注意事項

1. **後方互換性**: Phase 3 完了まで既存の `run` コマンドとオーケストレーションは維持する。
   ユーザは段階的に `dispatch` に移行できる。

2. **結果ファイルの信頼性**: Claude が指示通りに結果ファイルを出力しない可能性がある。
   フォールバック（index.jsonl 読み取り）を常に残す。

3. **永続化の整合性**: dispatcher 再起動時に Lifecycle のサブジョブが実行中だった場合、
   そのジョブの完了通知を受け取れない。サーバ起動時に active な Lifecycle の
   current_dispatch_id を確認し、対応するプロセスが存在しなければ
   異常終了として扱う回復ロジックが必要。

4. **LLM プロジェクト判定のコスト**: 毎回 Claude を呼び出すため、
   レイテンシとコストが追加される。キャッシュやヒューリスティック
   （タスク起点なら既に project_id がある等）で最小化する。

5. **完了時アクションの扱い**: 現在は操作9でユーザが手動実行している。
   PASS 判定時に自動実行するには、タスク md の `## 完了時アクション` を
   パースして実行する仕組みが必要。Phase 3 では「done にする」のみとし、
   完了時アクションの自動化は追加タスクとする。
