"""task_store モジュールのテスト。"""

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lib.task_store import (
    load_index, save_index, generate_task_id,
    load_task_yaml, save_task_yaml,
    create_task_yaml, update_task_yaml, reopen_task_yaml, delete_task,
    update_task_field, update_index_status,
    start_generation, finish_generation, get_latest_generation,
    migrate_history_to_generations,
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
            {"id": "20260318-001", "title": "Task 1", "status": "pending"},
            {"id": "20260318-002", "title": "Task 2", "status": "done"},
        ]
        save_index(tasks_dir, entries)
        loaded = load_index(tasks_dir)
        assert len(loaded) == 2
        assert loaded[0]["id"] == "20260318-001"
        assert loaded[1]["status"] == "done"

    def test_overwrite(self, tasks_dir):
        save_index(tasks_dir, [{"id": "a", "title": "A", "status": "pending"}])
        save_index(tasks_dir, [{"id": "b", "title": "B", "status": "done"}])
        loaded = load_index(tasks_dir)
        assert len(loaded) == 1
        assert loaded[0]["id"] == "b"


class TestTaskYamlRoundTrip:
    """タスク YAML の読み書きラウンドトリップ。"""

    def test_create_and_load(self, tasks_dir):
        entry = {
            "id": "20260318-001",
            "datasource_id": "jira",
            "title": "Test Task",
            "status": "pending",
        }
        create_task_yaml(tasks_dir, entry)

        data = load_task_yaml(tasks_dir, "20260318-001")
        assert data is not None
        assert data["id"] == "20260318-001"
        assert data["title"] == "Test Task"
        assert data["status"] == "pending"
        assert data["generations"] == []
        assert data["history"] == []

    def test_load_nonexistent(self, tasks_dir):
        assert load_task_yaml(tasks_dir, "nonexistent") is None

    def test_save_and_load(self, tasks_dir):
        data = {"id": "t1", "title": "Hello", "custom": "field"}
        save_task_yaml(tasks_dir, "t1", data)
        loaded = load_task_yaml(tasks_dir, "t1")
        assert loaded["custom"] == "field"

    def test_update_task_yaml(self, tasks_dir):
        entry = {
            "id": "t1",
            "datasource_id": "jira",
            "title": "Original",
            "status": "pending",
        }
        create_task_yaml(tasks_dir, entry)

        entry["title"] = "Updated"
        entry["status"] = "in_progress"
        update_task_yaml(tasks_dir, entry)

        data = load_task_yaml(tasks_dir, "t1")
        assert data["title"] == "Updated"
        assert data["status"] == "in_progress"

    def test_update_nonexistent_creates(self, tasks_dir):
        entry = {
            "id": "t2",
            "datasource_id": "jira",
            "title": "New",
            "status": "pending",
        }
        update_task_yaml(tasks_dir, entry)
        assert load_task_yaml(tasks_dir, "t2") is not None


class TestReopenTask:
    """タスク再オープン。"""

    def test_reopen_adds_history(self, tasks_dir):
        entry = {
            "id": "t1",
            "datasource_id": "jira",
            "title": "Task",
            "status": "done",
            "generation": 1,
        }
        create_task_yaml(tasks_dir, entry)

        entry["status"] = "pending"
        entry["generation"] = 2
        reopen_task_yaml(tasks_dir, entry, prev_summary="Gen 1 done")

        data = load_task_yaml(tasks_dir, "t1")
        assert data["status"] == "pending"
        assert data["generation"] == 2
        assert len(data["history"]) == 1
        assert data["history"][0]["generation"] == 1
        assert data["history"][0]["summary"] == "Gen 1 done"


class TestDeleteTask:
    """タスク削除。"""

    def test_delete(self, tasks_dir):
        entry = {
            "id": "t1",
            "datasource_id": "jira",
            "title": "Task",
            "status": "pending",
        }
        create_task_yaml(tasks_dir, entry)
        assert (tasks_dir / "t1.yaml").exists()

        delete_task(tasks_dir, "t1")
        assert not (tasks_dir / "t1.yaml").exists()

    def test_delete_nonexistent(self, tasks_dir):
        # should not raise
        delete_task(tasks_dir, "nonexistent")


class TestGenerateTaskId:
    """タスク ID 生成。"""

    def test_sequential(self, tasks_dir):
        entries = []
        id1 = generate_task_id(entries, tasks_dir)
        entries.append({"id": id1})
        id2 = generate_task_id(entries, tasks_dir)
        assert id1 != id2
        # Both should end with -001 and -002
        assert id1.endswith("-001")
        assert id2.endswith("-002")


class TestUpdateTaskField:
    """フィールド単位更新。"""

    def test_update_field(self, tasks_dir):
        entry = {
            "id": "t1",
            "datasource_id": "jira",
            "title": "Task",
            "status": "pending",
        }
        create_task_yaml(tasks_dir, entry)

        update_task_field(tasks_dir, "t1", "pr_url", "https://example.com/pr/1")

        data = load_task_yaml(tasks_dir, "t1")
        assert data["pr_url"] == "https://example.com/pr/1"

    def test_update_field_nonexistent(self, tasks_dir):
        # should not raise
        update_task_field(tasks_dir, "nonexistent", "field", "value")


