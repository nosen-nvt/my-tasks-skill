"""Task Store — タスク YAML / Index の操作ロジックを集約するモジュール。"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import yaml

JST = timezone(timedelta(hours=9))


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now(JST).strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclass
class IndexEntry:
    """index.jsonl の各行に対応する軽量エントリ。"""
    id: str
    remote_id: str = ""
    datasource_id: str = ""
    title: str = ""
    status: str = "pending"
    project_id: str = ""
    run_count: int = 0
    generation: int = 1

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "remote_id": self.remote_id,
            "datasource_id": self.datasource_id,
            "title": self.title,
            "status": self.status,
            "project_id": self.project_id,
            "run_count": self.run_count,
            "generation": self.generation,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "IndexEntry":
        return cls(
            id=d["id"],
            remote_id=d.get("remote_id", ""),
            datasource_id=d.get("datasource_id", ""),
            title=d.get("title", ""),
            status=d.get("status", "pending"),
            project_id=d.get("project_id", ""),
            run_count=d.get("run_count", 0),
            generation=d.get("generation", 1),
        )


@dataclass
class Generation:
    """generations[] の各エントリ。"""
    seq: int
    type: str = "dispatch"
    status: str = "running"
    started_at: str | None = None
    finished_at: str | None = None
    output: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "type": self.type,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "output": self.output,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Generation":
        return cls(
            seq=d.get("seq", 0),
            type=d.get("type", "dispatch"),
            status=d.get("status", "running"),
            started_at=d.get("started_at"),
            finished_at=d.get("finished_at"),
            output=d.get("output") or {},
        )


@dataclass
class FeedbackItem:
    """feedback[] の各エントリ。"""
    source: str
    body: str = ""
    author: str = ""
    timestamp: str = ""
    generation: int = 1

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "author": self.author,
            "timestamp": self.timestamp,
            "body": self.body,
            "generation": self.generation,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FeedbackItem":
        return cls(
            source=d.get("source", ""),
            body=d.get("body", ""),
            author=d.get("author", ""),
            timestamp=d.get("timestamp", ""),
            generation=d.get("generation", 1),
        )


@dataclass
class HistoryEntry:
    """history[] の各エントリ（レガシー）。"""
    generation: int = 0
    summary: str = ""

    def to_dict(self) -> dict:
        return {"generation": self.generation, "summary": self.summary}

    @classmethod
    def from_dict(cls, d: dict) -> "HistoryEntry":
        return cls(
            generation=d.get("generation", 0),
            summary=d.get("summary", ""),
        )


@dataclass
class TaskData:
    """タスク YAML の全フィールドを保持するデータクラス。"""
    id: str
    remote_id: str = ""
    datasource_id: str = ""
    project_id: str = ""
    title: str = ""
    status: str = "pending"
    generation: int = 1
    description: str = ""
    open_questions: list[str] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    completion_actions: list = field(default_factory=list)
    execute_prompt: str = ""
    generations: list[Generation] = field(default_factory=list)
    history: list[HistoryEntry] = field(default_factory=list)
    feedback: list[FeedbackItem] = field(default_factory=list)
    feedback_cursor: dict[str, str] = field(default_factory=dict)
    pr_url: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "remote_id": self.remote_id,
            "datasource_id": self.datasource_id,
            "project_id": self.project_id,
            "title": self.title,
            "status": self.status,
            "generation": self.generation,
            "description": self.description,
            "open_questions": self.open_questions,
            "preconditions": self.preconditions,
            "acceptance_criteria": self.acceptance_criteria,
            "completion_actions": self.completion_actions,
            "execute_prompt": self.execute_prompt,
            "generations": [g.to_dict() for g in self.generations],
            "history": [h.to_dict() for h in self.history],
            "feedback": [f.to_dict() for f in self.feedback],
            "feedback_cursor": self.feedback_cursor,
            "pr_url": self.pr_url,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TaskData":
        generations_raw = d.get("generations") or []
        history_raw = d.get("history") or []
        feedback_raw = d.get("feedback") or []

        return cls(
            id=d.get("id", ""),
            remote_id=d.get("remote_id", ""),
            datasource_id=d.get("datasource_id", ""),
            project_id=d.get("project_id", ""),
            title=d.get("title", ""),
            status=d.get("status", "pending"),
            generation=d.get("generation", 1),
            description=d.get("description", ""),
            open_questions=d.get("open_questions") or [],
            preconditions=d.get("preconditions") or [],
            acceptance_criteria=d.get("acceptance_criteria") or [],
            completion_actions=d.get("completion_actions") or [],
            execute_prompt=d.get("execute_prompt", ""),
            generations=[Generation.from_dict(g) for g in generations_raw if isinstance(g, dict)],
            history=[HistoryEntry.from_dict(h) for h in history_raw if isinstance(h, dict)],
            feedback=[FeedbackItem.from_dict(f) for f in feedback_raw if isinstance(f, dict)],
            feedback_cursor=d.get("feedback_cursor") or {},
            pr_url=d.get("pr_url", ""),
        )

    def to_index_entry(self) -> "IndexEntry":
        return IndexEntry(
            id=self.id,
            remote_id=self.remote_id,
            datasource_id=self.datasource_id,
            title=self.title,
            status=self.status,
            project_id=self.project_id,
            generation=self.generation,
        )


# ---------------------------------------------------------------------------
# YAML I/O
# ---------------------------------------------------------------------------

def _dump_yaml(data: dict, path: Path) -> None:
    """YAML を書き出す。"""
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def load_task_yaml(tasks_dir: Path, task_id: str) -> TaskData | None:
    """タスク YAML を読み込む。存在しない場合は None を返す。"""
    yaml_path = tasks_dir / f"{task_id}.yaml"
    if not yaml_path.exists():
        return None
    with open(yaml_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return TaskData.from_dict(raw)


def save_task_yaml(tasks_dir: Path, task_id: str, data: TaskData) -> None:
    """タスク YAML を書き出す。"""
    yaml_path = tasks_dir / f"{task_id}.yaml"
    _dump_yaml(data.to_dict(), yaml_path)


# ---------------------------------------------------------------------------
# タスクインデックス (index.jsonl)
# ---------------------------------------------------------------------------

def load_index(tasks_dir: Path) -> list[IndexEntry]:
    """tasks/index.jsonl を読み込んでリストで返す。存在しない場合は空リストを返す。"""
    index_path = tasks_dir / "index.jsonl"
    if not index_path.exists():
        return []
    entries: list[IndexEntry] = []
    with open(index_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(IndexEntry.from_dict(json.loads(line)))
            except json.JSONDecodeError:
                continue
    return entries


def save_index(tasks_dir: Path, entries: list[IndexEntry]) -> None:
    """tasks/index.jsonl に書き出す。"""
    tasks_dir.mkdir(parents=True, exist_ok=True)
    index_path = tasks_dir / "index.jsonl"
    with open(index_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# タスク ID 生成
# ---------------------------------------------------------------------------

def generate_task_id(index_entries: list[IndexEntry], tasks_dir: Path | None = None) -> str:
    """YYYYMMDD-NNN 形式のタスク ID を生成する。

    GC でインデックスから削除されたタスクの ID を再利用しないよう、
    発行済みの最大シーケンス番号を .seq ファイルに永続化する。
    """
    date_prefix = today_str()

    max_seq = 0
    for entry in index_entries:
        eid = entry.id
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

def create_task_yaml(tasks_dir: Path, entry: IndexEntry) -> None:
    """タスク実体の YAML ファイルを生成する。"""
    task_data = TaskData(
        id=entry.id,
        remote_id=entry.remote_id,
        datasource_id=entry.datasource_id,
        project_id=entry.project_id,
        title=entry.title,
        status=entry.status,
        generation=entry.generation,
    )
    yaml_path = tasks_dir / f"{entry.id}.yaml"
    _dump_yaml(task_data.to_dict(), yaml_path)


def update_task_yaml(tasks_dir: Path, entry: IndexEntry) -> None:
    """タスク YAML ファイルのメタデータ部分を更新する。"""
    task_id = entry.id
    yaml_path = tasks_dir / f"{task_id}.yaml"

    if not yaml_path.exists():
        create_task_yaml(tasks_dir, entry)
        return

    data = load_task_yaml(tasks_dir, task_id)
    if data is None:
        create_task_yaml(tasks_dir, entry)
        return

    data.title = entry.title
    data.status = entry.status
    data.generation = entry.generation
    data.remote_id = entry.remote_id or data.remote_id
    data.datasource_id = entry.datasource_id
    data.project_id = entry.project_id or data.project_id

    save_task_yaml(tasks_dir, task_id, data)


def reopen_task_yaml(tasks_dir: Path, entry: IndexEntry, prev_summary: str = "") -> None:
    """done タスクを再オープンする。history に前世代サマリを追加する。"""
    task_id = entry.id
    yaml_path = tasks_dir / f"{task_id}.yaml"

    if not yaml_path.exists():
        create_task_yaml(tasks_dir, entry)
        return

    data = load_task_yaml(tasks_dir, task_id)
    if data is None:
        create_task_yaml(tasks_dir, entry)
        return

    prev_gen = entry.generation - 1
    data.status = entry.status
    data.generation = entry.generation

    if entry.title:
        data.title = entry.title

    data.history.append(HistoryEntry(generation=prev_gen, summary=prev_summary or ""))

    save_task_yaml(tasks_dir, task_id, data)


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
    data = load_task_yaml(tasks_dir, task_id)
    if data is None:
        return
    if not hasattr(data, field):
        return
    setattr(data, field, value)
    save_task_yaml(tasks_dir, task_id, data)


def update_index_status(tasks_dir: Path, task_id: str, status: str) -> None:
    """Index 内の指定タスクのステータスを更新する。"""
    entries = load_index(tasks_dir)
    for entry in entries:
        if entry.id == task_id:
            entry.status = status
            break
    save_index(tasks_dir, entries)


# ---------------------------------------------------------------------------
# Generation 管理
# ---------------------------------------------------------------------------

def start_generation(tasks_dir: Path, task_id: str, gen_type: str, seq: int | None = None) -> Generation:
    """generations[] に新エントリを追加する。

    Args:
        gen_type: "dispatch" | "interactive"
        seq: 明示指定する場合。None なら自動採番。

    Returns:
        追加された Generation エントリ。
    """
    data = load_task_yaml(tasks_dir, task_id)
    if data is None:
        raise FileNotFoundError(f"Task not found: {task_id}")

    if seq is None:
        seq = (data.generations[-1].seq + 1) if data.generations else 1

    gen = Generation(
        seq=seq,
        type=gen_type,
        status="running",
        started_at=now_iso(),
    )
    data.generations.append(gen)
    save_task_yaml(tasks_dir, task_id, data)
    return gen


def finish_generation(tasks_dir: Path, task_id: str, seq: int, status: str, output: dict | None = None) -> None:
    """generations[seq] の status, finished_at, output を更新する。

    Args:
        status: done | needs_input | plan_ready | rejected | review_needed | aborted
        output: Generation の出力データ。
    """
    data = load_task_yaml(tasks_dir, task_id)
    if data is None:
        return

    for gen in data.generations:
        if gen.seq == seq:
            gen.status = status
            gen.finished_at = now_iso()
            if output is not None:
                gen.output = output
            break

    save_task_yaml(tasks_dir, task_id, data)


def get_latest_generation(tasks_dir: Path, task_id: str) -> Generation | None:
    """generations[-1] を返す。generations が空なら None。"""
    data = load_task_yaml(tasks_dir, task_id)
    if data is None:
        return None
    if not data.generations:
        return None
    return data.generations[-1]


def migrate_history_to_generations(data: TaskData) -> TaskData:
    """history[] → generations[] の自動変換（メモリ上のみ）。

    既存の history[] エントリを generations[] 形式に変換する。
    既に generations[] が存在する場合はそのまま返す。
    """
    if data.generations:
        return data

    if not data.history:
        return data

    data.generations = [
        Generation(
            seq=h.generation,
            type="dispatch",
            status="done",
            output={"summary": h.summary},
        )
        for h in data.history
    ]

    return data
