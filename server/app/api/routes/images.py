"""分镜图片批量生成、查询与单镜重做接口。

接口不直接请求图片供应商。它创建带模型配置快照的工作流，再经统一 Worker 投递
边界执行，以便未来替换 Redis 队列或图片模型时保持 API 与前端不变。
"""

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy.orm import Session

from app.api.routes.projects import _run_response
from app.core.database import get_db
from app.schemas import StoryboardImageResponse, WorkflowRunResponse
from app.services.image_service import list_images, run
from app.services.worker_runtime import dispatch_workflow


router = APIRouter(prefix="/api/v1", tags=["分镜图片"])


def _image_response(item) -> StoryboardImageResponse:
    """编码图片版本的可审计字段，不把数据库实体直接暴露给前端。"""

    return StoryboardImageResponse(
        id=item.id,
        storyboard_package_id=item.storyboard_package_id,
        generation_run_id=item.generation_run_id,
        shot_number=item.shot_number,
        version=item.version,
        prompt=item.prompt,
        image_url=item.image_url,
        status=item.status.value,
        error_message=item.error_message,
        created_at=item.created_at,
    )


@router.post(
    "/projects/{project_id}/image-runs",
    response_model=WorkflowRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_image_generation(
    project_id: str,
    background_tasks: BackgroundTasks,
    shot_numbers: Optional[list[int]] = None,
    db: Session = Depends(get_db),
) -> WorkflowRunResponse:
    """批量或按镜头创建新图片版本，并异步投递模型任务。"""

    workflow_run = run(db, project_id, shot_numbers)
    dispatch_workflow(background_tasks, workflow_run.workflow_key, workflow_run.id)
    return _run_response(workflow_run)


@router.get("/projects/{project_id}/storyboard-images", response_model=list[StoryboardImageResponse])
def list_storyboard_images(
    project_id: str,
    db: Session = Depends(get_db),
) -> list[StoryboardImageResponse]:
    """返回全部图片历史版本；前端可据镜头号选取最新成功的一版。"""

    return [_image_response(item) for item in list_images(db, project_id)]
