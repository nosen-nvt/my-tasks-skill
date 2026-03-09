"""Lifecycle ステートマシン + 評価テンプレート。"""

import json
import os
import sys
from pathlib import Path
from typing import Callable, Awaitable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sandbox_exec

from .models import Lifecycle, Job, log, now_iso, SOCKET_DIR_NAME
from . import tasks as task_helpers


EVALUATION_TEMPLATE = """\
あなたはタスク評価エージェントです。直前の実行ジョブの結果を評価し、タスクの達成条件が満たされたか判定してください。

# 対象タスク

ファイル: `{tasks_dir}/{task_id}.md`

```markdown
{task_md}
```

# 実行ログ（直前のジョブ、末尾 {log_lines} 行）

```
{execution_log}
```

# 実行情報

- 終了コード: {exit_code}
- 終了日時: {finished_at}

# 判定基準

以下の verdict のいずれかを選択してください:

- **PASS**: 達成条件がすべて満たされている
- **RETRY**: 達成条件が未達だが、AIエージェントが次の実行で修正できる。具体的なフィードバック（何が失敗し、どう修正すべきか）を記載すること
- **BLOCKED**: 達成条件が未達で、ユーザからの追加情報・判断が必要。何が必要かを明確に質問として記載すること
- **ABORT**: 根本的に実行不可能（前提条件の誤り、権限不足で回復不能など）

# 出力指示

必要に応じて作業ディレクトリ配下のソースコードや変更差分を調査してください。

1. `{tasks_dir}/{task_id}.md` の「## 実行履歴」セクションに以下を追記:
   ```markdown
   ### Run {next_run}

   - 日時: {finished_at}
   - 結果: （成功 or 失敗）
   - 終了コード: {exit_code}
   - Verdict: （PASS / RETRY / BLOCKED / ABORT）
   - 要約: （実行結果の要約）
   ```

2. verdict が BLOCKED の場合:
   - 「## 未決事項」セクションに質問をチェックボックス形式で追記

3. `{tasks_dir}/index.jsonl` を更新:
   - `run_count` を `{next_run}` に更新
   - `status` を verdict に応じて設定:
     - PASS → `done`
     - RETRY → `reshaping`
     - BLOCKED → `needs_input`
     - ABORT → `aborted`

index.jsonl は JSONL 形式（1行1タスク）です。該当行のみ変更し、他の行は変更しないでください。

# 結果ファイル出力

以下のパスに結果 JSON を書き出してください:
  {result_file_path}

フォーマット:
  {{"verdict": "PASS", "summary": "..."}} — 達成条件すべて満たされている
  {{"verdict": "RETRY", "summary": "..."}} — 再実行で修正可能
  {{"verdict": "BLOCKED", "summary": "..."}} — ユーザ入力が必要
  {{"verdict": "ABORT", "summary": "..."}} — 実行不可能
"""


