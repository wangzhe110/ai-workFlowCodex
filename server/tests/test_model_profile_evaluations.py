"""模型小样本验收记录必须按配置版本保存并正确计算可比较指标。"""

from fastapi.testclient import TestClient

from app.main import app


def test_model_evaluation_records_cost_latency_quality_and_success_rate() -> None:
    """用户可在同一模型配置下追加验收记录，前端无需自行计算关键指标。"""

    with TestClient(app) as client:
        profiles = client.get("/api/v1/model-profiles").json()
        profile = next(item for item in profiles if item["step_key"] == "generate_story_package")

        created = client.post(
            f"/api/v1/model-profiles/{profile['id']}/evaluations",
            json={
                "scenario": "10 条都市情感选题小样",
                "sample_count": 10,
                "success_count": 8,
                "total_cost_yuan": 3.2,
                "average_latency_seconds": 4.5,
                "quality_score": 86,
                "notes": "结构稳定，建议继续扩大样本。",
            },
        )
        assert created.status_code == 201
        record = created.json()
        assert record["success_rate"] == 80.0
        assert record["average_cost_yuan"] == 0.32
        assert record["cost_per_success_yuan"] == 0.4

        listed = client.get(f"/api/v1/model-profiles/{profile['id']}/evaluations")
        assert listed.status_code == 200
        assert listed.json()[0]["id"] == record["id"]

        comparisons = client.get(
            "/api/v1/model-profiles/evaluation-comparisons",
            params={"step_key": "generate_story_package"},
        )
        assert comparisons.status_code == 200
        comparison = next(item for item in comparisons.json() if item["id"] == record["id"])
        assert comparison["model_profile_id"] == profile["id"]
        assert comparison["step_key"] == "generate_story_package"
        assert comparison["profile_version"] == profile["version"]


def test_model_evaluation_rejects_success_count_larger_than_sample_count() -> None:
    """错误统计不能污染后续模型成本比较。"""

    with TestClient(app) as client:
        profile = client.get("/api/v1/model-profiles").json()[0]
        response = client.post(
            f"/api/v1/model-profiles/{profile['id']}/evaluations",
            json={
                "scenario": "无效统计",
                "sample_count": 2,
                "success_count": 3,
                "total_cost_yuan": 1,
                "average_latency_seconds": 1,
                "quality_score": 50,
            },
        )

    assert response.status_code == 422
    assert "成功样本数不能大于总样本数" in response.json()["detail"]
