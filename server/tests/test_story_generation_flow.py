"""故事包生成闭环测试。"""
from fastapi.testclient import TestClient
from app.main import app

def test_selected_topic_can_generate_and_confirm_story_package() -> None:
    """故事必须依赖人工确认选题，确认后才成为后续分镜输入。"""
    with TestClient(app) as client:
        project_id = client.post("/api/v1/projects", json={"title": "故事测试"}).json()["id"]
        client.post(f"/api/v1/projects/{project_id}/source-video", files={"file": ("source.mp4", b"fixture", "video/mp4")})
        client.post(f"/api/v1/projects/{project_id}/analysis-runs")
        client.post(f"/api/v1/projects/{project_id}/topic-generation-runs")
        topic = client.get(f"/api/v1/projects/{project_id}/topic-candidates").json()[0]
        client.post(f"/api/v1/topic-candidates/{topic['id']}/select")
        run = client.post(f"/api/v1/projects/{project_id}/story-generation-runs")
        assert run.status_code == 202
        packages = client.get(f"/api/v1/projects/{project_id}/story-packages").json()
        assert len(packages) == 1 and len(packages[0]["roles"]) >= 1 and len(packages[0]["scenes"]) >= 1
        confirmed = client.post(f"/api/v1/story-packages/{packages[0]['id']}/confirm")
        assert confirmed.status_code == 200 and confirmed.json()["status"] == "CONFIRMED"
