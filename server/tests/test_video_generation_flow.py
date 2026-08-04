"""从确认分镜、成功图片到可重做视频片段的端到端测试。"""

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app.main import app
from conftest import real_video_bytes
from app.services.video_service import _validate_video_input_urls


def _prepare_confirmed_board(client: TestClient, shot_count: int) -> str:
    """搭建视频模块的最小上游数据，避免测试绕过真实审批门禁。"""

    project_id = client.post("/api/v1/projects", json={"title": "视频片段测试"}).json()["id"]
    client.post(
        f"/api/v1/projects/{project_id}/source-video",
            files={"file": ("reference.mp4", real_video_bytes(), "video/mp4")},
    )
    client.post(f"/api/v1/projects/{project_id}/analysis-runs")
    client.post(f"/api/v1/projects/{project_id}/topic-generation-runs")
    topic = client.get(f"/api/v1/projects/{project_id}/topic-candidates").json()[0]
    client.post(f"/api/v1/topic-candidates/{topic['id']}/select")
    client.post(f"/api/v1/projects/{project_id}/story-generation-runs")
    story = client.get(f"/api/v1/projects/{project_id}/story-packages").json()[0]
    client.post(f"/api/v1/story-packages/{story['id']}/confirm")
    client.post(
        f"/api/v1/projects/{project_id}/storyboard-runs",
        json={"shot_count": shot_count},
    )
    board = client.get(f"/api/v1/projects/{project_id}/storyboard-packages").json()[0]
    client.post(f"/api/v1/storyboard-packages/{board['id']}/confirm")
    return project_id


def test_video_generation_groups_shots_and_keeps_retry_versions() -> None:
    """默认以配置值分组，并且单组重做只新增该组版本。"""

    with TestClient(app) as client:
        project_id = _prepare_confirmed_board(client, shot_count=5)
        blocked = client.post(
            f"/api/v1/projects/{project_id}/video-runs",
            json={"shots_per_group": 2},
        )
        assert blocked.status_code == 409

        assert client.post(f"/api/v1/projects/{project_id}/image-runs").status_code == 202
        response = client.post(
            f"/api/v1/projects/{project_id}/video-runs",
            json={"shots_per_group": 2},
        )
        assert response.status_code == 202
        assert response.json()["workflow_key"] == "video_generation"

        clips = client.get(f"/api/v1/projects/{project_id}/video-clips").json()
        assert [(item["group_number"], item["start_shot_number"], item["end_shot_number"]) for item in clips] == [
            (1, 1, 2),
            (2, 3, 4),
            (3, 5, 5),
        ]
        assert all(item["status"] == "SUCCEEDED" for item in clips)
        assert all(item["video_url"].startswith("mock://video/") for item in clips)

        export = client.post(f"/api/v1/projects/{project_id}/final-video-runs")
        assert export.status_code == 202
        assert export.json()["workflow_key"] == "final_video_export"
        final_videos = client.get(f"/api/v1/projects/{project_id}/final-videos").json()
        assert len(final_videos) == 1
        assert final_videos[0]["status"] == "SUCCEEDED"
        assert len(final_videos[0]["clip_ids"]) == 3
        assert final_videos[0]["video_url"].startswith("mock://final-video/")
        assert final_videos[0]["download_url"] is None

        retry = client.post(
            f"/api/v1/projects/{project_id}/video-runs",
            json={"shots_per_group": 2, "group_numbers": [2]},
        )
        assert retry.status_code == 202
        latest_group_two = [
            item
            for item in client.get(f"/api/v1/projects/{project_id}/video-clips").json()
            if item["group_number"] == 2
        ]
        assert [item["version"] for item in latest_group_two] == [2, 1]

        # 成片 v2 应吸收第 2 组最新重做版本，同时继续使用原完整分组方案的其他组。
        assert client.post(f"/api/v1/projects/{project_id}/final-video-runs").status_code == 202
        exports = client.get(f"/api/v1/projects/{project_id}/final-videos").json()
        assert [item["version"] for item in exports] == [2, 1]
        assert latest_group_two[0]["id"] in exports[0]["clip_ids"]


def test_real_video_preflight_rejects_non_public_mock_images() -> None:
    """真实视频模型在创建任务前拒绝 data URL，避免无效的第三方扣费请求。"""

    snapshot = {
        "provider_key": "configurable_async_video",
        "provider_config": {"end_image_field": "image_end_url"},
    }
    groups = [
        {
            "group_number": 1,
            "shots": [
                {"number": 1, "image_url": "data:image/svg+xml,mock"},
                {"number": 2, "image_url": "https://cdn.example/shot-2.png"},
            ],
        }
    ]

    with pytest.raises(HTTPException, match="公网 HTTPS 图片首帧") as error:
        _validate_video_input_urls(snapshot, groups)
    assert error.value.status_code == 409
