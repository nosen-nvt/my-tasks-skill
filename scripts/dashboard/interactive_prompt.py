"""対話セッション用プロンプト構築。"""

import json
from pathlib import Path

from lib.task_store import TaskData


def resolve_completion_commands(task: TaskData, datasource: dict | None) -> list[dict]:
    """task の completion_actions からシェルコマンドを解決する。

    Returns:
        list[dict]: [{"description": ..., "command": ...}, ...]
    """
    actions = task.completion_actions
    if not actions or not datasource:
        return []

    operations = datasource.get("operations") or {}
    remote_id = task.remote_id
    site_mapping = datasource.get("site_mapping") or {}

    # project_key を remote_id から推定 (e.g. "ROOK-123" -> "ROOK")
    project_key = remote_id.rsplit("-", 1)[0] if "-" in remote_id else ""
    site = site_mapping.get(project_key, "")

    resolved: list[dict] = []
    for action in actions:
        if isinstance(action, str):
            # 文字列の場合はそのままコマンドとして扱う
            resolved.append({"description": action, "command": action})
            continue

        op_name = action.get("operation", "")
        op_def = operations.get(op_name)
        if not op_def:
            resolved.append({"description": f"(unknown operation: {op_name})", "command": ""})
            continue

        cmd = op_def.get("command", "")
        params = action.get("params") or {}

        # 共通プレースホルダを追加
        placeholders = {
            "remote_id": remote_id,
            "site": site,
            **params,
        }

        for key, value in placeholders.items():
            cmd = cmd.replace(f"{{{key}}}", str(value))

        resolved.append({
            "description": op_def.get("description", op_name),
            "command": cmd,
        })

    return resolved


def build_lifecycle_session_prompts(
    entry: dict, ctx: dict, dispatch_dir: str
) -> tuple[str, str]:
    """ライフサイクル suspend 用の (system_prompt, prompt) を構築する。"""

    suspend_reason = entry.get("suspend_reason", "")
    context_path_str = entry.get("context_path", "")

    description = ctx.get("description", "")
    phases = ctx.get("phases", [])
    current_phase = entry.get("current_phase", 0)
    acceptance_criteria = ctx.get("acceptance_criteria", [])

    system_prompt = ""
    if context_path_str:
        system_prompt = (
            "コンテキスト YAML を更新する際は以下のファイルを直接編集してください:\n"
            f"  {context_path_str}\n\n"
            "更新後は yaml.safe_load でパースできる状態を維持すること。"
        )

    if suspend_reason == "needs_input":
        open_questions = ctx.get("open_questions", [])
        oq_text = "\n".join(f"- {q}" for q in open_questions) if open_questions else "- (未決事項なし)"
        prompt = (
            "このタスクは計画段階で未決事項が発生し、ユーザーの入力を待っています。\n\n"
            f"# タスク概要\n{description}\n\n"
            f"# 未決事項\n{oq_text}\n\n"
            "# 指示\n"
            "ユーザーと対話して上記の未決事項を解決してください。\n"
            "回答が得られたら、コンテキスト YAML を更新してください:\n"
            f"  {context_path_str}\n\n"
            "更新内容:\n"
            "- open_questions: 回答済みの質問を更新・削除\n"
            "- 必要に応じて description, acceptance_criteria, preconditions も補完\n\n"
            "# 作業進行時の対応\n"
            "対話中に未決事項の解決だけでなく、作業自体が自然に進行した場合は、\n"
            "コンテキスト YAML の phases も更新してください:\n"
            "- 完了したフェーズは `status: done` にし、`notes` に成果を記録\n"
            "- 未完了のフェーズは `status: pending` のまま残す\n"
            "これにより、Resume 時に既に完了した作業がスキップされます。"
        )
    elif suspend_reason == "agent_review":
        reason_text = ""
        current_dispatch_id = entry.get("current_dispatch_id", "")
        if current_dispatch_id:
            result_path = Path(dispatch_dir) / f"{current_dispatch_id}.result.json"
            if result_path.exists():
                try:
                    with open(result_path, encoding="utf-8") as f:
                        result_data = json.load(f)
                    reason_text = result_data.get("reason", "")
                except (json.JSONDecodeError, OSError):
                    pass

        phase_info = ""
        if phases:
            total = len(phases)
            goal = phases[current_phase].get("goal", "") if current_phase < total else ""
            phase_info = f"Phase {current_phase + 1}/{total}: {goal}"

        prompt = (
            "このタスクは評価段階でレビューが必要と判定されました。\n\n"
            f"# タスク概要\n{description}\n\n"
            f"# 評価結果\n{reason_text}\n\n"
            + (f"# 現在のフェーズ\n{phase_info}\n\n" if phase_info else "")
            + "# 指示\n"
            "ユーザーと対話して評価結果の問題を確認し、対応方針を決定してください。\n"
            "必要に応じてコンテキスト YAML を更新してください:\n"
            f"  {context_path_str}"
        )
    elif suspend_reason == "approval_required":
        phases_text = "\n".join(
            f"{i + 1}. {p.get('goal', '')}" for i, p in enumerate(phases)
        ) if phases else "(フェーズ計画なし)"
        ac_text = "\n".join(
            f"- {c}" for c in acceptance_criteria
        ) if acceptance_criteria else "(達成条件なし)"

        prompt = (
            "このタスクの実行計画が作成されました。ユーザーと計画をレビューしてください。\n\n"
            f"# タスク概要\n{description}\n\n"
            f"# フェーズ計画\n{phases_text}\n\n"
            f"# 達成条件\n{ac_text}\n\n"
            "# 指示\n"
            "ユーザーと対話して計画をレビューしてください。\n"
            "修正が必要な場合はコンテキスト YAML を更新してください:\n"
            f"  {context_path_str}"
        )
    else:
        prompt = (
            f"このタスクは suspend 状態です（理由: {suspend_reason}）。\n\n"
            f"# タスク概要\n{description}\n\n"
            "# 指示\n"
            "ユーザーと対話して状況を確認してください。"
        )

    return system_prompt, prompt


