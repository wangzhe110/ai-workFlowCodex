"""无密钥 mock_v1 Adapter 的完整生产闭环测试。"""

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.main import app
from app.models import ModelInvocation
from conftest import real_video_bytes


def _run(client: TestClient, project_id: str, run_key: str) -> None:
    """TestClient 会等待 BackgroundTask；随后由接口读取结果验证真实状态而非返回初态。"""

    response = client.post(f"/api/v1/production/projects/{project_id}/generation-runs/{run_key}")
    assert response.status_code == 202, response.text


def test_mock_v1_adapter_runs_full_reviewed_production_closure() -> None:
    """从上传参考视频到审核成片，全程只能经正式生成和审核节点推进。"""

    with TestClient(app) as client:
        project_id = client.post("/api/v1/projects", json={"title": "V1 本地闭环"}).json()["id"]
        uploaded = client.post(
            f"/api/v1/projects/{project_id}/source-video",
            files={"file": ("reference.mp4", real_video_bytes(), "video/mp4")},
        )
        assert uploaded.status_code == 201

        _run(client, project_id, "reference_analysis")
        assert client.get(f"/api/v1/production/projects/{project_id}/state").json()["active_stage"] == "ANALYSIS_REVIEW"
        analysis = client.get(f"/api/v1/production/projects/{project_id}/reference-analyses").json()[0]
        assert analysis["generation_status"] == "SUCCEEDED"
        assert client.post(f"/api/v1/production/reference-analyses/{analysis['id']}/lock", json={"reviewer_label": "测试制作人"}).status_code == 200

        _run(client, project_id, "story_generation")
        proposals = client.get(f"/api/v1/production/projects/{project_id}/story-proposals").json()
        assert len(proposals) == 3
        assert client.post(f"/api/v1/production/story-proposals/{proposals[0]['id']}/select", json={}).status_code == 200

        _run(client, project_id, "character_design")
        _run(client, project_id, "character_images")
        character_images = client.get(f"/api/v1/production/projects/{project_id}/character-reference-images").json()
        assert len(character_images) == 2
        for image in character_images:
            assert client.post(f"/api/v1/production/character-reference-images/{image['id']}/lock", json={}).status_code == 200
        assert client.get(f"/api/v1/production/projects/{project_id}/state").json()["active_stage"] == "SCENE_ASSETS"

        _run(client, project_id, "scene_design")
        _run(client, project_id, "scene_images")
        scene_images = client.get(f"/api/v1/production/projects/{project_id}/scene-reference-images").json()
        assert len(scene_images) == 2
        for image in scene_images:
            assert client.post(f"/api/v1/production/scene-reference-images/{image['id']}/lock", json={}).status_code == 200

        _run(client, project_id, "director_plan")
        _run(client, project_id, "shot_keyframes")
        frames = client.get(f"/api/v1/production/projects/{project_id}/shot-keyframes").json()
        assert len(frames) == 3
        for frame in frames:
            assert client.post(f"/api/v1/production/shot-keyframes/{frame['id']}/lock", json={}).status_code == 200

        _run(client, project_id, "video_generation")
        clips = client.get(f"/api/v1/production/projects/{project_id}/video-clips").json()
        assert len(clips) == 3
        for clip in clips:
            assert client.post(f"/api/v1/production/video-clips/{clip['id']}/approve", json={}).status_code == 200

        _run(client, project_id, "final_compose")
        state = client.get(f"/api/v1/production/projects/{project_id}/state").json()
        assert state["active_stage"] == "COMPLETED"

        db = SessionLocal()
        try:
            invocations = db.query(ModelInvocation).filter(ModelInvocation.project_id == project_id).all()
            assert len(invocations) >= 12
            assert all(item.prompt_template_id and item.status.value == "SUCCEEDED" for item in invocations)
        finally:
            db.close()

        # 制作人可通过安全追溯接口确认每次生成冻结的工作流、模型与 Prompt 版本，
        # 但接口不返回原始模型输入或输出，避免参考素材内容泄露到普通页面。
        traces = client.get(f"/api/v1/production/projects/{project_id}/model-invocations")
        assert traces.status_code == 200, traces.text
        assert len(traces.json()) >= 12
        first_trace = traces.json()[0]
        assert first_trace["workflow_version"] == "LemonFlow_V1"
        assert first_trace["model_display_name"]
        assert first_trace["prompt_name"]
        assert "input_snapshot" not in first_trace
        assert "output_reference" not in first_trace
