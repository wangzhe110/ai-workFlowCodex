"""V1 参考视频候选列表与显式分析启动边界测试。"""

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import SessionLocal
from app.main import app
from app.models import WorkflowRun
from conftest import real_video_bytes


def _upload(client: TestClient, project_id: str, filename: str) -> dict:
    """上传一条真实 MP4；上传成功仅代表素材已保存，绝不代表已经调用模型。"""

    response = client.post(
        f"/api/v1/projects/{project_id}/source-video",
        files={"file": (filename, real_video_bytes(), "video/mp4")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_uploaded_videos_wait_for_explicit_selection_before_v1_analysis() -> None:
    """多视频上传、删除和勾选分析必须是三个独立的用户动作。"""

    with TestClient(app) as client:
        project_id = client.post("/api/v1/projects", json={"title": "待分析视频列表"}).json()["id"]
        first = _upload(client, project_id, "first.mp4")
        second = _upload(client, project_id, "second.mp4")

        detail = client.get(f"/api/v1/projects/{project_id}")
        assert detail.status_code == 200
        assert [asset["id"] for asset in detail.json()["assets"] if asset["kind"] == "SOURCE_VIDEO"] == [first["id"], second["id"]]
        assert not [run for run in detail.json()["workflow_runs"] if run["workflow_key"] == "v1_reference_analysis"]

        # 未勾选时后端拒绝创建任务，不能偷偷猜“最新上传”的第二条视频。
        missing_selection = client.post(
            f"/api/v1/production/projects/{project_id}/generation-runs/reference_analysis",
            json={},
        )
        assert missing_selection.status_code == 409
        assert "勾选" in missing_selection.json()["detail"]

        # 分析前的误传素材可以删除，剩余候选不受影响。
        deleted = client.delete(f"/api/v1/projects/{project_id}/source-videos/{first['id']}")
        assert deleted.status_code == 204, deleted.text
        remaining = client.get(f"/api/v1/projects/{project_id}").json()["assets"]
        assert [asset["id"] for asset in remaining if asset["kind"] == "SOURCE_VIDEO"] == [second["id"]]

        created = client.post(
            f"/api/v1/production/projects/{project_id}/generation-runs/reference_analysis",
            json={"source_asset_id": second["id"]},
        )
        assert created.status_code == 202, created.text

        db = SessionLocal()
        try:
            run = db.scalar(
                select(WorkflowRun).where(
                    WorkflowRun.project_id == project_id,
                    WorkflowRun.workflow_key == "v1_reference_analysis",
                )
            )
            assert run is not None
            assert run.input_snapshot["context"]["source_asset_id"] == second["id"]
        finally:
            db.close()

        # 一旦用户已明确创建任务，素材成为可追溯的冻结输入，不能再物理删除。
        frozen_delete = client.delete(f"/api/v1/projects/{project_id}/source-videos/{second['id']}")
        assert frozen_delete.status_code == 409
        assert "冻结" in frozen_delete.json()["detail"]
