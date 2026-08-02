"""故事大纲、角色卡、场景卡生成与人工确认接口。

故事包只从人工选定的原创选题创建。模型调用被投递到 Worker，路由层不会保存
第三方密钥、拼装模型提示词或同步等待生成结果。
"""

from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy.orm import Session

from app.api.routes.projects import _run_response
from app.core.database import get_db
from app.schemas import StoryPackageResponse, WorkflowRunResponse
from app.services.story_service import confirm_story_package, create_story_run, list_story_packages
from app.services.worker_runtime import dispatch_story_generation


router = APIRouter(prefix="/api/v1", tags=["故事创作"])


def _story_response(item) -> StoryPackageResponse:
    """将故事 ORM 实体编码为包含大纲、角色和场景的稳定接口契约。"""

    return StoryPackageResponse(
        id=item.id,
        project_id=item.project_id,
        topic_candidate_id=item.topic_candidate_id,
        generation_run_id=item.generation_run_id,
        title=item.title,
        premise=item.premise,
        outline=item.outline,
        roles=item.roles,
        scenes=item.scenes,
        status=item.status.value,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.post(
    "/projects/{project_id}/story-generation-runs",
    response_model=WorkflowRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_story_generation(
    project_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> WorkflowRunResponse:
    """从已确认选题异步创建故事大纲、角色卡和场景卡。"""

    run = create_story_run(db, project_id)
    dispatch_story_generation(background_tasks, run.id)
    return _run_response(run)


@router.get("/projects/{project_id}/story-packages", response_model=list[StoryPackageResponse])
def list_stories(
    project_id: str,
    db: Session = Depends(get_db),
) -> list[StoryPackageResponse]:
    """列出项目历史故事包，供人工审核和版本比较。"""

    return [_story_response(item) for item in list_story_packages(db, project_id)]


@router.post("/story-packages/{package_id}/confirm", response_model=StoryPackageResponse)
def confirm_story(
    package_id: str,
    db: Session = Depends(get_db),
) -> StoryPackageResponse:
    """人工确认故事版本；后续分镜仅消费被确认的一版。"""

    return _story_response(confirm_story_package(db, package_id))
