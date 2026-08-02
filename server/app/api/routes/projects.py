"""项目和原视频素材的 HTTP 接口。"""

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.models import AssetKind, Project, WorkflowRun
from app.schemas import AssetResponse, ProjectCreateRequest, ProjectDetailResponse, ProjectSummaryResponse, WorkflowRunResponse
from app.services.storage import asset_storage
from app.services.workflow_service import add_source_video, create_project, get_project_or_404


router = APIRouter(prefix="/api/v1/projects", tags=["项目"])

ALLOWED_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}


def _asset_response(asset) -> AssetResponse:
    """把资产实体转换为公开字段，刻意隐藏 storage_key。"""

    return AssetResponse(
        id=asset.id,
        kind=asset.kind.value,
        original_filename=asset.original_filename,
        content_type=asset.content_type,
        byte_size=asset.byte_size,
        created_at=asset.created_at,
    )


def _step_response(step):
    """延迟导入避免路由文件与契约层形成循环依赖。"""

    from app.schemas import WorkflowStepResponse

    return WorkflowStepResponse(
        id=step.id,
        step_key=step.step_key,
        position=step.position,
        status=step.status.value,
        progress=step.progress,
        attempt=step.attempt,
        output_payload=step.output_payload,
        error_message=step.error_message,
        started_at=step.started_at,
        finished_at=step.finished_at,
    )


def _run_response(run) -> WorkflowRunResponse:
    """统一编码运行状态，保证项目详情和任务接口返回相同结构。"""

    return WorkflowRunResponse(
        id=run.id,
        project_id=run.project_id,
        workflow_key=run.workflow_key,
        status=run.status.value,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        steps=[_step_response(step) for step in run.steps],
    )


def _project_summary_response(project: Project) -> ProjectSummaryResponse:
    """编码列表项；source_video_count 只统计原视频，不混入未来生成资产。"""

    return ProjectSummaryResponse(
        id=project.id,
        title=project.title,
        description=project.description,
        created_at=project.created_at,
        updated_at=project.updated_at,
        source_video_count=sum(asset.kind == AssetKind.SOURCE_VIDEO for asset in project.assets),
    )


@router.post("", response_model=ProjectSummaryResponse, status_code=status.HTTP_201_CREATED)
def create_project_endpoint(payload: ProjectCreateRequest, db: Session = Depends(get_db)) -> ProjectSummaryResponse:
    """创建项目。创建后须上传素材才能启动分析。"""

    project = create_project(db, payload.title, payload.description)
    # 新建实体还未加载关系，直接构造避免无意义的懒加载。
    return ProjectSummaryResponse(
        id=project.id,
        title=project.title,
        description=project.description,
        created_at=project.created_at,
        updated_at=project.updated_at,
        source_video_count=0,
    )


@router.get("", response_model=list[ProjectSummaryResponse])
def list_projects_endpoint(db: Session = Depends(get_db)) -> list[ProjectSummaryResponse]:
    """返回项目列表，附带素材数量以决定页面下一步操作按钮。"""

    # 为避免列表页 N+1 查询，显式预加载资产。
    projects = list(
        db.scalars(select(Project).options(selectinload(Project.assets)).order_by(Project.updated_at.desc())).all()
    )
    return [_project_summary_response(project) for project in projects]


@router.get("/{project_id}", response_model=ProjectDetailResponse)
def get_project_endpoint(project_id: str, db: Session = Depends(get_db)) -> ProjectDetailResponse:
    """返回项目详情、素材与历史工作流，供详情工作台初始加载。"""

    statement = (
        select(Project)
        .options(
            selectinload(Project.assets),
            selectinload(Project.workflow_runs).selectinload(WorkflowRun.steps),
        )
        .where(Project.id == project_id)
    )
    project = db.scalars(statement).first()
    if project is None:
        get_project_or_404(db, project_id)  # 保持统一 404 文案
        raise AssertionError("unreachable")

    summary = _project_summary_response(project)
    return ProjectDetailResponse(
        id=summary.id,
        title=summary.title,
        description=summary.description,
        created_at=summary.created_at,
        updated_at=summary.updated_at,
        source_video_count=summary.source_video_count,
        assets=[_asset_response(asset) for asset in project.assets],
        workflow_runs=[_run_response(run) for run in project.workflow_runs],
    )


@router.post("/{project_id}/source-video", response_model=AssetResponse, status_code=status.HTTP_201_CREATED)
def upload_source_video_endpoint(
    project_id: str,
    file: UploadFile = File(..., description="用户有权使用的参考视频"),
    db: Session = Depends(get_db),
) -> AssetResponse:
    """上传并登记原视频。

    先验证声明类型与扩展名，再写存储。生产阶段还会接入文件大小限制、病毒扫描、
    媒体转码校验和对象存储直传签名。
    """

    suffix = Path(file.filename or "").suffix.lower()
    content_type = file.content_type or "application/octet-stream"
    if not content_type.startswith("video/") and suffix not in ALLOWED_VIDEO_SUFFIXES:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="仅支持常见视频文件")

    # 先验证项目存在，避免非法 project_id 在对象存储留下孤儿文件。
    get_project_or_404(db, project_id)

    try:
        storage_key, byte_size = asset_storage.save_source_video(project_id, file)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_507_INSUFFICIENT_STORAGE, detail="素材存储失败") from exc
    finally:
        file.file.close()

    asset = add_source_video(
        db=db,
        project_id=project_id,
        original_filename=file.filename or "source-video",
        content_type=content_type,
        byte_size=byte_size,
        storage_key=storage_key,
    )
    return _asset_response(asset)
