"""Lifecycle ステートマシン + テンプレート。"""

import json
import os
import sys
from pathlib import Path
from typing import Callable, Awaitable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sandbox_exec

from .models import Lifecycle, Job, log, now_iso, SOCKET_DIR_NAME


# ---------------------------------------------------------------------------
# 精査テンプレート
# ---------------------------------------------------------------------------

REFINE_TRIAGE_TEMPLATE = """\
あなたはタスク精査エージェントです。以下のタスクを精査し、ステータスを遷移させてください。

# 対象タスク

ファイル: `{context_path}`

```markdown
{context}
```

# プロジェクト情報

- プロジェクトID: {project_id}
- プロジェクト名: {project_name}
- 説明: {project_description}
- 作業ディレクトリ: {working_directory}

# 指示

1. タスクの内容を分析してください。必要に応じて作業ディレクトリ配下のソースコードを調査してください。

2. 以下の観点で未決事項を洗い出してください:
   - タスクの目的・スコープが明確か
   - 実装方針に曖昧さがないか
   - 事前条件・依存関係が特定されているか
   - 達成条件がAIエージェント自身でローカル検証可能か（ファイル確認、テスト実行、ビルド成功など）

3. 判定:
   - **未決事項がある場合**: `## 未決事項` セクションにチェックボックス形式で質問を記載し、`needs_input` に遷移
   - **未決事項がない場合**: `## 概要`、`## 事前条件`、`## 達成条件`、`## 完了時アクション` を記載し、`scoped` に遷移。さらに `## 実行プロンプト` セクションに実行プロンプトを生成

4. 達成条件のルール（重要）:
   - このタスクは AI エージェントが実装・実行する前提です
   - 達成条件は AI エージェント自身がローカルで検証可能な内容にしてください
   - OK: ファイル内容の確認、YAML/JSON のパース検証、テスト実行結果、ビルド成功
   - NG: ブラウザでの手動確認、外部サービスの目視確認など

5. 実行プロンプト生成時の注意（scoped の場合）:
   - 実行プロンプトは、別の AI エージェントがこのタスクを実行するための指示書です
   - 背景・目的、具体的な作業手順、達成条件を含めてください
   - 実行エージェントはタスク md の他のセクションを読まない前提で、自己完結した内容にしてください

# ファイル更新

`{context_path}` を更新してください（精査結果を反映）。

# 結果ファイル出力

以下のパスに結果 JSON を書き出してください:
  {result_file_path}

フォーマット:
  {{"next_status": "scoped"}} — 精査完了、実行可能
  {{"next_status": "needs_input"}} — ユーザへの質問あり
"""

REFINE_RECLARIFY_TEMPLATE = """\
あなたはタスク精査エージェントです。以下のタスクは質問への回答が完了しています。回答を踏まえて精査を完了させてください。

# 対象タスク

ファイル: `{context_path}`

```markdown
{context}
```

# プロジェクト情報

- プロジェクトID: {project_id}
- プロジェクト名: {project_name}
- 説明: {project_description}
- 作業ディレクトリ: {working_directory}

# 指示

1. `## 未決事項` セクションの回答内容を確認してください。必要に応じて作業ディレクトリ配下のソースコードを調査してください。

2. 回答を踏まえて以下を実施:
   - `## 概要`、`## 事前条件`、`## 達成条件`、`## 完了時アクション` を記載（既存内容があれば更新）
   - `scoped` に遷移
   - `## 実行プロンプト` セクションに実行プロンプトを生成

3. 達成条件のルール（重要）:
   - このタスクは AI エージェントが実装・実行する前提です
   - 達成条件は AI エージェント自身がローカルで検証可能な内容にしてください
   - OK: ファイル内容の確認、YAML/JSON のパース検証、テスト実行結果、ビルド成功
   - NG: ブラウザでの手動確認、外部サービスの目視確認など

4. 実行プロンプト生成の注意:
   - 実行プロンプトは、別の AI エージェントがこのタスクを実行するための指示書です
   - 背景・目的、具体的な作業手順、達成条件を含めてください
   - 実行エージェントはタスク md の他のセクションを読まない前提で、自己完結した内容にしてください

# ファイル更新

`{context_path}` を更新してください（精査結果を反映）。

# 結果ファイル出力

以下のパスに結果 JSON を書き出してください:
  {result_file_path}

フォーマット:
  {{"next_status": "scoped"}} — 精査完了、実行可能
"""

