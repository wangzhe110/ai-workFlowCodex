"""V1 真实生产前的版本选择、冻结与幂等回归测试。

这些测试刻意只使用 ``mock_v1`` Adapter：它们验证数据库状态机和供应商调用边界，
不会读取真实 API Key 或产生任何付费调用。
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import Optional

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import SessionLocal
from app.main import app
from app.models import (
    MediaAsset,
    FinalVideo,
    ModelInvocation,
    ModelProfile,
    ModelSlotProfileBinding,
    PromptTemplate,
    RunStatus,
    VideoReviewStatus,
    WorkflowRun,
    WorkflowStep,
    VideoClip,
)
from app.services.v1_execution_service import create_v1_run, execute_v1_workflow
from app.services.v1_execution_service import execute_v1_video_child
from app.services.analysis_provider import VideoTaskResult
from conftest import real_video_bytes


def _upload_project(client: TestClient, title: str = "V1 完整性") -> str:
    project_id = client.post("/api/v1/projects", json={"title": title}).json()["id"]
    response = client.post(
        f"/api/v1/projects/{project_id}/source-video",
        files={"file": ("reference.mp4", real_video_bytes(), "video/mp4")},
    )
    assert response.status_code == 201, response.text
    return project_id


def _run(client: TestClient, project_id: str, key: str, payload: Optional[dict] = None) -> dict:
    response = client.post(f"/api/v1/production/projects/{project_id}/generation-runs/{key}", json=payload or {})
    assert response.status_code == 202, response.text
    return response.json()


def _advance_to_video_review(client: TestClient) -> str:
    """跑到视频审核节点，但不通过视频，供版本重做测试使用。"""

    project_id = _upload_project(client, "视频版本重做")
    _run(client, project_id, "reference_analysis")
    analysis = client.get(f"/api/v1/production/projects/{project_id}/reference-analyses").json()[0]
    assert client.post(f"/api/v1/production/reference-analyses/{analysis['id']}/lock", json={}).status_code == 200
    _run(client, project_id, "story_generation")
    story = client.get(f"/api/v1/production/projects/{project_id}/story-proposals").json()[0]
    assert client.post(f"/api/v1/production/story-proposals/{story['id']}/select", json={}).status_code == 200
    _run(client, project_id, "character_design")
    _run(client, project_id, "character_images")
    for item in client.get(f"/api/v1/production/projects/{project_id}/character-reference-images").json():
        assert client.post(f"/api/v1/production/character-reference-images/{item['id']}/lock", json={}).status_code == 200
    _run(client, project_id, "scene_design")
    _run(client, project_id, "scene_images")
    for item in client.get(f"/api/v1/production/projects/{project_id}/scene-reference-images").json():
        assert client.post(f"/api/v1/production/scene-reference-images/{item['id']}/lock", json={}).status_code == 200
    _run(client, project_id, "director_plan")
    _run(client, project_id, "shot_keyframes")
    for item in client.get(f"/api/v1/production/projects/{project_id}/shot-keyframes").json():
        assert client.post(f"/api/v1/production/shot-keyframes/{item['id']}/lock", json={}).status_code == 200
    _run(client, project_id, "video_generation")
    assert client.get(f"/api/v1/production/projects/{project_id}/state").json()["active_stage"] == "VIDEO_REVIEW"
    return project_id


def test_rejected_video_version_is_not_used_by_review_gate_or_final_export() -> None:
    """驳回第 2 镜并重做后，FINAL_EXPORT 只冻结三个当前采用版本。"""

    with TestClient(app) as client:
        project_id = _advance_to_video_review(client)
        first = client.get(f"/api/v1/production/projects/{project_id}/video-clips").json()
        assert len(first) == 3
        first_by_shot = {item["shot_number"]: item for item in first}
        # 先采用第 1、3 镜，驳回第 2 镜。旧版本必须继续存在但不再成为当前版本。
        assert client.post(f"/api/v1/production/video-clips/{first_by_shot[1]['id']}/approve", json={}).status_code == 200
        assert client.post(f"/api/v1/production/video-clips/{first_by_shot[3]['id']}/approve", json={}).status_code == 200
        rejected = client.post(f"/api/v1/production/video-clips/{first_by_shot[2]['id']}/reject", json={})
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["review_status"] == VideoReviewStatus.REJECTED.value
        assert client.get(f"/api/v1/production/projects/{project_id}/state").json()["active_stage"] == "VIDEO_GENERATION"

        rerun = _run(client, project_id, "video_generation", {"shot_plan_ids": [first_by_shot[2]["shot_plan_id"]]})
        child_steps = [step for step in rerun["steps"] if step["step_key"] == "VIDEO_SHOT"]
        assert len(child_steps) == 1
        all_versions = client.get(f"/api/v1/production/projects/{project_id}/video-clips").json()
        assert len(all_versions) == 4
        replacement = next(item for item in all_versions if item["shot_number"] == 2 and item["version"] == 2)
        assert replacement["task_status"] == "SUCCEEDED"
        assert client.post(f"/api/v1/production/video-clips/{replacement['id']}/approve", json={}).status_code == 200
        assert client.get(f"/api/v1/production/projects/{project_id}/state").json()["active_stage"] == "FINAL_EXPORT"

        _run(client, project_id, "final_compose")
        db = SessionLocal()
        try:
            final = db.execute(select(FinalVideo).where(FinalVideo.project_id == project_id)).scalar_one()
            assert set(final.clip_ids) == {
                first_by_shot[1]["id"], replacement["id"], first_by_shot[3]["id"],
            }
            assert first_by_shot[2]["id"] not in final.clip_ids
        finally:
            db.close()


def test_workflow_creation_freezes_source_model_prompt_and_story_order() -> None:
    """修改中心配置或再上传素材，都不能改变已创建 WorkflowRun 的执行输入。"""

    with TestClient(app) as client:
        project_id = _upload_project(client, "冻结快照")
        db = SessionLocal()
        try:
            run = create_v1_run(db, project_id=project_id, run_key="reference_analysis")
            source_id = run.input_snapshot["context"]["source_asset_id"]
            model_before = run.input_snapshot["model_bindings"]["VIDEO_ANALYSIS"][0]["profile_snapshot"]["display_name"]
            prompt_before = run.input_snapshot["prompt_templates"]["VIDEO_ANALYSIS"]["content"]
            profile_id = run.input_snapshot["model_bindings"]["VIDEO_ANALYSIS"][0]["model_profile_id"]
            prompt_id = run.input_snapshot["prompt_templates"]["VIDEO_ANALYSIS"]["id"]
            db.get(ModelProfile, profile_id).display_name = "changed-after-run-created"
            db.get(PromptTemplate, prompt_id).content = "changed-after-run-created"
            db.commit()
        finally:
            db.close()
        second_upload = client.post(
            f"/api/v1/projects/{project_id}/source-video",
            files={"file": ("newer.mp4", real_video_bytes(), "video/mp4")},
        )
        assert second_upload.status_code == 201
        execute_v1_workflow(run.id)
        db = SessionLocal()
        try:
            invocation = db.execute(select(ModelInvocation).where(ModelInvocation.workflow_run_id == run.id)).scalar_one()
            assert invocation.input_snapshot["source_asset_id"] == source_id
            assert invocation.model_profile_snapshot["display_name"] == model_before
            assert invocation.prompt_snapshot["content"] == prompt_before
            assert db.get(MediaAsset, source_id) is not None
        finally:
            db.close()

        # 继续到故事生成，创建任务后颠倒当前绑定优先级并修改活动 Prompt；执行仍须
        # 保持创建时的三模型顺序和 Prompt 快照。
        analysis = client.get(f"/api/v1/production/projects/{project_id}/reference-analyses").json()[0]
        assert client.post(f"/api/v1/production/reference-analyses/{analysis['id']}/lock", json={}).status_code == 200
        db = SessionLocal()
        try:
            story_run = create_v1_run(db, project_id=project_id, run_key="story_generation")
            frozen = story_run.input_snapshot["model_bindings"]["STORY_GENERATE"]
            frozen_ids = [item["model_profile_id"] for item in frozen]
            frozen_prompt = story_run.input_snapshot["prompt_templates"]["STORY_GENERATE"]["content"]
            bindings = db.execute(
                select(ModelSlotProfileBinding).where(ModelSlotProfileBinding.slot_id == frozen[0]["slot_id"])
            ).scalars().all()
            for position, binding in enumerate(reversed(bindings), start=1):
                binding.priority = position
            db.get(PromptTemplate, story_run.input_snapshot["prompt_templates"]["STORY_GENERATE"]["id"]).content = "new active prompt"
            db.commit()
        finally:
            db.close()
        execute_v1_workflow(story_run.id)
        db = SessionLocal()
        try:
            invocations = db.execute(
                select(ModelInvocation)
                .where(ModelInvocation.workflow_run_id == story_run.id)
                .order_by(ModelInvocation.created_at, ModelInvocation.id)
            ).scalars().all()
            assert [item.model_profile_id for item in invocations] == frozen_ids
            assert all(item.prompt_snapshot["content"] == frozen_prompt for item in invocations)
        finally:
            db.close()


def test_duplicate_and_concurrent_requests_return_one_active_v1_run() -> None:
    """重复点击和并发网络重试只能得到同一个 PENDING 运行，不产生第二个收费任务。"""

    with TestClient(app) as client:
        project_id = _upload_project(client, "幂等保护")

        def create_once() -> str:
            db = SessionLocal()
            try:
                return create_v1_run(db, project_id=project_id, run_key="reference_analysis").id
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            run_ids = list(pool.map(lambda _: create_once(), range(2)))
        assert len(set(run_ids)) == 1
        db = SessionLocal()
        try:
            runs = db.execute(
                select(WorkflowRun).where(
                    WorkflowRun.project_id == project_id,
                    WorkflowRun.workflow_key == "v1_reference_analysis",
                    WorkflowRun.status.in_((RunStatus.PENDING, RunStatus.RUNNING)),
                )
            ).scalars().all()
            assert len(runs) == 1
            assert len(list(db.execute(select(WorkflowStep).where(WorkflowStep.workflow_run_id == runs[0].id)).scalars())) == 1
        finally:
            db.close()


def test_video_child_restart_polls_existing_provider_task_without_resubmit(monkeypatch) -> None:
    """Worker 中断后已有 provider_task_id 的镜头只能轮询恢复，不能再次提交。"""

    class FakeProvider:
        submits = 0

        def submit(self, _request):
            self.submits += 1
            return VideoTaskResult(provider_task_id="provider-task-0001", status="PENDING")

        def poll(self, task_id: str):
            assert task_id == "provider-task-0001"
            return VideoTaskResult(provider_task_id=task_id, status="SUCCEEDED", video_url="https://example.invalid/clip.mp4")

    provider = FakeProvider()
    with TestClient(app) as client:
        project_id = _advance_to_video_review(client)
        old_second = next(item for item in client.get(f"/api/v1/production/projects/{project_id}/video-clips").json() if item["shot_number"] == 2)
        assert client.post(f"/api/v1/production/video-clips/{old_second['id']}/reject", json={}).status_code == 200
        db = SessionLocal()
        try:
            run = create_v1_run(db, project_id=project_id, run_key="video_generation", shot_plan_ids=[old_second["shot_plan_id"]])
            snapshot = deepcopy(run.input_snapshot)
            snapshot["model_bindings"]["VIDEO_GENERATE"][0]["profile_snapshot"]["adapter_key"] = "test_async"
            run.input_snapshot = snapshot
            child = next(step for step in run.steps if step.step_key == "VIDEO_SHOT")
            child_payload = deepcopy(child.input_payload)
            child_payload["binding"]["profile_snapshot"]["adapter_key"] = "test_async"
            child.input_payload = child_payload
            db.commit()
        finally:
            db.close()

        monkeypatch.setattr("app.services.v1_execution_service.video_provider", lambda _snapshot: provider)
        calls = {"count": 0}

        def interrupted_wait(_provider, _snapshot, first_result):
            calls["count"] += 1
            if calls["count"] == 1:
                # 模拟进程被外部终止：不会落失败终态，数据库中只保留供应商任务号。
                raise KeyboardInterrupt("worker interrupted")
            return first_result

        monkeypatch.setattr("app.services.v1_execution_service.wait_for_video_result", interrupted_wait)
        try:
            execute_v1_video_child(run.id, child.id)
        except KeyboardInterrupt:
            pass
        db = SessionLocal()
        try:
            interrupted = db.get(WorkflowStep, child.id)
            assert interrupted.status == RunStatus.RUNNING
            assert interrupted.provider_task_id == "provider-task-0001"
        finally:
            db.close()

        execute_v1_video_child(run.id, child.id)
        db = SessionLocal()
        try:
            completed = db.get(WorkflowStep, child.id)
            clip = db.get(VideoClip, completed.video_clip_id)
            assert provider.submits == 1
            assert completed.status == RunStatus.SUCCEEDED
            assert clip.generation_status == RunStatus.SUCCEEDED.value
            invocation = db.execute(select(ModelInvocation).where(ModelInvocation.workflow_step_id == completed.id)).scalar_one()
            assert completed.idempotency_key == clip.idempotency_key == invocation.idempotency_key
        finally:
            db.close()