class TestUpdateIndexStatus:
    """Index ステータス更新。"""

    def test_update_status(self, tasks_dir):
        entries = [
            {"id": "t1", "status": "pending"},
            {"id": "t2", "status": "pending"},
        ]
        save_index(tasks_dir, entries)

        update_index_status(tasks_dir, "t1", "in_progress")

        loaded = load_index(tasks_dir)
        assert loaded[0]["status"] == "in_progress"
        assert loaded[1]["status"] == "pending"


class TestStartGeneration:
    """Generation の開始。"""

    def test_start_generation(self, tasks_dir):
        entry = {
            "id": "t1",
            "datasource_id": "jira",
            "title": "Task",
            "status": "pending",
        }
        create_task_yaml(tasks_dir, entry)

        gen = start_generation(tasks_dir, "t1", "dispatch")
        assert gen["seq"] == 1
        assert gen["type"] == "dispatch"
        assert gen["status"] == "running"
        assert gen["started_at"] is not None

        data = load_task_yaml(tasks_dir, "t1")
        assert len(data["generations"]) == 1

    def test_start_multiple(self, tasks_dir):
        entry = {
            "id": "t1",
            "datasource_id": "jira",
            "title": "Task",
            "status": "pending",
        }
        create_task_yaml(tasks_dir, entry)

        start_generation(tasks_dir, "t1", "dispatch")
        gen2 = start_generation(tasks_dir, "t1", "interactive")
        assert gen2["seq"] == 2

    def test_start_nonexistent_raises(self, tasks_dir):
        with pytest.raises(FileNotFoundError):
            start_generation(tasks_dir, "nonexistent", "dispatch")


class TestFinishGeneration:
    """Generation の完了。"""

    def test_finish_generation(self, tasks_dir):
        entry = {
            "id": "t1",
            "datasource_id": "jira",
            "title": "Task",
            "status": "pending",
        }
        create_task_yaml(tasks_dir, entry)
        start_generation(tasks_dir, "t1", "dispatch")

        finish_generation(tasks_dir, "t1", 1, "done", {"summary": "completed"})

        data = load_task_yaml(tasks_dir, "t1")
        gen = data["generations"][0]
        assert gen["status"] == "done"
        assert gen["finished_at"] is not None
        assert gen["output"]["summary"] == "completed"

    def test_finish_nonexistent_seq(self, tasks_dir):
        entry = {
            "id": "t1",
            "datasource_id": "jira",
            "title": "Task",
            "status": "pending",
        }
        create_task_yaml(tasks_dir, entry)
        start_generation(tasks_dir, "t1", "dispatch")

        # finishing a non-existent seq should not crash
        finish_generation(tasks_dir, "t1", 999, "done", {})
        data = load_task_yaml(tasks_dir, "t1")
        assert data["generations"][0]["status"] == "running"


class TestGetLatestGeneration:
    """最新 Generation の取得。"""

    def test_get_latest(self, tasks_dir):
        entry = {
            "id": "t1",
            "datasource_id": "jira",
            "title": "Task",
            "status": "pending",
        }
        create_task_yaml(tasks_dir, entry)
        start_generation(tasks_dir, "t1", "dispatch")
        start_generation(tasks_dir, "t1", "interactive")

        latest = get_latest_generation(tasks_dir, "t1")
        assert latest["seq"] == 2
        assert latest["type"] == "interactive"

    def test_get_latest_empty(self, tasks_dir):
        entry = {
            "id": "t1",
            "datasource_id": "jira",
            "title": "Task",
            "status": "pending",
        }
        create_task_yaml(tasks_dir, entry)
        assert get_latest_generation(tasks_dir, "t1") is None


class TestMigrateHistoryToGenerations:
    """history[] → generations[] の変換。"""

    def test_migrate(self):
        data = {
            "history": [
                {"generation": 1, "summary": "Gen 1 done"},
                {"generation": 2, "summary": "Gen 2 done"},
            ],
            "generations": [],
        }
        result = migrate_history_to_generations(data)
        assert len(result["generations"]) == 2
        assert result["generations"][0]["seq"] == 1
        assert result["generations"][0]["output"]["summary"] == "Gen 1 done"

    def test_skip_if_generations_exist(self):
        data = {
            "history": [{"generation": 1, "summary": "old"}],
            "generations": [{"seq": 1, "type": "dispatch", "status": "done"}],
        }
        result = migrate_history_to_generations(data)
        assert len(result["generations"]) == 1
        assert result["generations"][0].get("output") is None  # not overwritten

    def test_no_history(self):
        data = {"history": [], "generations": []}
        result = migrate_history_to_generations(data)
        assert result["generations"] == []
