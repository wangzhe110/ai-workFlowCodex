"""工作流运行、进度轮询和重试接口。"""

from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy.orm import Session

from app.api.routes.projects import _run_response
from app.core.database import get_db
from app.schemas import WorkflowRunResponse
from app.services.worker_runtime import dispatch_video_analysis, dispatch_workflow
from app.services.workflow_service import create_video_analysis_run, get_workflow_run_or_404, retry_workflow_run


router = APIRouter(prefix="/api/v1", tags=["工作流"])


@router.post("/projects/{project_id}/analysis-runs", response_model=WorkflowRunResponse, status_code=status.HTTP_202_ACCEPTED)
def start_video_analysis_endpoint(
    project_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> WorkflowRunResponse:
    """创建分析运行后立刻投递任务，接口不等待模型完成。"""

    run = create_video_analysis_run(db, project_id)
    dispatch_video_analysis(background_tasks, run.id)
    return _run_response(run)


@router.get("/workflow-runs/{run_id}", response_model=WorkflowRunResponse)
def get_workflow_run_endpoint(run_id: str, db: Session = Depends(get_db)) -> WorkflowRunResponse:
    """供前端轮询或后续 SSE 推送使用的单运行状态接口。"""

    return _run_response(get_workflow_run_or_404(db, run_id))


@router.post("/workflow-runs/{run_id}/retry", response_model=WorkflowRunResponse, status_code=status.HTTP_202_ACCEPTED)
def retry_workflow_run_endpoint(
    run_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> WorkflowRunResponse:
    """重置可重试任务并重新投递；运行 ID 保持不变以便审计。"""

    run = retry_workflow_run(db, run_id)
    dispatch_workflow(background_tasks, run.workflow_key, run.id)
    return _run_response(run)
