"""task_store モジュールのテスト (v2)。"""

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lib.task_store import (
    IndexEntry, TaskData, HistoryEntry, FeedbackItem, FeedbackGroup,
    load_index, save_index, generate_task_id,
    load_task_yaml, save_task_yaml,
    create_task_yaml, update_task_yaml, delete_task,
    update_task_field, update_index_status,
)


@pytest.fixture
def tasks_dir(tmp_path):
    d = tmp_path / "tasks"
    d.mkdir()
    return d


class TestIndexRoundTrip:
    """index.jsonl の読み書きラウンドトリップ。"""

    def test_empty(self, tasks_dir):
        assert load_index(tasks_dir) == []

    def test_save_and_load(self, tasks_dir):
        entries = [
            IndexEntry(id="20260318-001", title="Task 1", status="pending"),
            IndexEntry(id="20260318-002", title="Task 2", status="done"),
        ]
        save_index(tasks_dir, entries)
        loaded = load_index(tasks_dir)
        assert len(loaded) == 2
        assert loaded[0].id == "20260318-001"
        assert loaded[1].status == "done"

    def test_overwrite(self, tasks_dir):
        save_index(tasks_dir, [IndexEntry(id="a", title="A", status="pending")])
        save_index(tasks_dir, [IndexEntry(id="b", title="B", status="done")])
        loaded = load_index(tasks_dir)
        assert len(loaded) == 1
        assert loaded[0].id == "b"


class TestTaskYamlRoundTrip:
    """タスク YAML の読み書きラウンドトリップ。"""

    def test_create_and_load(self, tasks_dir):
        entry = IndexEntry(
            id="20260318-001",
            datasource_id="jira",
            title="Test Task",
            status="pending",
        )
        create_task_yaml(tasks_dir, entry)

        data = load_task_yaml(tasks_dir, "20260318-001")
        assert data is not None
        assert data.id == "20260318-001"
        assert data.title == "Test Task"
        assert data.status == "pending"
        assert data.history == []
        assert data.feedback == []

    def test_load_nonexistent(self, tasks_dir):
        assert load_task_yaml(tasks_dir, "nonexistent") is None

    def test_save_and_load(self, tasks_dir):
        data = TaskData(id="t1", title="Hello")
        save_task_yaml(tasks_dir, "t1", data)
        loaded = load_task_yaml(tasks_dir, "t1")
        assert loaded is not None
        assert loaded.title == "Hello"

    def test_update_task_yaml(self, tasks_dir):
        entry = IndexEntry(
            id="t1",
            datasource_id="jira",
            title="Original",
            status="pending",
        )
        create_task_yaml(tasks_dir, entry)

        entry.title = "Updated"
        entry.status = "in_progress"
        update_task_yaml(tasks_dir, entry)

        data = load_task_yaml(tasks_dir, "t1")
        assert data.title == "Updated"
        assert data.status == "in_progress"

    def test_update_nonexistent_creates(self, tasks_dir):
        entry = IndexEntry(
            id="t2",
            datasource_id="jira",
            title="New",
            status="pending",
        )
        update_task_yaml(tasks_dir, entry)
        assert load_task_yaml(tasks_dir, "t2") is not None


class TestDeleteTask:
    """タスク削除。"""

    def test_delete(self, tasks_dir):
        entry = IndexEntry(
            id="t1",
            datasource_id="jira",
            title="Task",
            status="pending",
        )
        create_task_yaml(tasks_dir, entry)
        assert (tasks_dir / "t1.yaml").exists()

        delete_task(tasks_dir, "t1")
        assert not (tasks_dir / "t1.yaml").exists()

    def test_delete_nonexistent(self, tasks_dir):
        delete_task(tasks_dir, "nonexistent")


class TestGenerateTaskId:
    """タスク ID 生成。"""

    def test_sequential(self, tasks_dir):
        entries: list[IndexEntry] = []
        id1 = generate_task_id(entries, tasks_dir)
        entries.append(IndexEntry(id=id1))
        id2 = generate_task_id(entries, tasks_dir)
        assert id1 != id2
        assert id1.endswith("-001")
        assert id2.endswith("-002")


