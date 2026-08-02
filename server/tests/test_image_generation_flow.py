"""确认分镜后的批量与单镜图片版本测试。"""
from fastapi.testclient import TestClient
from app.main import app
def test_confirmed_storyboard_generates_versioned_images() -> None:
    with TestClient(app) as c:
        p = c.post("/api/v1/projects", json={"title":"图片测试"}).json()["id"]; c.post(f"/api/v1/projects/{p}/source-video", files={"file":("x.mp4",b"x","video/mp4")}); c.post(f"/api/v1/projects/{p}/analysis-runs"); c.post(f"/api/v1/projects/{p}/topic-generation-runs")
        t=c.get(f"/api/v1/projects/{p}/topic-candidates").json()[0]; c.post(f"/api/v1/topic-candidates/{t['id']}/select"); c.post(f"/api/v1/projects/{p}/story-generation-runs"); s=c.get(f"/api/v1/projects/{p}/story-packages").json()[0]; c.post(f"/api/v1/story-packages/{s['id']}/confirm"); c.post(f"/api/v1/projects/{p}/storyboard-runs",json={"shot_count":2}); b=c.get(f"/api/v1/projects/{p}/storyboard-packages").json()[0]; c.post(f"/api/v1/storyboard-packages/{b['id']}/confirm")
        assert c.post(f"/api/v1/projects/{p}/image-runs").status_code == 202; images=c.get(f"/api/v1/projects/{p}/storyboard-images").json(); assert len(images)==2 and images[0]["image_url"].startswith("data:image/svg")
