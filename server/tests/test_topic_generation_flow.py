"""原创选题工作流的端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app
from conftest import real_video_bytes


def test_completed_analysis_can_generate_and_select_original_topic() -> None:
    """选题必须以成功分析为前提，确认行为由用户接口触发。"""

    with TestClient(app) as client:
        project_id = client.post("/api/v1/projects", json={"title": "选题测试", "description": "悬疑情感"}).json()["id"]
        client.post(
            f"/api/v1/projects/{project_id}/source-video",
            files={"file": ("reference.mp4", real_video_bytes(), "video/mp4")},
        )
        analysis = client.post(f"/api/v1/projects/{project_id}/analysis-runs")
        assert analysis.status_code == 202

        topic_run = client.post(f"/api/v1/projects/{project_id}/topic-generation-runs")
        assert topic_run.status_code == 202
        assert topic_run.json()["workflow_key"] == "topic_generation"

        candidates_response = client.get(f"/api/v1/projects/{project_id}/topic-candidates")
        candidates = candidates_response.json()
        assert candidates_response.status_code == 200
        assert len(candidates) == 3
        assert all(candidate["status"] == "DRAFT" for candidate in candidates)

        selected_response = client.post(f"/api/v1/topic-candidates/{candidates[0]['id']}/select")
        assert selected_response.status_code == 200
        assert selected_response.json()["status"] == "SELECTED"