class TestUpdateTaskField:
    """フィールド単位更新。"""

    def test_update_field(self, tasks_dir):
        entry = IndexEntry(
            id="t1",
            datasource_id="jira",
            title="Task",
            status="pending",
        )
        create_task_yaml(tasks_dir, entry)

        update_task_field(tasks_dir, "t1", "pr_url", "https://example.com/pr/1")

        data = load_task_yaml(tasks_dir, "t1")
        assert data.pr_url == "https://example.com/pr/1"

    def test_update_field_nonexistent(self, tasks_dir):
        update_task_field(tasks_dir, "nonexistent", "field", "value")


class TestUpdateIndexStatus:
    """Index ステータス更新。"""

    def test_update_status(self, tasks_dir):
        entries = [
            IndexEntry(id="t1", status="pending"),
            IndexEntry(id="t2", status="pending"),
        ]
        save_index(tasks_dir, entries)

        update_index_status(tasks_dir, "t1", "in_progress")

        loaded = load_index(tasks_dir)
        assert loaded[0].status == "in_progress"
        assert loaded[1].status == "pending"


class TestFeedbackGrouping:
    """フィードバックのグループ化。"""

    def test_v2_format_roundtrip(self, tasks_dir):
        data = TaskData(id="t1", title="Task")
        group = FeedbackGroup(
            collected_at="2026-03-24T10:00:00+09:00",
            items=[
                FeedbackItem(source="github_pr", author="user1", body="fix this"),
                FeedbackItem(source="github_pr_review", author="user2", body="[CHANGES_REQUESTED] add tests"),
            ],
        )
        data.feedback.append(group)
        save_task_yaml(tasks_dir, "t1", data)

        loaded = load_task_yaml(tasks_dir, "t1")
        assert len(loaded.feedback) == 1
        assert loaded.feedback[0].collected_at == "2026-03-24T10:00:00+09:00"
        assert len(loaded.feedback[0].items) == 2
        assert loaded.feedback[0].items[0].source == "github_pr"

    def test_v1_flat_backward_compat(self):
        """v1 のフラットなフィードバック形式が v2 グループに変換される。"""
        raw = {
            "id": "t1",
            "title": "Task",
            "feedback": [
                {"source": "jira_comment", "author": "u1", "body": "old", "timestamp": "2026-03-20T10:00:00+09:00", "generation": 1},
                {"source": "user", "author": "", "body": "manual", "timestamp": "2026-03-21T10:00:00+09:00", "generation": 1},
            ],
        }
        data = TaskData.from_dict(raw)
        assert len(data.feedback) == 1
        assert len(data.feedback[0].items) == 2
        assert data.feedback[0].items[0].source == "jira_comment"


class TestHistoryV2:
    """v2 の実行履歴。"""

    def test_history_roundtrip(self, tasks_dir):
        data = TaskData(id="t1", title="Task")
        data.history.append(HistoryEntry(
            dispatch_id="bo-3",
            started_at="2026-03-24T10:00:00+09:00",
            finished_at="2026-03-24T10:15:00+09:00",
            exit_code=0,
            summary="API 実装完了",
        ))
        save_task_yaml(tasks_dir, "t1", data)

        loaded = load_task_yaml(tasks_dir, "t1")
        assert len(loaded.history) == 1
        assert loaded.history[0].dispatch_id == "bo-3"
        assert loaded.history[0].exit_code == 0

    def test_v1_history_backward_compat(self):
        """v1 の generation ベース履歴が変換される。"""
        raw = {
            "id": "t1",
            "title": "Task",
            "history": [
                {"generation": 1, "summary": "Gen 1 done"},
                {"generation": 2, "summary": "Gen 2 done"},
            ],
        }
        data = TaskData.from_dict(raw)
        assert len(data.history) == 2
        assert data.history[0].summary == "Gen 1 done"


class TestSessionId:
    """session_id フィールド。"""

    def test_session_id_roundtrip(self, tasks_dir):
        data = TaskData(
            id="t1",
            title="Task",
            session_id="123e4567-e89b-12d3-a456-426614174000",
            dispatch_id="bo-5",
        )
        save_task_yaml(tasks_dir, "t1", data)

        loaded = load_task_yaml(tasks_dir, "t1")
        assert loaded.session_id == "123e4567-e89b-12d3-a456-426614174000"
        assert loaded.dispatch_id == "bo-5"
