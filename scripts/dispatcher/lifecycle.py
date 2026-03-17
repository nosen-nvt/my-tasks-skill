"""Lifecycle ステートマシン + テンプレート（フェーズベース）。"""

import json
import os
import sys
from pathlib import Path
from typing import Callable, Awaitable

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sandbox_exec

from .models import Lifecycle, Job, log, now_iso, SOCKET_DIR_NAME


# ---------------------------------------------------------------------------
# 計画テンプレート
# ---------------------------------------------------------------------------

PLAN_TEMPLATE = """\
あなたはタスク計画エージェントです。以下のタスクを分析し、実行フェーズを計画してください。

# タスク情報

```yaml
{context_yaml}
```

# プロジェクト情報

- プロジェクトID: {project_id}
- プロジェクト名: {project_name}
- 説明: {project_description}
- 作業ディレクトリ: {working_directory}

# フィードバック

`feedback` フィールドにレビュアーやユーザーからのフィードバックが含まれている場合:
- 各フィードバック項目の内容を分析し、修正方針に反映してください
- フィードバックの指摘事項をフェーズ計画の達成条件に組み込んでください
- previous_generations のサマリーとフィードバックを照合し、前世代で何が不足していたかを特定してください
- フィードバックがない場合はこのセクションを無視してください

# 指示

1. タスクの内容を分析してください。必要に応じて作業ディレクトリ配下のソースコードを調査してください。

2. 以下の観点で未決事項を洗い出してください:
   - タスクの目的・スコープが明確か
   - 実装方針に曖昧さがないか
   - 事前条件・依存関係が特定されているか
   - 達成条件がAIエージェント自身でローカル検証可能か（ファイル確認、テスト実行、ビルド成功など）

3. 判定:
   - **未決事項がある場合**: `needs_input` に遷移。コンテキスト YAML の `open_questions` にチェックボックス形式で質問を記載
   - **未決事項がない場合**: `planned` に遷移。フェーズ計画を作成

4. フェーズ計画（planned の場合）:
   - タスクを 1〜5 個のフェーズに分割してください
   - 各フェーズには goal（目標）と criteria（完了判定基準）を設定
   - フェーズは順番に実行されます。前のフェーズの成果物を次のフェーズが利用できます
   - 最初のフェーズの実行プロンプト（execute_prompt）を生成してください

5. 達成条件のルール（重要）:
   - このタスクは AI エージェントが実装・実行する前提です
   - 達成条件は AI エージェント自身がローカルで検証可能な内容にしてください
   - OK: ファイル内容の確認、YAML/JSON のパース検証、テスト実行結果、ビルド成功
   - NG: ブラウザでの手動確認、外部サービスの目視確認など

6. 実行プロンプト生成時の注意:
   - 実行プロンプトは、別の AI エージェントがこのタスクを実行するための指示書です
   - 背景・目的、具体的な作業手順、達成条件を含めてください
   - 実行エージェントはコンテキスト YAML を読まない前提で、自己完結した内容にしてください

# コンテキスト YAML 更新

`{context_path}` を更新してください。以下のフィールドを設定:
- `description`: タスクの概要（精査・補完した内容）
- `preconditions`: 事前条件リスト
- `acceptance_criteria`: 達成条件リスト
- `completion_actions`: 完了時アクションリスト
- `open_questions`: 未決事項（planned なら空リスト）
- `phases`: フェーズ計画リスト（以下の形式で記述）:
  ```yaml
  phases:
    - goal: "フェーズの目標"
      criteria: "完了判定基準"
    - goal: "次のフェーズの目標"
      criteria: "完了判定基準"
  ```
  **重要**: フィールド名は必ず `goal` と `criteria` を使用してください（`name` や `notes` は不可）
- `execute_prompt`: 最初のフェーズの実行プロンプト

# 結果ファイル出力

以下のパスに結果 JSON を書き出してください:
  {result_file_path}

フォーマット:
  {{"next_status": "planned", "phases": [{{"goal": "...", "criteria": "..."}}]}} — 計画完了、実行可能
  {{"next_status": "needs_input"}} — ユーザへの質問あり
"""

