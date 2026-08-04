"""分镜细纲工作流测试。"""
from fastapi.testclient import TestClient
from app.main import app
from conftest import real_video_bytes
def test_confirmed_story_can_generate_configurable_storyboard() -> None:
    with TestClient(app) as client:
        pid = client.post("/api/v1/projects", json={"title": "分镜测试"}).json()["id"]
        client.post(f"/api/v1/projects/{pid}/source-video", files={"file": ("source.mp4", real_video_bytes(), "video/mp4")}); client.post(f"/api/v1/projects/{pid}/analysis-runs"); client.post(f"/api/v1/projects/{pid}/topic-generation-runs")
        topic = client.get(f"/api/v1/projects/{pid}/topic-candidates").json()[0]; client.post(f"/api/v1/topic-candidates/{topic['id']}/select"); client.post(f"/api/v1/projects/{pid}/story-generation-runs")
        story = client.get(f"/api/v1/projects/{pid}/story-packages").json()[0]; client.post(f"/api/v1/story-packages/{story['id']}/confirm")
        run = client.post(f"/api/v1/projects/{pid}/storyboard-runs", json={"shot_count": 7}); assert run.status_code == 202
        package = client.get(f"/api/v1/projects/{pid}/storyboard-packages").json()[0]; assert package["target_shot_count"] == 7 and len(package["shots"]) == 7
        assert client.post(f"/api/v1/storyboard-packages/{package['id']}/confirm").json()["status"] == "CONFIRMED"
