"""Day 1 最小业务闭环测试。"""

from fastapi.testclient import TestClient

from app.main import app
from conftest import real_video_bytes


def test_reference_video_can_be_analyzed_into_abstract_mechanisms() -> None:
    """验证视频不会被直接复刻，输出包含抽象分析和合规提示。"""

    with TestClient(app) as client:
        project_response = client.post(
            "/api/v1/projects",
            json={"title": "测试原创短剧", "description": "用于验证 Day 1 闭环"},
        )
        assert project_response.status_code == 201
        project_id = project_response.json()["id"]

        upload_response = client.post(
            f"/api/v1/projects/{project_id}/source-video",
            files={"file": ("reference.mp4", real_video_bytes(), "video/mp4")},
        )
        assert upload_response.status_code == 201

        run_response = client.post(f"/api/v1/projects/{project_id}/analysis-runs")
        assert run_response.status_code == 202
        run_id = run_response.json()["id"]

        final_response = client.get(f"/api/v1/workflow-runs/{run_id}")
        assert final_response.status_code == 200
        run = final_response.json()
        assert run["status"] == "SUCCEEDED"
        assert [step["step_key"] for step in run["steps"]] == [
            "transcribe_reference_audio",
            "analyze_reference_mechanisms",
        ]
        assert all(step["progress"] == 100 for step in run["steps"])
        assert run["steps"][0]["output_payload"]["transcript_persisted"] is False
        assert "compliance_note" in run["steps"][1]["output_payload"]