PLAN_RECLARIFY_TEMPLATE = """\
あなたはタスク計画エージェントです。以下のタスクは質問への回答が完了しています。回答を踏まえて計画を完了させてください。

# タスク情報

```yaml
{context_yaml}
```

# プロジェクト情報

- プロジェクトID: {project_id}
- プロジェクト名: {project_name}
- 説明: {project_description}
- 作業ディレクトリ: {working_directory}

# フィードバック

`feedback` フィールドにレビュアーやユーザーからのフィードバックが含まれている場合:
- 各フィードバック項目の内容を分析し、修正方針に反映してください
- フィードバックの指摘事項をフェーズ計画の達成条件に組み込んでください
- previous_generations のサマリーとフィードバックを照合し、前世代で何が不足していたかを特定してください
- フィードバックがない場合はこのセクションを無視してください

# 指示

1. `open_questions` の回答内容を確認してください。必要に応じて作業ディレクトリ配下のソースコードを調査してください。

2. 回答を踏まえてフェーズ計画を作成してください:
   - タスクを 1〜5 個のフェーズに分割
   - 各フェーズに goal と criteria を設定
   - 最初のフェーズの execute_prompt を生成
   - `planned` に遷移

3. 達成条件のルール（重要）:
   - このタスクは AI エージェントが実装・実行する前提です
   - 達成条件は AI エージェント自身がローカルで検証可能な内容にしてください
   - OK: ファイル内容の確認、YAML/JSON のパース検証、テスト実行結果、ビルド成功
   - NG: ブラウザでの手動確認、外部サービスの目視確認など

4. 実行プロンプト生成の注意:
   - 実行プロンプトは、別の AI エージェントがこのタスクを実行するための指示書です
   - 背景・目的、具体的な作業手順、達成条件を含めてください
   - 実行エージェントはコンテキスト YAML を読まない前提で、自己完結した内容にしてください

# コンテキスト YAML 更新

`{context_path}` を更新してください（精査結果を反映）。
phases のフィールド名は必ず `goal` と `criteria` を使用してください:
```yaml
phases:
  - goal: "フェーズの目標"
    criteria: "完了判定基準"
```

# 結果ファイル出力

以下のパスに結果 JSON を書き出してください:
  {result_file_path}

フォーマット:
  {{"next_status": "planned", "phases": [{{"goal": "...", "criteria": "..."}}]}} — 計画完了、実行可能
"""


# ---------------------------------------------------------------------------
# 対話セッション進捗反映テンプレート
# ---------------------------------------------------------------------------

PLAN_RESUME_PROGRESS_TEMPLATE = """\
あなたはタスク計画エージェントです。対話セッション中に作業が一部進行したタスクの残作業を計画してください。

# タスク情報

```yaml
{context_yaml}
```

# 完了済みフェーズ

対話セッション中に以下のフェーズが完了しています:
{completed_phases}

# 残フェーズ

以下のフェーズが未完了です:
{remaining_phases}

# プロジェクト情報

- プロジェクトID: {project_id}
- プロジェクト名: {project_name}
- 説明: {project_description}
- 作業ディレクトリ: {working_directory}

# 指示

1. 完了済みフェーズの成果を確認してください。必要に応じて作業ディレクトリ配下の成果物を調査してください。

2. 残フェーズを見直し、必要に応じて調整してください:
   - 完了済みフェーズの成果を踏まえ、不要になったフェーズは削除
   - 必要な追加フェーズがあれば追加
   - 各フェーズに goal と criteria を設定

3. 次に実行すべきフェーズの execute_prompt を生成してください:
   - 完了済みフェーズの成果を前提情報として含める
   - 背景・目的、具体的な作業手順、達成条件を含める
   - 実行エージェントはコンテキスト YAML を読まない前提で、自己完結した内容にする

4. 達成条件のルール（重要）:
   - このタスクは AI エージェントが実装・実行する前提です
   - 達成条件は AI エージェント自身がローカルで検証可能な内容にしてください
   - OK: ファイル内容の確認、YAML/JSON のパース検証、テスト実行結果、ビルド成功
   - NG: ブラウザでの手動確認、外部サービスの目視確認など

# コンテキスト YAML 更新

`{context_path}` を更新してください。
- 完了済みフェーズはそのまま保持してください（status: done を変更しない）
- 残フェーズの goal/criteria を必要に応じて更新してください
- execute_prompt を次フェーズ用に設定してください

phases のフィールド名は必ず `goal` と `criteria` を使用してください:
```yaml
phases:
  - goal: "完了済みフェーズの目標"
    status: done
    criteria: "完了判定基準"
    summary: "成果サマリー"
  - goal: "残フェーズの目標"
    status: pending
    criteria: "完了判定基準"
```

# 結果ファイル出力

以下のパスに結果 JSON を書き出してください:
  {result_file_path}

フォーマット:
  {{"next_status": "planned", "phases": [{{"goal": "...", "criteria": "...", "status": "done|pending"}}]}} — 計画完了、実行可能
"""


