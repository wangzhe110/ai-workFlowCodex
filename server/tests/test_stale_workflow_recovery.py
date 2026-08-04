"""卡死任务必须转为可人工复核状态，不能无限显示“执行中”。"""

from datetime import timedelta

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.main import app
from conftest import real_video_bytes
from app.models import RunStatus, WorkflowStep
from app.services.workflow_service import create_video_analysis_run, utcnow


def test_stale_running_workflow_becomes_failed_and_can_be_retried() -> None:
    """超出安全阈值的 Worker 中断任务会保留错误，再交给用户决定是否重新消费额度。"""

    with TestClient(app) as client:
        project = client.post("/api/v1/projects", json={"title": "卡死任务测试"}).json()
        upload = client.post(
            f"/api/v1/projects/{project['id']}/source-video",
            files={"file": ("authorized.mp4", real_video_bytes(), "video/mp4")},
        )
        assert upload.status_code == 201

        with SessionLocal() as db:
            run = create_video_analysis_run(db, project["id"])
            run.status = RunStatus.RUNNING
            run.started_at = utcnow() - timedelta(seconds=2200)
            running_step = run.steps[0]
            running_step.status = RunStatus.RUNNING
            running_step.started_at = run.started_at
            db.commit()
            run_id = run.id
            running_step_id = running_step.id

        stale_response = client.get(f"/api/v1/workflow-runs/{run_id}")
        assert stale_response.status_code == 200
        stale_run = stale_response.json()
        assert stale_run["status"] == "FAILED"
        stale_step = next(item for item in stale_run["steps"] if item["id"] == running_step_id)
        assert stale_step["status"] == "FAILED"
        assert "已停止等待" in stale_step["error_message"]

        # 失败后仍使用现有重试入口，且本地模拟会同步完成，证明不会永久锁死。
        retry_response = client.post(f"/api/v1/workflow-runs/{run_id}/retry")
        assert retry_response.status_code == 202
        completed_response = client.get(f"/api/v1/workflow-runs/{run_id}")
        assert completed_response.status_code == 200
        assert completed_response.json()["status"] == "SUCCEEDED"

        with SessionLocal() as db:
            stored_step = db.get(WorkflowStep, running_step_id)
            assert stored_step is not None
            assert stored_step.attempt == 1
