"""V1 模型质量报表的审核评分、采用率与只读统计边界测试。"""

from fastapi.testclient import TestClient

from app.main import app
from conftest import real_video_bytes


def test_quality_report_uses_review_score_without_auto_switching_models() -> None:
    """评分进入快照后只可查看，刷新报表不会修改模型槽位或重跑模型。"""

    with TestClient(app) as client:
        project_id = client.post("/api/v1/projects", json={"title": "质量报表小样"}).json()["id"]
        uploaded = client.post(
            f"/api/v1/projects/{project_id}/source-video",
            files={"file": ("reference.mp4", real_video_bytes(), "video/mp4")},
        )
        assert uploaded.status_code == 201

        generated = client.post(f"/api/v1/production/projects/{project_id}/generation-runs/reference_analysis")
        assert generated.status_code == 202, generated.text
        analysis = client.get(f"/api/v1/production/projects/{project_id}/reference-analyses").json()[0]

        invalid_score = client.post(
            f"/api/v1/production/reference-analyses/{analysis['id']}/lock",
            json={"quality_score": 11},
        )
        assert invalid_score.status_code == 422

        locked = client.post(
            f"/api/v1/production/reference-analyses/{analysis['id']}/lock",
            json={"reviewer_label": "质量测试制作人", "quality_score": 8},
        )
        assert locked.status_code == 200, locked.text

        # 刷新只聚合已发生的调用与审核；不创建任何新的生产任务。
        refreshed = client.post(
            "/api/v1/production/model-quality-evaluations/refresh",
            json={"task_type": "VIDEO_ANALYSIS"},
        )
        assert refreshed.status_code == 200, refreshed.text
        rows = refreshed.json()
        analysis_row = next(row for row in rows if row["task_type"] == "VIDEO_ANALYSIS")
        assert analysis_row["sample_count"] >= 1
        assert analysis_row["success_count"] >= 1
        assert analysis_row["average_human_score"] == 8
        assert analysis_row["adoption_rate"] == 1
        assert analysis_row["average_cost_amount"] is None

        listed = client.get("/api/v1/production/model-quality-evaluations?task_type=VIDEO_ANALYSIS")
        assert listed.status_code == 200
        assert any(row["id"] == analysis_row["id"] for row in listed.json())