# ---------------------------------------------------------------------------
# 評価テンプレート
# ---------------------------------------------------------------------------

EVALUATE_TEMPLATE = """\
あなたはフェーズ評価エージェントです。直前の実行ジョブの結果を評価し、次のアクションを決定してください。

# 現フェーズ情報

- フェーズ {phase_number}/{total_phases}: {phase_goal}
- 完了判定基準: {phase_criteria}

# 達成条件（タスク全体）

{acceptance_criteria}

# 完了済みフェーズ

{completed_phases}

# 残フェーズ

{remaining_phases}

# 実行ログ（直前のジョブ、末尾 {log_lines} 行）

```
{execution_log}
```

# 実行情報

- 終了コード: {exit_code}
- 終了日時: {finished_at}

# 判定基準

以下の verdict のいずれかを選択してください:

- **DONE**: タスク全体の達成条件がすべて満たされている（残フェーズが不要になった場合も含む）
- **NEXT_PHASE**: 現フェーズの criteria は満たされた。次のフェーズに進む。残フェーズの見直しと次フェーズの実行プロンプト生成が必要
- **SUSPEND**: 達成条件が未達で、ユーザからの追加情報・判断が必要。何が必要かを明確に記載すること
- **ABORT**: 根本的に実行不可能（前提条件の誤り、権限不足で回復不能など）

# 出力指示

必要に応じて作業ディレクトリ配下のソースコードや変更差分を調査してください。

DONE の場合:
- 実行ログに Pull Request の作成が含まれる場合、PR の URL を `pr_url` フィールドに含めてください

NEXT_PHASE の場合:
- 残フェーズを見直し、必要に応じて修正・追加・削除してください
- 次フェーズの実行プロンプトを生成してください（前フェーズの成果を踏まえた自己完結した内容）
- 実行プロンプトは、別の AI エージェントがこのフェーズを実行するための指示書です

# 結果ファイル出力

以下のパスに結果 JSON を書き出してください:
  {result_file_path}

フォーマット:
  {{"verdict": "DONE", "phase_summary": "...", "pr_url": "https://..."}} — タスク完了（pr_url は PR 作成時のみ）
  {{"verdict": "NEXT_PHASE", "phase_summary": "...", "remaining_phases": [{{"goal": "...", "criteria": "..."}}], "next_execute_prompt": "..."}} — 次フェーズへ
  {{"verdict": "SUSPEND", "phase_summary": "...", "reason": "..."}} — ユーザ入力が必要
  {{"verdict": "ABORT", "phase_summary": "...", "reason": "..."}} — 実行不可能
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
                # 後方互換: phases/current_phase がない場合のフォールバック
                data.setdefault("phases", [])
                data.setdefault("current_phase", 0)
                lc = Lifecycle(**data)
                self.lifecycles[lc.lifecycle_id] = lc
                if lc.lifecycle_id.startswith("lc-"):
                    try:
                        seq = int(lc.lifecycle_id.split("-")[1])
                        self._lc_counter = max(self._lc_counter, seq)
                    except (ValueError, IndexError):
                        pass

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

    # --- コンテキストファイル管理 (YAML) ---

    def _context_dir(self) -> Path:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/run-{os.getuid()}")
        return Path(runtime_dir) / SOCKET_DIR_NAME

    def _init_context_yaml(self, lc: Lifecycle, context_data: dict):
        """YAML コンテキストファイルを作成し、lc.context_path を設定。"""
        path = self._context_dir() / f"{lc.lifecycle_id}.context.yaml"
        path.write_text(
            yaml.dump(context_data, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        lc.context_path = str(path)

    def _read_context_yaml(self, lc: Lifecycle) -> dict | None:
        """YAML コンテキストファイルを読み込む。"""
        if not lc.context_path:
            return None
        path = Path(lc.context_path)
        if not path.exists():
            return None
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            return None

    def _save_task_yaml_field(self, lc: Lifecycle, field: str, value):
        """タスク YAML の指定フィールドを更新する。"""
        context_data = self._read_context_yaml(lc)
        if not context_data:
            return
        task_id = context_data.get("meta", {}).get("task_id", "")
        if not task_id:
            return
        yaml_path = self.repo_dir / "tasks" / f"{task_id}.yaml"
        if not yaml_path.exists():
            return
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            data[field] = value
            yaml_path.write_text(
                yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
                encoding="utf-8",
            )
        except (yaml.YAMLError, OSError) as e:
            log.error(f"Lifecycle: failed to update task YAML field {field}: {e}")

    def _update_context_yaml(self, lc: Lifecycle, data: dict):
        """YAML コンテキストファイルを更新。"""
        if not lc.context_path:
            return
        path = Path(lc.context_path)
        path.write_text(
            yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )

    def _read_context_yaml_raw(self, lc: Lifecycle) -> str | None:
        """YAML コンテキストファイルを生テキストとして読み込む。"""
        if not lc.context_path:
            return None
        path = Path(lc.context_path)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def _normalize_context_phases(self, lc: Lifecycle):
        """context YAML のフェーズフィールド名を正規化し、YAML を上書きする。

        plan エージェントが name/notes 等を使った場合に goal/criteria へ変換する。
        resume 前に呼ぶことで、リプランや実行時のフィールド参照エラーを防ぐ。
        """
        context_data = self._read_context_yaml(lc)
        if not context_data:
            return
        phases = context_data.get("phases", [])
        if not phases:
            return
        changed = False
        for p in phases:
            if "name" in p and "goal" not in p:
                p["goal"] = p.pop("name")
                changed = True
            if "notes" in p and "criteria" not in p:
                p["criteria"] = p.pop("notes")
                changed = True
        if changed:
            self._update_context_yaml(lc, context_data)

    def _sync_phases_from_context(self, lc: Lifecycle):
        """context YAML のフェーズを lc.phases に同期する。

        plan エージェントが goal/name 等の異なるフィールド名を使う場合を正規化する。
        """
        context_data = self._read_context_yaml(lc)
        if not context_data:
            return
        phases_raw = context_data.get("phases", [])
        if not phases_raw:
            return
        lc.phases = [
            {
                "goal": p.get("goal") or p.get("name", ""),
                "criteria": p.get("criteria", ""),
                "status": p.get("status", "pending"),
                "summary": p.get("summary") or p.get("notes"),
                "commits": [],
                "dispatch_id": None,
            }
            for p in phases_raw
        ]
        lc.current_phase = 0

    # --- プロンプト構築 ---

    def _build_plan_prompt(
        self, lc: Lifecycle, context_data: dict, project: dict,
        prev_suspend_reason: str | None = None,
    ) -> tuple[str, str]:
        dispatch_id = self._generate_dispatch_id(lc.project_id)
        result_file_path = str(self._result_path(dispatch_id))

        # 計画に必要なフィールドのみ切り出し
        plan_context = {
            "description": context_data.get("description", ""),
            "preconditions": context_data.get("preconditions", []),
            "acceptance_criteria": context_data.get("acceptance_criteria", []),
            "open_questions": context_data.get("open_questions", []),
            "previous_generations": context_data.get("previous_generations", []),
            "feedback": context_data.get("feedback", []),
        }
        context_yaml = yaml.dump(plan_context, allow_unicode=True, default_flow_style=False, sort_keys=False)

        if prev_suspend_reason == "needs_input_with_progress":
            # 対話セッション中に作業が進行した場合
            phases = context_data.get("phases", [])
            completed = []
            remaining = []
            for i, p in enumerate(phases):
                goal = p.get("goal") or p.get("name", "")
                summary = p.get("summary") or p.get("notes", "")
                criteria = p.get("criteria", "")
                if p.get("status") == "done":
                    completed.append(f"- Phase {i+1}: {goal} — {summary}")
                else:
                    remaining.append(f"- Phase {i+1}: {goal} (criteria: {criteria})")
            completed_text = "\n".join(completed) if completed else "(なし)"
            remaining_text = "\n".join(remaining) if remaining else "(なし)"

            return PLAN_RESUME_PROGRESS_TEMPLATE.format(
                context_yaml=context_yaml,
                completed_phases=completed_text,
                remaining_phases=remaining_text,
                context_path=lc.context_path,
                project_id=lc.project_id,
                project_name=project.get("name", ""),
                project_description=project.get("description", ""),
                working_directory=project.get("working_directory", ""),
                result_file_path=result_file_path,
            ), dispatch_id

        template = PLAN_RECLARIFY_TEMPLATE if prev_suspend_reason == "needs_input" else PLAN_TEMPLATE

        return template.format(
            context_yaml=context_yaml,
            context_path=lc.context_path,
            project_id=lc.project_id,
            project_name=project.get("name", ""),
            project_description=project.get("description", ""),
            working_directory=project.get("working_directory", ""),
            result_file_path=result_file_path,
        ), dispatch_id

    def _build_execute_prompt(self, lc: Lifecycle, context_data: dict) -> str:
        execute_prompt = context_data.get("execute_prompt", "")
        if not execute_prompt:
            execute_prompt = lc.prompt  # fallback

        phases = context_data.get("phases", [])
        current_idx = lc.current_phase

        # 完了フェーズのサマリー一覧
        completed_summaries = []
        for i, p in enumerate(phases):
            if i < current_idx and p.get("status") == "done":
                completed_summaries.append(f"- Phase {i+1} ({p.get('goal', '')}): {p.get('summary', '完了')}")

        current_goal = phases[current_idx]["goal"] if current_idx < len(phases) else ""
        current_criteria = phases[current_idx]["criteria"] if current_idx < len(phases) else ""

        prompt_parts = [execute_prompt]
        if completed_summaries:
            prompt_parts.append("\n\n# 完了済みフェーズ\n" + "\n".join(completed_summaries))
        if current_goal:
            prompt_parts.append(f"\n\n# 現フェーズ\n- 目標: {current_goal}\n- 完了判定基準: {current_criteria}")

        return "\n".join(prompt_parts)

    def _build_evaluate_prompt(
        self, lc: Lifecycle, context_data: dict, execute_job: Job, log_tail: str,
    ) -> tuple[str, str]:
        phases = context_data.get("phases", [])
        current_idx = lc.current_phase
        total = len(phases)

        current_phase = phases[current_idx] if current_idx < total else {}
        phase_goal = current_phase.get("goal", "")
        phase_criteria = current_phase.get("criteria", "")

        # 達成条件
        acceptance_criteria = context_data.get("acceptance_criteria", [])
        ac_text = "\n".join(f"- {c}" for c in acceptance_criteria) if acceptance_criteria else "(未設定)"

        # 完了済みフェーズ
        completed = []
        for i, p in enumerate(phases):
            if i < current_idx and p.get("status") == "done":
                completed.append(f"- Phase {i+1}: {p.get('goal', '')} — {p.get('summary', '完了')}")
        completed_text = "\n".join(completed) if completed else "(なし)"

        # 残フェーズ
        remaining = []
        for i, p in enumerate(phases):
            if i > current_idx:
                remaining.append(f"- Phase {i+1}: {p.get('goal', '')} (criteria: {p.get('criteria', '')})")
        remaining_text = "\n".join(remaining) if remaining else "(なし — 現フェーズが最後)"

        dispatch_id = self._generate_dispatch_id(lc.project_id)
        result_file_path = str(self._result_path(dispatch_id))

        return EVALUATE_TEMPLATE.format(
            phase_number=current_idx + 1,
            total_phases=total,
            phase_goal=phase_goal,
            phase_criteria=phase_criteria,
            acceptance_criteria=ac_text,
            completed_phases=completed_text,
            remaining_phases=remaining_text,
            execution_log=log_tail,
            log_lines=min(len(log_tail.splitlines()), 200),
            exit_code=execute_job.exit_code,
            finished_at=execute_job.finished_at or now_iso(),
            result_file_path=result_file_path,
        ), dispatch_id

    # --- ステートマシン ---

    async def on_job_complete(self, job: Job):
        if not job.lifecycle_id:
            return
        lc = self.lifecycles.get(job.lifecycle_id)
        if not lc:
            return

        result = self._read_result(job.dispatch_id)

        if job.job_type == "plan":
            await self._on_plan_complete(lc, job, result)
        elif job.job_type == "execute":
            await self._on_execute_complete(lc, job, result)
        elif job.job_type == "evaluate":
            await self._on_evaluate_complete(lc, job, result)

    async def _on_plan_complete(self, lc: Lifecycle, job: Job, result: dict | None):
        next_status = (result or {}).get("next_status")

        if not next_status:
            log.error(f"Lifecycle: no plan result for {lc.lifecycle_id}")
            return

        if next_status == "planned":
            # フェーズ計画を保存
            phases_raw = (result or {}).get("phases", [])
            lc.phases = [
                {
                    "goal": p.get("goal", ""),
                    "criteria": p.get("criteria", ""),
                    "status": p.get("status", "pending"),
                    "summary": p.get("summary") or None,
                    "commits": [],
                    "dispatch_id": None,
                }
                for p in phases_raw
            ]
            # 完了済みフェーズをスキップして最初の pending フェーズから開始
            first_pending = next(
                (i for i, p in enumerate(lc.phases) if p["status"] != "done"),
                len(lc.phases),
            )
            lc.current_phase = first_pending
            self._update_status(lc, "planned")

            # auto_approve チェック
            project = sandbox_exec.load_project(lc.project_id, self.repo_dir)
            orchestration = (project or {}).get("orchestration", {})
            if self.should_auto_approve(orchestration, lc.run_count):
                self._update_status(lc, "phase_executing")
                await self.dispatch_execute(lc)
            else:
                lc.suspend_reason = "approval_required"
                self._update_status(lc, "suspend")

        elif next_status == "needs_input":
            # context YAML からフェーズ等を同期（plan エージェントが書き込み済みの場合）
            self._sync_phases_from_context(lc)
            lc.suspend_reason = "needs_input"
            self._update_status(lc, "suspend")

        else:
            log.error(f"Lifecycle: unexpected plan result for {lc.lifecycle_id}: {next_status}")

    async def _on_execute_complete(self, lc: Lifecycle, job: Job, result: dict | None):
        # フェーズの dispatch_id を記録
        if lc.current_phase < len(lc.phases):
            lc.phases[lc.current_phase]["dispatch_id"] = job.dispatch_id
        self._update_status(lc, "phase_evaluating")
        await self.dispatch_evaluate(lc, job)

    async def _on_evaluate_complete(self, lc: Lifecycle, job: Job, result: dict | None):
        verdict = (result or {}).get("verdict")

        if not verdict:
            log.error(f"Lifecycle: no evaluate result for {lc.lifecycle_id}")
            return

        phase_summary = (result or {}).get("phase_summary", "")

        # 現フェーズを完了済みに
        if lc.current_phase < len(lc.phases):
            lc.phases[lc.current_phase]["status"] = "done"
            lc.phases[lc.current_phase]["summary"] = phase_summary

        if verdict == "DONE":
            # 残フェーズを全てスキップ
            for i in range(lc.current_phase + 1, len(lc.phases)):
                lc.phases[i]["status"] = "skipped"
            # pr_url をタスク YAML に書き戻す
            pr_url = (result or {}).get("pr_url", "")
            if pr_url:
                self._save_task_yaml_field(lc, "pr_url", pr_url)
            self._update_status(lc, "done")

        elif verdict == "NEXT_PHASE":
            lc.run_count += 1

            # 残フェーズ更新
            remaining_phases = (result or {}).get("remaining_phases", [])
            next_execute_prompt = (result or {}).get("next_execute_prompt", "")

            # 残フェーズを再構成
            new_phases = lc.phases[:lc.current_phase + 1]  # 完了済みフェーズを保持
            for p in remaining_phases:
                new_phases.append({
                    "goal": p.get("goal", ""),
                    "criteria": p.get("criteria", ""),
                    "status": "pending",
                    "summary": None,
                    "commits": [],
                    "dispatch_id": None,
                })
            lc.phases = new_phases
            lc.current_phase += 1

            # コンテキスト YAML 更新
            context_data = self._read_context_yaml(lc)
            if context_data:
                context_data["phases"] = [
                    {"goal": p["goal"], "criteria": p["criteria"], "status": p["status"], "summary": p.get("summary")}
                    for p in lc.phases
                ]
                context_data["execute_prompt"] = next_execute_prompt
                self._update_context_yaml(lc, context_data)

            # max_runs チェック
            if lc.run_count >= lc.max_runs:
                self._update_status(lc, "aborted")
            else:
                self._update_status(lc, "phase_executing")
                await self.dispatch_execute(lc)

        elif verdict == "SUSPEND":
            lc.suspend_reason = "agent_review"
            self._update_status(lc, "suspend")

        elif verdict == "ABORT":
            self._update_status(lc, "aborted")

        else:
            log.error(f"Lifecycle: unknown verdict for {lc.lifecycle_id}: {verdict}")

    # --- ディスパッチヘルパー ---

    async def dispatch_plan(self, lc: Lifecycle, prev_suspend_reason: str | None = None):
        context_data = self._read_context_yaml(lc)
        if not context_data:
            log.error(f"Lifecycle: context file missing for {lc.lifecycle_id}")
            return

        project = sandbox_exec.load_project(lc.project_id, self.repo_dir)
        if not project:
            log.error(f"Lifecycle: project not found: {lc.project_id}")
            return

        prompt, dispatch_id = self._build_plan_prompt(lc, context_data, project, prev_suspend_reason)

        actual_dispatch_id = await self._dispatch_fn(
            project_id=lc.project_id,
            prompt=prompt,
            job_type="plan",
            lifecycle_id=lc.lifecycle_id,
            dispatch_id_hint=dispatch_id,
            run=lc.run_count,
        )
        lc.current_dispatch_id = actual_dispatch_id
        self._save()

    async def dispatch_execute(self, lc: Lifecycle):
        context_data = self._read_context_yaml(lc)
        if not context_data:
            prompt = lc.prompt  # fallback
        else:
            prompt = self._build_execute_prompt(lc, context_data)

        # フェーズ状態を更新
        if lc.current_phase < len(lc.phases):
            lc.phases[lc.current_phase]["status"] = "running"

        dispatch_id = await self._dispatch_fn(
            project_id=lc.project_id,
            prompt=prompt,
            job_type="execute",
            lifecycle_id=lc.lifecycle_id,
            run=lc.current_phase,
        )
        lc.current_dispatch_id = dispatch_id
        if lc.current_phase < len(lc.phases):
            lc.phases[lc.current_phase]["dispatch_id"] = dispatch_id
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

        context_data = self._read_context_yaml(lc)
        if not context_data:
            log.error(f"Lifecycle: context file missing for {lc.lifecycle_id}")
            return

        prompt, dispatch_id = self._build_evaluate_prompt(lc, context_data, execute_job, execution_log)

        actual_dispatch_id = await self._dispatch_fn(
            project_id=lc.project_id,
            prompt=prompt,
            job_type="evaluate",
            lifecycle_id=lc.lifecycle_id,
            dispatch_id_hint=dispatch_id,
            run=lc.current_phase,
        )
        lc.current_dispatch_id = actual_dispatch_id
        self._save()

    @staticmethod
    def should_auto_approve(orchestration: dict, run_count: int) -> bool:
        if not orchestration.get("auto_approve", False):
            return False
        if orchestration.get("require_first_approval", True) and run_count == 0:
            return False
        return True
