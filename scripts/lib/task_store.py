"""Task Store — タスク YAML / Index の操作ロジックを集約するモジュール。"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

JST = timezone(timedelta(hours=9))


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now(JST).strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# YAML I/O
# ---------------------------------------------------------------------------

def _dump_yaml(data: dict, path: Path) -> None:
    """YAML を書き出す。"""
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def load_task_yaml(tasks_dir: Path, task_id: str) -> dict | None:
    """タスク YAML を読み込む。存在しない場合は None を返す。"""
    yaml_path = tasks_dir / f"{task_id}.yaml"
    if not yaml_path.exists():
        return None
    with open(yaml_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_task_yaml(tasks_dir: Path, task_id: str, data: dict) -> None:
    """タスク YAML を書き出す。"""
    yaml_path = tasks_dir / f"{task_id}.yaml"
    _dump_yaml(data, yaml_path)


# ---------------------------------------------------------------------------
# タスクインデックス (index.jsonl)
# ---------------------------------------------------------------------------

def load_index(tasks_dir: Path) -> list[dict]:
    """tasks/index.jsonl を読み込んでリストで返す。存在しない場合は空リストを返す。"""
    index_path = tasks_dir / "index.jsonl"
    if not index_path.exists():
        return []
    entries = []
    with open(index_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def save_index(tasks_dir: Path, entries: list[dict]) -> None:
    """tasks/index.jsonl に書き出す。"""
    tasks_dir.mkdir(parents=True, exist_ok=True)
    index_path = tasks_dir / "index.jsonl"
    with open(index_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# タスク ID 生成
# ---------------------------------------------------------------------------

def generate_task_id(index_entries: list[dict], tasks_dir: Path | None = None) -> str:
    """YYYYMMDD-NNN 形式のタスク ID を生成する。

    GC でインデックスから削除されたタスクの ID を再利用しないよう、
    発行済みの最大シーケンス番号を .seq ファイルに永続化する。
    """
    date_prefix = today_str()

    max_seq = 0
    for entry in index_entries:
        eid = entry.get("id", "")
        if eid.startswith(date_prefix + "-"):
            try:
                seq = int(eid[len(date_prefix) + 1:])
                if seq > max_seq:
                    max_seq = seq
            except ValueError:
                pass

    # .seq ファイルから過去の最大値を取得
    seq_data = {}
    if tasks_dir:
        seq_file = tasks_dir / ".seq"
        if seq_file.exists():
            try:
                seq_data = json.loads(seq_file.read_text(encoding="utf-8"))
                stored = seq_data.get(date_prefix, 0)
                if stored > max_seq:
                    max_seq = stored
            except (json.JSONDecodeError, OSError):
                pass

    new_seq = max_seq + 1

    # .seq ファイルに保存
    if tasks_dir:
        seq_data[date_prefix] = new_seq
        seq_file = tasks_dir / ".seq"
        try:
            seq_file.write_text(json.dumps(seq_data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    return f"{date_prefix}-{new_seq:03d}"


# ---------------------------------------------------------------------------
# タスク YAML CRUD
# ---------------------------------------------------------------------------

def _task_yaml_data(entry: dict) -> dict:
    """タスク YAML の初期データを構築する。"""
    return {
        "id": entry["id"],
        "remote_id": entry.get("remote_id", ""),
        "datasource_id": entry["datasource_id"],
        "project_id": entry.get("project_id", ""),
        "title": entry["title"],
        "status": entry["status"],
        "generation": entry.get("generation", 1),
        "description": "",
        "open_questions": [],
        "preconditions": [],
        "acceptance_criteria": [],
        "completion_actions": [],
        "execute_prompt": "",
        "generations": [],
        "history": [],
        "feedback": [],
        "feedback_cursor": {},
    }


def create_task_yaml(tasks_dir: Path, entry: dict) -> None:
    """タスク実体の YAML ファイルを生成する。"""
    task_id = entry["id"]
    yaml_path = tasks_dir / f"{task_id}.yaml"
    data = _task_yaml_data(entry)
    _dump_yaml(data, yaml_path)


def update_task_yaml(tasks_dir: Path, entry: dict) -> None:
    """タスク YAML ファイルのメタデータ部分を更新する。"""
    task_id = entry["id"]
    yaml_path = tasks_dir / f"{task_id}.yaml"

    if not yaml_path.exists():
        create_task_yaml(tasks_dir, entry)
        return

    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    data["title"] = entry["title"]
    data["status"] = entry["status"]
    data["generation"] = entry.get("generation", 1)
    data["remote_id"] = entry.get("remote_id", data.get("remote_id", ""))
    data["datasource_id"] = entry["datasource_id"]
    data["project_id"] = entry.get("project_id", data.get("project_id", ""))

    _dump_yaml(data, yaml_path)


def reopen_task_yaml(tasks_dir: Path, entry: dict, prev_summary: str = "") -> None:
    """done タスクを再オープンする。history に前世代サマリを追加する。"""
    task_id = entry["id"]
    yaml_path = tasks_dir / f"{task_id}.yaml"

    if not yaml_path.exists():
        create_task_yaml(tasks_dir, entry)
        return

    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    prev_gen = entry.get("generation", 2) - 1
    data["status"] = entry["status"]
    data["generation"] = entry.get("generation", 2)

    if entry.get("title"):
        data["title"] = entry["title"]

    # history に前世代サマリを追加
    history = data.get("history", [])
    if not isinstance(history, list):
        history = []
    history.append({
        "generation": prev_gen,
        "summary": prev_summary or "",
    })
    data["history"] = history

    _dump_yaml(data, yaml_path)


def delete_task(tasks_dir: Path, task_id: str) -> None:
    """index エントリの削除は呼び出し側で行う。ここでは .yaml ファイルの削除のみ。"""
    yaml_path = tasks_dir / f"{task_id}.yaml"
    yaml_path.unlink(missing_ok=True)
    # 後方互換: .md が残っていれば削除
    md_path = tasks_dir / f"{task_id}.md"
    md_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# フィールド単位更新
# ---------------------------------------------------------------------------

def update_task_field(tasks_dir: Path, task_id: str, field: str, value) -> None:
    """タスク YAML の指定フィールドを更新する。"""
    yaml_path = tasks_dir / f"{task_id}.yaml"
    if not yaml_path.exists():
        return
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        data[field] = value
        _dump_yaml(data, yaml_path)
    except (yaml.YAMLError, OSError):
        pass


def update_index_status(tasks_dir: Path, task_id: str, status: str) -> None:
    """Index 内の指定タスクのステータスを更新する。"""
    entries = load_index(tasks_dir)
    for entry in entries:
        if entry.get("id") == task_id:
            entry["status"] = status
            break
    save_index(tasks_dir, entries)


# ---------------------------------------------------------------------------
# Generation 管理
# ---------------------------------------------------------------------------

def start_generation(tasks_dir: Path, task_id: str, gen_type: str, seq: int | None = None) -> dict:
    """generations[] に新エントリを追加する。

    Args:
        gen_type: "dispatch" | "interactive"
        seq: 明示指定する場合。None なら自動採番。

    Returns:
        追加された generation エントリ。
    """
    data = load_task_yaml(tasks_dir, task_id)
    if data is None:
        raise FileNotFoundError(f"Task not found: {task_id}")

    generations = data.get("generations", [])
    if not isinstance(generations, list):
        generations = []

    if seq is None:
        seq = (generations[-1]["seq"] + 1) if generations else 1

    entry = {
        "seq": seq,
        "type": gen_type,
        "status": "running",
        "started_at": now_iso(),
        "finished_at": None,
        "output": {},
    }
    generations.append(entry)
    data["generations"] = generations
    save_task_yaml(tasks_dir, task_id, data)
    return entry


def finish_generation(tasks_dir: Path, task_id: str, seq: int, status: str, output: dict | None = None) -> None:
    """generations[seq] の status, finished_at, output を更新する。

    Args:
        status: done | needs_input | plan_ready | rejected | review_needed | aborted
        output: Generation の出力データ。
    """
    data = load_task_yaml(tasks_dir, task_id)
    if data is None:
        return

    generations = data.get("generations", [])
    if not isinstance(generations, list):
        return

    for gen in generations:
        if gen.get("seq") == seq:
            gen["status"] = status
            gen["finished_at"] = now_iso()
            if output is not None:
                gen["output"] = output
            break

    data["generations"] = generations
    save_task_yaml(tasks_dir, task_id, data)


def get_latest_generation(tasks_dir: Path, task_id: str) -> dict | None:
    """generations[-1] を返す。generations が空なら None。"""
    data = load_task_yaml(tasks_dir, task_id)
    if data is None:
        return None
    generations = data.get("generations", [])
    if not isinstance(generations, list) or not generations:
        return None
    return generations[-1]


def migrate_history_to_generations(data: dict) -> dict:
    """history[] → generations[] の自動変換（メモリ上のみ）。

    既存の history[] エントリを generations[] 形式に変換する。
    既に generations[] が存在する場合はそのまま返す。
    """
    generations = data.get("generations", [])
    if isinstance(generations, list) and generations:
        return data

    history = data.get("history", [])
    if not isinstance(history, list) or not history:
        return data

    converted = []
    for h in history:
        gen_seq = h.get("generation", 0)
        converted.append({
            "seq": gen_seq,
            "type": "dispatch",
            "status": "done",
            "started_at": None,
            "finished_at": None,
            "output": {
                "summary": h.get("summary", ""),
            },
        })

    data["generations"] = converted
    return data