def build_task_session_prompts(
    task: TaskData,
    datasource: dict | None,
    repo_dir: str,
) -> tuple[str, str]:
    """タスク直接セッション用の (system_prompt, prompt) を構築する。"""

    task_id = task.id
    datasource_id = task.datasource_id
    remote_id = task.remote_id
    tasks_dir = str(Path(repo_dir) / "tasks")
    yaml_path = f"{tasks_dir}/{task_id}.yaml"
    index_path = f"{tasks_dir}/index.jsonl"

    # completion_actions のコマンドを解決
    completion_cmds = resolve_completion_commands(task, datasource)
    completion_section = ""
    if completion_cmds:
        cmd_lines = []
        for c in completion_cmds:
            if c["command"]:
                cmd_lines.append(f"  - `{c['command']}`  ({c['description']})")
            else:
                cmd_lines.append(f"  - (未解決) {c['description']}")
        completion_section = (
            "\n## Completion Actions\n"
            "タスク完了時に以下のコマンドを順次実行してください:\n"
            + "\n".join(cmd_lines)
        )
    else:
        # datasource の operations から mark_read 等のデフォルトアクションを提示
        if datasource:
            operations = datasource.get("operations") or {}
            if operations:
                op_lines = []
                for op_name, op_def in operations.items():
                    cmd = op_def.get("command", "")
                    # remote_id を置換
                    if remote_id:
                        cmd = cmd.replace("{remote_id}", remote_id)
                    op_lines.append(f"  - {op_name}: `{cmd}`  ({op_def.get('description', '')})")
                completion_section = (
                    "\n## Available Operations\n"
                    "必要に応じて以下のデータソース操作が利用できます:\n"
                    + "\n".join(op_lines)
                )

    system_prompt = f"""あなたはタスク対話セッションで実行されています。

## タスクメタデータ
- task_id: {task_id}
- datasource_id: {datasource_id}
- remote_id: {remote_id}

## ファイルパス
- タスク YAML: {yaml_path}
- タスクインデックス: {index_path}

## 状態遷移ルール

タスクの処理結果に応じて、以下のいずれかの遷移を行ってください。

### done (タスク完了)
1. completion_actions のコマンドを順次実行
2. {yaml_path} の status を `done` に更新
3. {index_path} の該当エントリ (id: {task_id}) の status を `done` に更新

### needs_input (ユーザー入力が必要)
1. {yaml_path} の open_questions にユーザーへの質問を記録
2. status は `pending` のまま変更しない

### abort (タスク中止)
1. {yaml_path} の status を `aborted` に更新
2. {index_path} の該当エントリ (id: {task_id}) の status を `aborted` に更新
{completion_section}"""

    # --- prompt ---
    title = task.title
    description = task.description

    prompt_parts = [f"# タスク: {title}"]
    if description:
        prompt_parts.append(f"\n{description}")

    # データソース種別に応じたコンテキスト
    ds_type = datasource.get("type", "") if datasource else ""
    if ds_type:
        prompt_parts.append(f"\nデータソース種別: {ds_type}")

    # 最新 Generation の状態に応じたコンテキスト
    latest_gen = task.generations[-1] if task.generations else None

    if latest_gen:
        gen_status = latest_gen.status
        gen_output = latest_gen.output

        if gen_status == "needs_input":
            oq = gen_output.get("open_questions") or task.open_questions
            if oq:
                prompt_parts.append("\n# 前回の未決事項（要回答）")
                for q in oq:
                    prompt_parts.append(f"- {q}")
                prompt_parts.append(
                    "\nこれらの未決事項をユーザに質問して解決してください。回答をタスク YAML に反映し、"
                    "open_questions を更新してください。\n\n"
                    "**重要**: このセッションの目的は未決事項の解決のみです。"
                    "実装計画の作成や実装作業には進まないでください。"
                    "計画・実行はディスパッチ時に自動的に行われます。"
                )

        elif gen_status == "review_needed":
            reason = gen_output.get("reason", "")
            summary = gen_output.get("summary", "")
            prompt_parts.append("\n# 実行結果の確認が必要")
            if summary:
                prompt_parts.append(f"サマリー: {summary}")
            if reason:
                prompt_parts.append(f"理由: {reason}")
            prompt_parts.append(
                "\n実行結果を確認し、次のアクションを決定してください。"
            )

        elif gen_status == "rejected":
            summary = gen_output.get("summary", "")
            prompt_parts.append("\n# 計画が却下されました")
            if summary:
                prompt_parts.append(f"サマリー: {summary}")
            prompt_parts.append(
                "\n却下された計画についてフィードバックを記録してください。"
                "次のディスパッチ時にフィードバックが反映されます。"
            )

        else:
            # running, done, aborted 等
            if gen_output.get("summary"):
                prompt_parts.append(f"\n# 前回の結果\n{gen_output['summary']}")

    else:
        # generations がない場合は history フォールバック
        if task.history:
            prompt_parts.append("\n# 前世代の履歴")
            for h in task.history:
                prompt_parts.append(f"- Generation {h.generation}: {h.summary}")

    # open_questions（Generation にない場合の直接表示）
    if not latest_gen or latest_gen.status not in ("needs_input",):
        if task.open_questions:
            prompt_parts.append("\n# 未決事項")
            for q in task.open_questions:
                prompt_parts.append(f"- {q}")

    prompt = "\n".join(prompt_parts)

    return system_prompt, prompt