REFINE_REREFINEMENT_TEMPLATE = """\
あなたはタスク精査エージェントです。以下のタスクはジョブ実行後に reshaping に戻されました。実行履歴を踏まえて再精査してください。

# 対象タスク

ファイル: `{context_path}`

```markdown
{context}
```

# プロジェクト情報

- プロジェクトID: {project_id}
- プロジェクト名: {project_name}
- 説明: {project_description}
- 作業ディレクトリ: {working_directory}

# 実行回数: {run_count}

# 指示

1. `## 実行履歴` セクションを確認し、前回の実行結果を把握してください。必要に応じて作業ディレクトリ配下のソースコードや変更差分を調査してください。

2. 判定:
   - **再作業が必要な場合**（失敗、レビュー指摘、不具合など）:
     - `## 事前条件`、`## 達成条件` を必要に応じて修正
     - `## 実行プロンプト` を前回の結果を踏まえて修正（何が問題だったか、どう修正すべきかを明記）
     - `scoped` に遷移
   - **追加の未決事項がある場合**:
     - `## 未決事項` にチェックボックス形式で質問を記載
     - `needs_input` に遷移
   - **問題がない場合**（タスクは正常完了している）:
     - `reshaping` のまま変更しない（完了確認待ち）
     - `## 概要` の末尾に「精査結果: 正常完了を確認。完了確認待ち。」と追記

3. 達成条件のルール（重要）:
   - このタスクは AI エージェントが実装・実行する前提です
   - 達成条件は AI エージェント自身がローカルで検証可能な内容にしてください
   - OK: ファイル内容の確認、YAML/JSON のパース検証、テスト実行結果、ビルド成功
   - NG: ブラウザでの手動確認、外部サービスの目視確認など

4. 実行プロンプト修正時の注意:
   - 実行プロンプトは、別の AI エージェントがこのタスクを実行するための指示書です
   - 前回の実行で何が起きたか、今回何を修正すべきかを明確に記載してください
   - 実行エージェントはタスク md の他のセクションを読まない前提で、自己完結した内容にしてください

# ファイル更新

`{context_path}` を更新してください（精査結果を反映）。

# 結果ファイル出力

以下のパスに結果 JSON を書き出してください:
  {result_file_path}

フォーマット:
  {{"next_status": "scoped"}} — 精査完了、実行可能
  {{"next_status": "needs_input"}} — ユーザへの質問あり
  {{"next_status": "reshaping"}} — 問題なし（完了確認待ち）
"""


# ---------------------------------------------------------------------------
# 評価テンプレート
# ---------------------------------------------------------------------------

