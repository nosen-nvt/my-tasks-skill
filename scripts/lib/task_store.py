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

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "remote_id": self.remote_id,
            "datasource_id": self.datasource_id,
            "title": self.title,
            "status": self.status,
            "project_id": self.project_id,
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
        )


@dataclass
class FeedbackItem:
    """feedback グループ内の個別アイテム。"""
    source: str
    body: str = ""
    author: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "author": self.author,
            "timestamp": self.timestamp,
            "body": self.body,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FeedbackItem":
        return cls(
            source=d.get("source", ""),
            body=d.get("body", ""),
            author=d.get("author", ""),
            timestamp=d.get("timestamp", ""),
        )


@dataclass
class FeedbackGroup:
    """収集タイミングごとにグループ化されたフィードバック。"""
    collected_at: str
    items: list[FeedbackItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "collected_at": self.collected_at,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FeedbackGroup":
        items_raw = d.get("items") or []
        return cls(
            collected_at=d.get("collected_at", ""),
            items=[FeedbackItem.from_dict(i) for i in items_raw if isinstance(i, dict)],
        )


@dataclass
class HistoryEntry:
    """history[] の各エントリ。"""
    dispatch_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    exit_code: int | None = None
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "dispatch_id": self.dispatch_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HistoryEntry":
        return cls(
            dispatch_id=d.get("dispatch_id", ""),
            started_at=d.get("started_at", ""),
            finished_at=d.get("finished_at", ""),
            exit_code=d.get("exit_code"),
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
    description: str = ""
    preconditions: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    completion_actions: list = field(default_factory=list)
    execute_prompt: str = ""
    pr_url: str = ""
    branch: str = ""
    dispatch_id: str = ""
    session_id: str = ""
    related_jobs: list[str] = field(default_factory=list)
    feedback: list[FeedbackGroup] = field(default_factory=list)
    feedback_cursor: dict[str, str] = field(default_factory=dict)
    history: list[HistoryEntry] = field(default_factory=list)
    plan_session_id: str = ""
    resume_requested: bool = False
    feedback_requested: bool = False
    actions_status: str = ""
    actions_error: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "remote_id": self.remote_id,
            "datasource_id": self.datasource_id,
            "project_id": self.project_id,
            "title": self.title,
            "status": self.status,
            "description": self.description,
            "preconditions": self.preconditions,
            "acceptance_criteria": self.acceptance_criteria,
            "completion_actions": self.completion_actions,
            "execute_prompt": self.execute_prompt,
            "pr_url": self.pr_url,
            "branch": self.branch,
            "dispatch_id": self.dispatch_id,
            "session_id": self.session_id,
            "related_jobs": self.related_jobs,
            "feedback": [g.to_dict() for g in self.feedback],
            "feedback_cursor": self.feedback_cursor,
            "history": [h.to_dict() for h in self.history],
            "plan_session_id": self.plan_session_id,
            "resume_requested": self.resume_requested,
            "feedback_requested": self.feedback_requested,
            "actions_status": self.actions_status,
            "actions_error": self.actions_error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TaskData":
        feedback_raw = d.get("feedback") or []
        history_raw = d.get("history") or []

        # feedback の後方互換: v1 のフラットリスト形式を検出してグループ化
        feedback_groups: list[FeedbackGroup] = []
        if feedback_raw and isinstance(feedback_raw[0], dict):
            if "collected_at" in feedback_raw[0]:
                # v2 形式
                feedback_groups = [FeedbackGroup.from_dict(g) for g in feedback_raw]
            elif "source" in feedback_raw[0]:
                # v1 フラット形式 → 単一グループに変換
                items = [FeedbackItem.from_dict(f) for f in feedback_raw]
                if items:
                    feedback_groups = [FeedbackGroup(
                        collected_at=items[0].timestamp or now_iso(),
                        items=items,
                    )]

        # history の後方互換: v1 の generation ベース形式
        history_entries: list[HistoryEntry] = []
        if history_raw and isinstance(history_raw[0], dict):
            if "dispatch_id" in history_raw[0]:
                history_entries = [HistoryEntry.from_dict(h) for h in history_raw]
            elif "generation" in history_raw[0]:
                # v1 形式 → 変換
                for h in history_raw:
                    history_entries.append(HistoryEntry(
                        summary=h.get("summary", ""),
                    ))

        return cls(
            id=d.get("id", ""),
            remote_id=d.get("remote_id", ""),
            datasource_id=d.get("datasource_id", ""),
            project_id=d.get("project_id", ""),
            title=d.get("title", ""),
            status=d.get("status", "pending"),
            description=d.get("description", ""),
            preconditions=d.get("preconditions") or [],
            acceptance_criteria=d.get("acceptance_criteria") or [],
            completion_actions=d.get("completion_actions") or [],
            execute_prompt=d.get("execute_prompt", ""),
            pr_url=d.get("pr_url", ""),
            branch=d.get("branch", ""),
            dispatch_id=d.get("dispatch_id", ""),
            session_id=d.get("session_id", ""),
            related_jobs=d.get("related_jobs") or [],
            feedback=feedback_groups,
            feedback_cursor=d.get("feedback_cursor") or {},
            history=history_entries,
            plan_session_id=d.get("plan_session_id", ""),
            resume_requested=bool(d.get("resume_requested", False)),
            feedback_requested=bool(d.get("feedback_requested", False)),
            actions_status=d.get("actions_status", ""),
            actions_error=d.get("actions_error", ""),
        )

    def to_index_entry(self) -> "IndexEntry":
        return IndexEntry(
            id=self.id,
            remote_id=self.remote_id,
            datasource_id=self.datasource_id,
            title=self.title,
            status=self.status,
            project_id=self.project_id,
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
    """YYYYMMDD-NNN 形式のタスク ID を生成する。"""
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
    data.remote_id = entry.remote_id or data.remote_id
    data.datasource_id = entry.datasource_id
    data.project_id = entry.project_id or data.project_id

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
