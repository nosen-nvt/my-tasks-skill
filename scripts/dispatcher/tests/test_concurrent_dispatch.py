"""同時ディスパッチ挙動の検証テスト。

■ テスト結果サマリー

asyncio 単一スレッドモデルにおける同時ディスパッチの安全性を検証した結果:

1. 同一プロジェクト同時ディスパッチ (シナリオ A):
   - 2つの Lifecycle が正常に lifecycles dict に登録される
   - 各 Lifecycle の context.yaml が独立して作成される
   - lifecycles.jsonl に両方の Lifecycle が正しく保存される
   - 【発見】プロジェクト単位の排他制御にレースコンディションが存在する:
     _dispatch_internal() は asyncio.create_task(execute_job(job)) でジョブを
     スケジュールするが、execute_job が実際に実行されるまでジョブの status は
     "queued" のまま。asyncio.gather で2つの dispatch_plan を同時に呼ぶと、
     2つ目の _dispatch_internal が running_project_ids() をチェックする時点で
     1つ目のジョブはまだ "queued" であるため、排他チェックをすり抜けて
     両方のジョブが running になってしまう。
     これは asyncio.create_task の非即時実行特性に起因するもの。

2. 異なるプロジェクト同時ディスパッチ (シナリオ B):
   - スロット数が十分な場合、両方のジョブが同時に running になる
   - lifecycles.jsonl に両方の Lifecycle が保存される
   - jobs.jsonl に両方のジョブが保存される
   - 共有ファイルの書き込み競合は発生しない

3. Lifecycle 状態遷移の独立性 (シナリオ C):
   - on_job_complete は lifecycle_id でジョブを正しい Lifecycle に紐付ける
   - 一方のジョブが完了しても、他方の Lifecycle の状態は変化しない
   - 状態遷移は完全に独立して動作する

4. _save() のファイル書き込み安全性 (シナリオ D):
   - asyncio 単一スレッドで _save() は完全同期（await ポイントなし）のため、
     割り込みが発生せずレースコンディションは起きない
   - 状態を交互に更新しても、全件が正しく保存・読み戻しできる

■ 結論

現在の asyncio 単一スレッドアーキテクチャでは:
- LifecycleManager の状態管理（dict 操作 + _save()）は安全
- ファイルロック未使用でも、asyncio の協調マルチタスクモデルにより
  同期的な dict 操作/ファイル書き込みは割り込まれない
- ただし _dispatch_internal() のプロジェクト排他チェックには
  asyncio.create_task の非即時実行に起因するレースコンディションがある:
  同一プロジェクトに対して asyncio.gather 等で同時にディスパッチすると、
  running_project_ids() チェックが期待通り機能しない。
  修正案: _dispatch_internal() 内でジョブの status を create_task 前に
  "running" に設定するか、asyncio.Lock を導入する。
- 将来的にマルチプロセス/スレッドに拡張する場合は
  ファイルロックや asyncio.Lock の追加導入が必要になる
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from dispatcher.models import Lifecycle, SOCKET_DIR_NAME, now_iso


def _make_project(project_id: str, working_dir: str) -> dict:
    return {
        "name": project_id,
        "description": f"Test project {project_id}",
        "working_directory": working_dir,
        "sandbox_profile": "default",
        "orchestration": {
            "auto_approve": True,
            "require_first_approval": False,
        },
    }


def _mock_sandbox_exec(projects: dict[str, dict]):
    """sandbox_exec のモックを構成する。"""
    def mock_load_project(project_id, repo_dir=None):
        return projects.get(project_id)

    def mock_resolve_params(project, **kwargs):
        return ("default", [], [], [])

    def mock_build_exec_args(**kwargs):
        return ["echo", "test"]

    def mock_resolve_profile(name):
        return {"profile_id": name, "proxy_profile": None}

    def mock_uses_network_protection(profile):
        return False

    return patch.multiple(
        "sandbox_exec",
        load_project=mock_load_project,
        resolve_project_sandbox_params=mock_resolve_params,
        build_exec_args=mock_build_exec_args,
        resolve_profile=mock_resolve_profile,
        uses_network_protection=mock_uses_network_protection,
    )


def _create_lifecycle(server, project_id: str, lifecycle_id: str, prompt: str = "test prompt"):
    """Lifecycle を作成し、コンテキスト YAML を初期化する。"""
    mgr = server.lifecycle_mgr
    lc = Lifecycle(
        lifecycle_id=lifecycle_id,
        project_id=project_id,
        prompt=prompt,
        max_runs=5,
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    mgr.lifecycles[lc.lifecycle_id] = lc

    context_data = {
        "meta": {
            "task_id": "",
            "remote_id": "",
            "datasource_id": "",
            "project_id": project_id,
            "generation": 1,
        },
        "description": prompt,
        "preconditions": [],
        "acceptance_criteria": ["テストが通ること"],
        "open_questions": [],
        "completion_actions": [],
        "phases": [],
        "execute_prompt": "",
        "previous_generations": [],
        "feedback": [],
    }
    mgr._init_context_yaml(lc, context_data)
    mgr._save()
    return lc


def _read_lifecycles_jsonl(runtime_dir: Path) -> list[dict]:
    """lifecycles.jsonl を読み込んで dict のリストを返す。"""
    path = runtime_dir / SOCKET_DIR_NAME / "lifecycles.jsonl"
    if not path.exists():
        return []
    result = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                result.append(json.loads(line))
    return result


def _read_jobs_jsonl(runtime_dir: Path) -> list[dict]:
    """jobs.jsonl を読み込んで dict のリストを返す。"""
    path = runtime_dir / SOCKET_DIR_NAME / "jobs.jsonl"
    if not path.exists():
        return []
    result = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                result.append(json.loads(line))
    return result


# =========================================================================
# シナリオ A: 同一プロジェクトの2つの Lifecycle 同時ディスパッチ
# =========================================================================


@pytest.mark.asyncio
async def test_same_project_concurrent_dispatch_race_condition(stub_server):
    """同一プロジェクトの2つの Lifecycle を asyncio.gather で同時にディスパッチする。

    【発見】_dispatch_internal() は asyncio.create_task(execute_job(job)) でジョブを
    スケジュールするが、execute_job が実際に実行されるまでジョブの status は "queued" のまま。
    そのため、asyncio.gather で2つの dispatch_plan を同時に呼ぶと、
    running_project_ids() チェックをすり抜けて両方のジョブが running になる。

    このテストはこのレースコンディションの存在を検証する。
    """
    server, project = stub_server
    runtime_dir = server.runtime_dir
    project_id = "proj-a"

    projects = {project_id: project}

    with _mock_sandbox_exec(projects):
        lc1 = _create_lifecycle(server, project_id, "lc-1", "task 1")
        lc2 = _create_lifecycle(server, project_id, "lc-2", "task 2")

        await asyncio.gather(
            server.lifecycle_mgr.dispatch_plan(lc1),
            server.lifecycle_mgr.dispatch_plan(lc2),
        )

        # asyncio.create_task で投入された execute_job を開始させる
        await asyncio.sleep(0)

    # --- 検証 ---

    # 両方の Lifecycle が登録されている
    assert "lc-1" in server.lifecycle_mgr.lifecycles
    assert "lc-2" in server.lifecycle_mgr.lifecycles

    # 【レースコンディション】同一プロジェクトにもかかわらず
    # 両方のジョブが running になっている
    running_jobs = [j for j in server.jobs.values() if j.status == "running"]
    assert len(running_jobs) == 2, \
        "Race condition: both jobs should be running due to create_task scheduling delay"

    # キューは空（排他チェックをすり抜けたため）
    assert len(server.queue) == 0

    # lifecycles.jsonl に両方保存されている
    lc_data = _read_lifecycles_jsonl(runtime_dir)
    lc_ids = {d["lifecycle_id"] for d in lc_data}
    assert "lc-1" in lc_ids
    assert "lc-2" in lc_ids

    # 各 Lifecycle の context.yaml が独立して存在する
    assert lc1.context_path and Path(lc1.context_path).exists()
    assert lc2.context_path and Path(lc2.context_path).exists()
    assert lc1.context_path != lc2.context_path

    ctx1 = yaml.safe_load(Path(lc1.context_path).read_text())
    ctx2 = yaml.safe_load(Path(lc2.context_path).read_text())
    assert ctx1["description"] == "task 1"
    assert ctx2["description"] == "task 2"


@pytest.mark.asyncio
async def test_same_project_sequential_dispatch_exclusion(stub_server):
    """同一プロジェクトに対して順次ディスパッチした場合、
    先行ジョブが running であれば後続ジョブは queued になることを確認する。

    asyncio.create_task 後に await asyncio.sleep(0) を挟むことで
    execute_job が開始される（status が "running" になる）のを待つ。
    """
    server, project = stub_server
    project_id = "proj-seq"

    projects = {project_id: project}

    with _mock_sandbox_exec(projects):
        lc1 = _create_lifecycle(server, project_id, "lc-s1", "task seq1")
        lc2 = _create_lifecycle(server, project_id, "lc-s2", "task seq2")

        # 1つ目をディスパッチ
        await server.lifecycle_mgr.dispatch_plan(lc1)
        # create_task された execute_job を走らせる
        await asyncio.sleep(0)

        # 2つ目をディスパッチ
        await server.lifecycle_mgr.dispatch_plan(lc2)

    # --- 検証 ---

    running_jobs = [j for j in server.jobs.values() if j.status == "running"]
    queued_jobs = list(server.queue)

    # 順次ディスパッチなら排他チェックが正しく機能する
    assert len(running_jobs) == 1, f"Expected 1 running job, got {len(running_jobs)}"
    assert len(queued_jobs) == 1, f"Expected 1 queued job, got {len(queued_jobs)}"
    assert running_jobs[0].lifecycle_id == "lc-s1"
    assert queued_jobs[0].lifecycle_id == "lc-s2"


# =========================================================================
# シナリオ B: 異なるプロジェクトの2つの Lifecycle 同時ディスパッチ
# =========================================================================


@pytest.mark.asyncio
async def test_different_project_concurrent_dispatch(stub_server):
    """異なるプロジェクトの2つの Lifecycle を同時にディスパッチする。

    期待:
    - スロット数が十分なら両方のジョブが running になる
    - lifecycles.jsonl に両方保存される
    - jobs.jsonl に両方保存される
    """
    server, project_template = stub_server
    runtime_dir = server.runtime_dir

    project_a = _make_project("proj-a", project_template["working_directory"])
    project_b = _make_project("proj-b", project_template["working_directory"])

    projects = {"proj-a": project_a, "proj-b": project_b}

    with _mock_sandbox_exec(projects):
        lc1 = _create_lifecycle(server, "proj-a", "lc-1", "task A")
        lc2 = _create_lifecycle(server, "proj-b", "lc-2", "task B")

        await asyncio.gather(
            server.lifecycle_mgr.dispatch_plan(lc1),
            server.lifecycle_mgr.dispatch_plan(lc2),
        )

    # --- 検証 ---

    # 両方のジョブが running（異なるプロジェクトのため排他なし）
    running_jobs = [j for j in server.jobs.values() if j.status == "running"]
    assert len(running_jobs) == 2, f"Expected 2 running jobs, got {len(running_jobs)}"

    running_project_ids = {j.project_id for j in running_jobs}
    assert "proj-a" in running_project_ids
    assert "proj-b" in running_project_ids

    # lifecycles.jsonl に両方保存
    lc_data = _read_lifecycles_jsonl(runtime_dir)
    lc_ids = {d["lifecycle_id"] for d in lc_data}
    assert "lc-1" in lc_ids
    assert "lc-2" in lc_ids

    # jobs.jsonl に両方保存
    job_data = _read_jobs_jsonl(runtime_dir)
    assert len(job_data) == 2
    job_projects = {d["project_id"] for d in job_data}
    assert "proj-a" in job_projects
    assert "proj-b" in job_projects


# =========================================================================
# シナリオ C: Lifecycle 状態遷移の独立性
# =========================================================================


@pytest.mark.asyncio
async def test_lifecycle_state_transition_independence(stub_server):
    """2つの Lifecycle を作成し、一方の plan ジョブを完了させたとき、
    他方の Lifecycle の状態が変わらないことを確認する。
    """
    server, project_template = stub_server

    project_a = _make_project("proj-a", project_template["working_directory"])
    project_b = _make_project("proj-b", project_template["working_directory"])

    projects = {"proj-a": project_a, "proj-b": project_b}

    with _mock_sandbox_exec(projects):
        lc1 = _create_lifecycle(server, "proj-a", "lc-1", "task A")
        lc2 = _create_lifecycle(server, "proj-b", "lc-2", "task B")

        # 両方ディスパッチ
        await asyncio.gather(
            server.lifecycle_mgr.dispatch_plan(lc1),
            server.lifecycle_mgr.dispatch_plan(lc2),
        )

        # create_task で投入された execute_job を開始させる
        await asyncio.sleep(0)

        dispatch_id_1 = lc1.current_dispatch_id
        dispatch_id_2 = lc2.current_dispatch_id

        assert dispatch_id_1 is not None
        assert dispatch_id_2 is not None
        assert dispatch_id_1 != dispatch_id_2

        # lc1 のジョブに対して plan 結果（planned）を書き出す
        server.write_result(dispatch_id_1, {
            "next_status": "planned",
            "phases": [
                {"goal": "Phase 1", "criteria": "Criteria 1"},
            ],
        })

        # lc1 のジョブを完了させる
        await server.complete_job(dispatch_id_1, exit_code=0)
        # on_job_complete + drain_queue の非同期処理を待つ
        await asyncio.sleep(0.1)

    # --- 検証 ---

    # lc1: plan 完了 → planned → auto_approve → phase_executing
    lc1_status = server.lifecycle_mgr.lifecycles["lc-1"].status
    assert lc1_status in ("planned", "phase_executing"), \
        f"lc-1 should be planned or phase_executing, got {lc1_status}"

    # lc1 のフェーズが設定されている
    assert len(server.lifecycle_mgr.lifecycles["lc-1"].phases) == 1
    assert server.lifecycle_mgr.lifecycles["lc-1"].phases[0]["goal"] == "Phase 1"

    # lc2: 何も完了していないので planning のまま
    lc2_status = server.lifecycle_mgr.lifecycles["lc-2"].status
    assert lc2_status == "planning", \
        f"lc-2 should still be planning, got {lc2_status}"


@pytest.mark.asyncio
async def test_on_job_complete_targets_correct_lifecycle(stub_server):
    """on_job_complete が lifecycle_id を正しく使って
    対象の Lifecycle のみに状態遷移を適用することをより詳細に検証する。
    """
    server, project_template = stub_server

    project_a = _make_project("proj-a", project_template["working_directory"])
    project_b = _make_project("proj-b", project_template["working_directory"])

    projects = {"proj-a": project_a, "proj-b": project_b}

    with _mock_sandbox_exec(projects):
        lc1 = _create_lifecycle(server, "proj-a", "lc-10", "task 10")
        _create_lifecycle(server, "proj-b", "lc-20", "task 20")
        _create_lifecycle(server, "proj-b", "lc-30", "task 30")

        # lc1 のみディスパッチ（異なるプロジェクトを使う）
        await server.lifecycle_mgr.dispatch_plan(lc1)
        # create_task で投入された execute_job を開始させる
        await asyncio.sleep(0)

        dispatch_id_1 = lc1.current_dispatch_id

        # lc1 の plan 完了
        server.write_result(dispatch_id_1, {
            "next_status": "needs_input",
        })
        await server.complete_job(dispatch_id_1, exit_code=0)
        await asyncio.sleep(0.1)

    # --- 検証 ---

    # lc1: needs_input → done（新仕様: Lifecycle は終了し Generation に記録）
    assert server.lifecycle_mgr.lifecycles["lc-10"].status == "done"

    # lc2, lc3: まだ planning のまま（dispatch もされていない）
    assert server.lifecycle_mgr.lifecycles["lc-20"].status == "planning"
    assert server.lifecycle_mgr.lifecycles["lc-30"].status == "planning"


# =========================================================================
# シナリオ D: _save() のファイル書き込み安全性
# =========================================================================


@pytest.mark.asyncio
async def test_save_file_write_safety(stub_server):
    """_save() の同期的なファイル書き込みが asyncio 単一スレッドで安全であることを検証する。

    複数の Lifecycle の状態を交互に更新し、
    毎回 _save() 後に lifecycles.jsonl を読み戻して全件存在を確認する。
    """
    server, _ = stub_server
    runtime_dir = server.runtime_dir
    mgr = server.lifecycle_mgr

    # 5つの Lifecycle を作成
    for i in range(5):
        lc = Lifecycle(
            lifecycle_id=f"lc-{i}",
            project_id=f"proj-{i}",
            prompt=f"task {i}",
            status="planning",
            max_runs=5,
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        mgr.lifecycles[lc.lifecycle_id] = lc

    mgr._save()

    statuses = ["planned", "phase_executing", "phase_evaluating", "done"]

    for status in statuses:
        for i in range(5):
            lc = mgr.lifecycles[f"lc-{i}"]
            lc.status = status
            lc.updated_at = now_iso()
            mgr._save()

            saved_data = _read_lifecycles_jsonl(runtime_dir)
            saved_ids = {d["lifecycle_id"] for d in saved_data}

            assert len(saved_data) == 5, \
                f"Expected 5 lifecycles after updating lc-{i} to {status}, got {len(saved_data)}"
            for j in range(5):
                assert f"lc-{j}" in saved_ids, \
                    f"lc-{j} missing after updating lc-{i} to {status}"

    final_data = _read_lifecycles_jsonl(runtime_dir)
    for d in final_data:
        assert d["status"] == "done"


@pytest.mark.asyncio
async def test_save_concurrent_interleaved_updates(stub_server):
    """asyncio.gather で複数のコルーチンが交互に _save() を呼ぶ場合でも
    データが失われないことを確認する。

    _save() 自体には await がないため、各 _save() は原子的に実行される。
    """
    server, _ = stub_server
    runtime_dir = server.runtime_dir
    mgr = server.lifecycle_mgr

    for i in range(3):
        lc = Lifecycle(
            lifecycle_id=f"lc-{i}",
            project_id=f"proj-{i}",
            prompt=f"task {i}",
            status="planning",
            max_runs=5,
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        mgr.lifecycles[lc.lifecycle_id] = lc
    mgr._save()

    async def update_lifecycle(lc_id: str, target_status: str):
        for status in ["planned", "phase_executing", target_status]:
            mgr.lifecycles[lc_id].status = status
            mgr._save()
            await asyncio.sleep(0)  # 他のコルーチンに制御を渡す

    await asyncio.gather(
        update_lifecycle("lc-0", "done"),
        update_lifecycle("lc-1", "phase_evaluating"),
        update_lifecycle("lc-2", "suspend"),
    )

    final_data = _read_lifecycles_jsonl(runtime_dir)
    assert len(final_data) == 3

    final_statuses = {d["lifecycle_id"]: d["status"] for d in final_data}
    assert final_statuses["lc-0"] == "done"
    assert final_statuses["lc-1"] == "phase_evaluating"
    assert final_statuses["lc-2"] == "suspend"


# =========================================================================
# 追加: キュー解放の検証
# =========================================================================


@pytest.mark.asyncio
async def test_queued_job_starts_after_running_job_completes(stub_server):
    """同一プロジェクトで queued になったジョブが、
    先行ジョブの完了後に drain_queue で実行開始されることを確認する。

    排他制御が正しく機能するよう、順次ディスパッチ + await asyncio.sleep(0)
    で execute_job の開始を待ってからテストする。
    """
    server, project_template = stub_server

    project = _make_project("proj-q", project_template["working_directory"])
    projects = {"proj-q": project}

    with _mock_sandbox_exec(projects):
        lc1 = _create_lifecycle(server, "proj-q", "lc-q1", "task Q1")
        lc2 = _create_lifecycle(server, "proj-q", "lc-q2", "task Q2")

        # 1つ目をディスパッチし、execute_job が開始されるのを待つ
        await server.lifecycle_mgr.dispatch_plan(lc1)
        await asyncio.sleep(0)

        # 2つ目をディスパッチ（排他チェックが機能して queued になるはず）
        await server.lifecycle_mgr.dispatch_plan(lc2)

        # 1つ running, 1つ queued を確認
        running_before = [j for j in server.jobs.values() if j.status == "running"]
        queued_before = list(server.queue)
        assert len(running_before) == 1
        assert len(queued_before) == 1

        first_dispatch_id = running_before[0].dispatch_id

        # 先行ジョブの結果を書き出して完了させる
        server.write_result(first_dispatch_id, {
            "next_status": "planned",
            "phases": [{"goal": "G1", "criteria": "C1"}],
        })
        await server.complete_job(first_dispatch_id, exit_code=0)
        await asyncio.sleep(0.1)

        # drain_queue が走ってキューのジョブが running になる
        # (先行ジョブの完了後、auto_approve で execute ジョブも投入される可能性がある)
        assert len(server.queue) == 0 or all(
            j.project_id != "proj-q" or j.status != "queued"
            for j in server.queue
        ), "Queued job for proj-q should have been drained"