EVALUATION_TEMPLATE = """\
あなたはタスク評価エージェントです。直前の実行ジョブの結果を評価し、タスクの達成条件が満たされたか判定してください。

# 対象タスク

ファイル: `{context_path}`

```markdown
{context}
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

1. `{context_path}` の「## 実行履歴」セクションに以下を追記:
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

    # --- コンテキストファイル管理 ---

    def _context_dir(self) -> Path:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/run-{os.getuid()}")
        return Path(runtime_dir) / SOCKET_DIR_NAME

    def _init_context(self, lc: Lifecycle, content: str):
        """コンテキストファイルを作成し、lc.context_path を設定。"""
        path = self._context_dir() / f"{lc.lifecycle_id}.context.md"
        path.write_text(content, encoding="utf-8")
        lc.context_path = str(path)

    def _read_context(self, lc: Lifecycle) -> str | None:
        """コンテキストファイルを読み込む。"""
        if not lc.context_path:
            return None
        path = Path(lc.context_path)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def _read_section(self, lc: Lifecycle, section: str) -> str | None:
        """コンテキストから指定セクション（## で始まる）の内容を抽出。"""
        context = self._read_context(lc)
        if not context:
            return None
        marker = f"## {section}"
        idx = context.find(marker)
        if idx == -1:
            return None
        body = context[idx + len(marker):]
        lines = []
        for line in body.split("\n"):
            if line.startswith("## "):
                break
            lines.append(line)
        return "\n".join(lines).strip() or None

    # --- テンプレート選択 ---

    def _select_refine_template(self, lc: Lifecycle, prev_suspend_reason: str | None = None) -> str:
        if prev_suspend_reason == "needs_input":
            return REFINE_RECLARIFY_TEMPLATE
        elif lc.run_count > 0:
            return REFINE_REREFINEMENT_TEMPLATE
        else:
            return REFINE_TRIAGE_TEMPLATE

    # --- ステートマシン ---

    async def on_job_complete(self, job: Job):
        if not job.lifecycle_id:
            return
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

        if not next_status:
            log.error(f"Lifecycle: no refine result for {lc.lifecycle_id}")
            return

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

        else:
            log.error(f"Lifecycle: unexpected refine result for {lc.lifecycle_id}: {next_status}")

    async def _on_execute_complete(self, lc: Lifecycle, job: Job, result: dict | None):
        self._update_status(lc, "evaluating")
        await self.dispatch_evaluate(lc, job)

    async def _on_evaluate_complete(self, lc: Lifecycle, job: Job, result: dict | None):
        verdict = (result or {}).get("verdict")

        if not verdict:
            log.error(f"Lifecycle: no evaluate result for {lc.lifecycle_id}")
            return

        lc.run_count += 1

        if verdict == "PASS":
            self._update_status(lc, "done")
        elif verdict == "RETRY":
            if lc.run_count >= lc.max_runs:
                self._update_status(lc, "done")
            else:
                self._update_status(lc, "reshaping")
                await self.dispatch_refine(lc)
        elif verdict == "BLOCKED":
            lc.suspend_reason = "needs_input"
            self._update_status(lc, "suspend")
        elif verdict == "ABORT":
            self._update_status(lc, "done")
        else:
            log.error(f"Lifecycle: unknown verdict for {lc.lifecycle_id}: {verdict}")

    # --- ディスパッチヘルパー ---

    async def dispatch_refine(self, lc: Lifecycle, prev_suspend_reason: str | None = None):
        context = self._read_context(lc)
        if not context:
            # context なし → refine スキップ、直接 execute
            self._update_status(lc, "running")
            await self.dispatch_execute(lc)
            return

        template = self._select_refine_template(lc, prev_suspend_reason)
        project = sandbox_exec.load_project(lc.project_id, self.repo_dir)
        if not project:
            log.error(f"Lifecycle: project not found: {lc.project_id}")
            return

        dispatch_id = self._generate_dispatch_id(lc.project_id)
        result_file_path = str(self._result_path(dispatch_id))

        prompt = template.format(
            context_path=lc.context_path,
            context=context,
            project_id=lc.project_id,
            project_name=project.get("name", ""),
            project_description=project.get("description", ""),
            working_directory=project.get("working_directory", ""),
            run_count=lc.run_count,
            result_file_path=result_file_path,
        )

        actual_dispatch_id = await self._dispatch_fn(
            project_id=lc.project_id,
            prompt=prompt,
            job_type="refine",
            lifecycle_id=lc.lifecycle_id,
            dispatch_id_hint=dispatch_id,
        )
        lc.current_dispatch_id = actual_dispatch_id
        self._save()

    async def dispatch_execute(self, lc: Lifecycle):
        prompt = self._read_section(lc, "実行プロンプト")
        if not prompt:
            prompt = lc.prompt  # fallback

        dispatch_id = await self._dispatch_fn(
            project_id=lc.project_id,
            prompt=prompt,
            job_type="execute",
            lifecycle_id=lc.lifecycle_id,
        )
        lc.current_dispatch_id = dispatch_id
        self._save()

    async def dispatch_evaluate(self, lc: Lifecycle, execute_job: Job):
        log_path = self._log_path(execute_job.dispatch_id)
        execution_log = ""
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > 200:
                execution_log = f"... ({len(lines) - 200} 行省略) ...\n"
                lines = lines[-200:]
            execution_log += "\n".join(lines)

        context = self._read_context(lc) or ""
        next_run = lc.run_count + 1

        prompt = EVALUATION_TEMPLATE.format(
            context_path=lc.context_path,
            context=context,
            execution_log=execution_log,
            log_lines=min(len(execution_log.splitlines()), 200),
            next_run=next_run,
            finished_at=execute_job.finished_at or now_iso(),
            exit_code=execute_job.exit_code,
            result_file_path="(システムプロンプトで指定されるパスを使用してください)",
        )

        dispatch_id = await self._dispatch_fn(
            project_id=lc.project_id,
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