class LifecycleManager:
    """Lifecycle ステートマシンの管理。"""

    def __init__(
        self,
        repo_dir: Path,
        dispatch_fn: Callable[..., Awaitable[str]],
        generate_dispatch_id_fn: Callable[[str], str],
        result_path_fn: Callable[[str], Path],
        log_path_fn: Callable[[str], Path],
    ):
        self.repo_dir = repo_dir
        self._dispatch_fn = dispatch_fn
        self._generate_dispatch_id = generate_dispatch_id_fn
        self._result_path = result_path_fn
        self._log_path = log_path_fn
        self.lifecycles: dict[str, Lifecycle] = {}
        self._lc_counter: int = 0

    def _lifecycles_path(self) -> Path:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/run-{os.getuid()}")
        return Path(runtime_dir) / SOCKET_DIR_NAME / "lifecycles.jsonl"

    def _save(self):
        path = self._lifecycles_path()
        with open(path, "w", encoding="utf-8") as f:
            for lc in self.lifecycles.values():
                f.write(json.dumps(lc.to_dict(), ensure_ascii=False) + "\n")

    def _load(self):
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
                seq = int(lc.lifecycle_id.split("-")[1])
                self._lc_counter = max(self._lc_counter, seq)

    def _update_status(self, lc: Lifecycle, new_status: str):
        lc.status = new_status
        lc.updated_at = now_iso()
        if new_status != "suspend":
            lc.suspend_reason = None
        self._save()
        log.info(f"Lifecycle {lc.lifecycle_id}: {new_status}")

    def generate_id(self) -> str:
        self._lc_counter += 1
        return f"lc-{self._lc_counter}"

    def _read_result(self, dispatch_id: str) -> dict | None:
        path = self._result_path(dispatch_id)
        if not path.exists():
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    # --- ステートマシン ---

    async def on_job_complete(self, job: Job):
        lc = self.lifecycles.get(job.lifecycle_id)
        if not lc:
            return

        result = self._read_result(job.dispatch_id)

        if job.job_type == "refine":
            await self._on_refine_complete(lc, job, result)
        elif job.job_type == "execute":
            await self._on_execute_complete(lc, job, result)
        elif job.job_type == "evaluate":
            await self._on_evaluate_complete(lc, job, result)

    async def _on_refine_complete(self, lc: Lifecycle, job: Job, result: dict | None):
        next_status = (result or {}).get("next_status")

        if not next_status and lc.task_id:
            entry = task_helpers.load_task_entry(self.repo_dir / "tasks", lc.task_id)
            next_status = entry.get("status") if entry else None

        if next_status == "scoped":
            project = sandbox_exec.load_project(lc.project_id, self.repo_dir)
            orchestration = (project or {}).get("orchestration", {})
            if self.should_auto_approve(orchestration, lc.run_count):
                self._update_status(lc, "running")
                await self.dispatch_execute(lc)
            else:
                lc.suspend_reason = "approval_required"
                self._update_status(lc, "suspend")

        elif next_status == "needs_input":
            lc.suspend_reason = "needs_input"
            self._update_status(lc, "suspend")

        elif next_status == "reshaping":
            self._update_status(lc, "done")
            if lc.task_id:
                task_helpers.update_task_index(self.repo_dir / "tasks", lc.task_id, {"status": "done"})
                task_helpers.update_task_md_status(self.repo_dir / "tasks", lc.task_id, "done")

        else:
            log.error(f"Lifecycle: unexpected refine result for {lc.lifecycle_id}: {next_status}")

    async def _on_execute_complete(self, lc: Lifecycle, job: Job, result: dict | None):
        self._update_status(lc, "evaluating")
        await self.dispatch_evaluate(lc, job)

    async def _on_evaluate_complete(self, lc: Lifecycle, job: Job, result: dict | None):
        verdict = (result or {}).get("verdict")

        if not verdict and lc.task_id:
            entry = task_helpers.load_task_entry(self.repo_dir / "tasks", lc.task_id)
            status = entry.get("status") if entry else None
            verdict = {"done": "PASS", "reshaping": "RETRY",
                       "needs_input": "BLOCKED", "aborted": "ABORT"}.get(status)

        lc.run_count += 1

        if verdict == "PASS":
            self._update_status(lc, "done")
        elif verdict == "RETRY":
            if lc.run_count >= lc.max_runs:
                self._update_status(lc, "done")
                if lc.task_id:
                    task_helpers.update_task_index(self.repo_dir / "tasks", lc.task_id, {"status": "aborted"})
            else:
                self._update_status(lc, "reshaping")
                await self.dispatch_refine(lc)
        elif verdict == "BLOCKED":
            lc.suspend_reason = "needs_input"
            self._update_status(lc, "suspend")
        elif verdict == "ABORT":
            self._update_status(lc, "done")
            if lc.task_id:
                task_helpers.update_task_index(self.repo_dir / "tasks", lc.task_id, {"status": "aborted"})
        else:
            log.error(f"Lifecycle: unknown verdict for {lc.lifecycle_id}: {verdict}")

    # --- ディスパッチヘルパー ---

    async def dispatch_refine(self, lc: Lifecycle):
        import refine as refine_mod

        tasks_dir = self.repo_dir / "tasks"
        projects_dir = self.repo_dir / "projects"

        if lc.task_id:
            task_entry = task_helpers.load_task_entry(tasks_dir, lc.task_id)
            task_md = task_helpers.read_file(tasks_dir / f"{lc.task_id}.md")
            project = refine_mod.load_project(projects_dir, lc.project_id)
            if not all([task_entry, task_md, project]):
                log.error(f"Lifecycle: missing data for refine: {lc.lifecycle_id}")
                return

            dispatch_id = self._generate_dispatch_id(lc.project_id)
            result_file_path = str(self._result_path(dispatch_id))
            prompt = refine_mod.build_prompt(task_entry, task_md, project, tasks_dir,
                                             result_file_path=result_file_path)
        else:
            self._update_status(lc, "running")
            await self.dispatch_execute(lc)
            return

        actual_dispatch_id = await self._dispatch_fn(
            project_id=lc.project_id,
            task_id=lc.task_id,
            prompt=prompt,
            job_type="refine",
            lifecycle_id=lc.lifecycle_id,
            dispatch_id_hint=dispatch_id,
        )
        lc.current_dispatch_id = actual_dispatch_id
        self._save()

    async def dispatch_execute(self, lc: Lifecycle):
        if lc.task_id:
            prompt = task_helpers.read_execution_prompt(self.repo_dir, lc.task_id)
            if not prompt:
                log.error(f"Lifecycle: no execution prompt for {lc.lifecycle_id}")
                return
            task_helpers.update_task_index(self.repo_dir / "tasks", lc.task_id, {"status": "approved"})
            task_helpers.update_task_md_status(self.repo_dir / "tasks", lc.task_id, "approved")
        else:
            prompt = lc.prompt

        dispatch_id = await self._dispatch_fn(
            project_id=lc.project_id,
            task_id=lc.task_id,
            prompt=prompt,
            job_type="execute",
            lifecycle_id=lc.lifecycle_id,
        )
        lc.current_dispatch_id = dispatch_id
        if lc.task_id:
            task_helpers.update_task_index(self.repo_dir / "tasks", lc.task_id, {"status": "running"})
            task_helpers.update_task_md_status(self.repo_dir / "tasks", lc.task_id, "running")
        self._save()

    async def dispatch_evaluate(self, lc: Lifecycle, execute_job: Job):
        tasks_dir = self.repo_dir / "tasks"

        log_path = self._log_path(execute_job.dispatch_id)
        execution_log = ""
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > 200:
                execution_log = f"... ({len(lines) - 200} 行省略) ...\n"
                lines = lines[-200:]
            execution_log += "\n".join(lines)

        task_md = ""
        next_run = lc.run_count + 1
        if lc.task_id:
            task_md = task_helpers.read_file(tasks_dir / f"{lc.task_id}.md") or ""

        prompt = EVALUATION_TEMPLATE.format(
            tasks_dir=tasks_dir,
            task_id=lc.task_id or "(direct)",
            task_md=task_md,
            execution_log=execution_log,
            log_lines=min(len(execution_log.splitlines()), 200),
            next_run=next_run,
            finished_at=execute_job.finished_at or now_iso(),
            exit_code=execute_job.exit_code,
            result_file_path="(システムプロンプトで指定されるパスを使用してください)",
        )

        dispatch_id = await self._dispatch_fn(
            project_id=lc.project_id,
            task_id=lc.task_id,
            prompt=prompt,
            job_type="evaluate",
            lifecycle_id=lc.lifecycle_id,
        )
        lc.current_dispatch_id = dispatch_id
        self._save()

    @staticmethod
    def should_auto_approve(orchestration: dict, run_count: int) -> bool:
        if not orchestration.get("auto_approve", False):
            return False
        if orchestration.get("require_first_approval", True) and run_count == 0:
            return False
        return True
