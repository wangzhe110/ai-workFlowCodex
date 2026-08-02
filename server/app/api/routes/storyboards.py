"""分镜细纲生成、查询与人工确认接口。

路由层只负责参数校验、响应编码和任务投递；分镜生成规则、模型选择和持久化均在
``storyboard_service`` 中完成，保证将来替换队列或模型不影响 HTTP 契约。
"""

from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy.orm import Session

from app.api.routes.projects import _run_response
from app.core.database import get_db
from app.schemas import StoryboardGenerationRequest, StoryboardPackageResponse, WorkflowRunResponse
from app.services.storyboard_service import confirm, create_run, list_packages
from app.services.worker_runtime import dispatch_workflow


router = APIRouter(prefix="/api/v1", tags=["分镜"])


def _storyboard_response(item) -> StoryboardPackageResponse:
    """把 ORM 分镜包转换成前端稳定使用的 API 响应。"""

    return StoryboardPackageResponse(
        id=item.id,
        project_id=item.project_id,
        story_package_id=item.story_package_id,
        generation_run_id=item.generation_run_id,
        target_shot_count=item.target_shot_count,
        shots=item.shots,
        status=item.status.value,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.post(
    "/projects/{project_id}/storyboard-runs",
    response_model=WorkflowRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_storyboard_generation(
    project_id: str,
    payload: StoryboardGenerationRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> WorkflowRunResponse:
    """按用户指定镜头数创建分镜任务，并交由统一投递边界执行。"""

    run = create_run(db, project_id, payload.shot_count)
    dispatch_workflow(background_tasks, run.workflow_key, run.id)
    return _run_response(run)


@router.get("/projects/{project_id}/storyboard-packages", response_model=list[StoryboardPackageResponse])
def list_storyboards(
    project_id: str,
    db: Session = Depends(get_db),
) -> list[StoryboardPackageResponse]:
    """返回项目的全部历史分镜包，方便人工比较后再确认一版。"""

    return [_storyboard_response(item) for item in list_packages(db, project_id)]


@router.post("/storyboard-packages/{package_id}/confirm", response_model=StoryboardPackageResponse)
def confirm_storyboard(
    package_id: str,
    db: Session = Depends(get_db),
) -> StoryboardPackageResponse:
    """人工确认分镜包；图片和视频步骤只会读取被确认的版本。"""

    return _storyboard_response(confirm(db, package_id))
