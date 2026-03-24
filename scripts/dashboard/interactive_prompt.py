"""対話セッション用プロンプト構築 (v2)。"""

from pathlib import Path

from lib.task_store import TaskData


def resolve_completion_commands(task: TaskData, datasource: dict | None) -> list[dict]:
    """task の completion_actions からシェルコマンドを解決する。"""
    actions = task.completion_actions
    if not actions or not datasource:
        return []

    operations = datasource.get("operations") or {}
    remote_id = task.remote_id
    site_mapping = datasource.get("site_mapping") or {}

    project_key = remote_id.rsplit("-", 1)[0] if "-" in remote_id else ""
    site = site_mapping.get(project_key, "")

    resolved: list[dict] = []
    for action in actions:
        if isinstance(action, str):
            resolved.append({"description": action, "command": action})
            continue

        op_name = action.get("operation", "")
        op_def = operations.get(op_name)
        if not op_def:
            resolved.append({"description": f"(unknown operation: {op_name})", "command": ""})
            continue

        cmd = op_def.get("command", "")
        params = action.get("params") or {}
        placeholders = {"remote_id": remote_id, "site": site, **params}
        for key, value in placeholders.items():
            cmd = cmd.replace(f"{{{key}}}", str(value))

        resolved.append({
            "description": op_def.get("description", op_name),
            "command": cmd,
        })

    return resolved


def build_plan_session_prompts(
    task: TaskData,
    datasource: dict | None,
    repo_dir: str,
) -> tuple[str, str]:
    """Plan 対話セッション用の (system_prompt, prompt) を構築する。"""

    task_id = task.id
    yaml_path = f"{Path(repo_dir) / 'tasks' / f'{task_id}.yaml'}"

    system_prompt = f"""あなたはタスク計画セッションで実行されています。

## タスク YAML パス
{yaml_path}

## 指示
ユーザーと対話しながらタスクを精査・計画してください。
計画が完了したら、タスク YAML の以下のフィールドを更新してください:
- description: タスクの説明（精査・補完した内容）
- preconditions: 事前条件リスト
- acceptance_criteria: 達成条件リスト（AIエージェントがローカルで検証可能な内容）
- execute_prompt: 実行プロンプト（別のAIエージェントがこのタスクを実行するための自己完結した指示書）

## 達成条件のルール
- タスクは AI エージェントが実装・実行する前提
- 達成条件は AI エージェント自身がローカルで検証可能な内容にすること
  - OK: ファイル内容の確認、テスト実行結果、ビルド成功
  - NG: ブラウザでの手動確認、外部サービスの目視確認

## 実行プロンプトの注意
- 実行プロンプトは別の AI エージェントへの指示書
- 背景・目的、具体的な作業手順、達成条件を含めること
- 実行エージェントはタスク YAML を読まない前提で、自己完結した内容にすること"""

    prompt_parts = [f"# タスク: {task.title}"]
    if task.description:
        prompt_parts.append(f"\n{task.description}")

    ds_type = datasource.get("type", "") if datasource else ""
    if ds_type:
        prompt_parts.append(f"\nデータソース種別: {ds_type}")

    if task.feedback:
        prompt_parts.append("\n# フィードバック")
        for group in task.feedback:
            prompt_parts.append(f"\n## 収集日時: {group.collected_at}")
            for item in group.items:
                prompt_parts.append(f"- [{item.source}] {item.author}: {item.body}")

    if task.history:
        prompt_parts.append("\n# 実行履歴")
        for h in task.history:
            prompt_parts.append(f"- {h.dispatch_id}: {h.summary}")

    prompt_parts.append(
        "\n# 指示\n"
        "作業ディレクトリのソースコードを調査し、ユーザーと対話しながらタスクを計画してください。\n"
        "計画が完了したらタスク YAML を更新してください。"
    )

    prompt = "\n".join(prompt_parts)
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
    elif datasource:
        operations = datasource.get("operations") or {}
        if operations:
            op_lines = []
            for op_name, op_def in operations.items():
                cmd = op_def.get("command", "")
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

### done (タスク完了)
1. completion_actions のコマンドを順次実行
2. {yaml_path} の status を `done` に更新
3. {index_path} の該当エントリ (id: {task_id}) の status を `done` に更新

### abort (タスク中止)
1. {yaml_path} の status を `aborted` に更新
2. {index_path} の該当エントリ (id: {task_id}) の status を `aborted` に更新
{completion_section}"""

    prompt_parts = [f"# タスク: {task.title}"]
    if task.description:
        prompt_parts.append(f"\n{task.description}")

    ds_type = datasource.get("type", "") if datasource else ""
    if ds_type:
        prompt_parts.append(f"\nデータソース種別: {ds_type}")

    if task.history:
        prompt_parts.append("\n# 実行履歴")
        for h in task.history:
            prompt_parts.append(f"- {h.dispatch_id}: {h.summary}")

    if task.feedback:
        prompt_parts.append("\n# フィードバック")
        for group in task.feedback:
            prompt_parts.append(f"\n## 収集日時: {group.collected_at}")
            for item in group.items:
                prompt_parts.append(f"- [{item.source}] {item.author}: {item.body}")

    prompt = "\n".join(prompt_parts)
    return system_prompt, prompt
