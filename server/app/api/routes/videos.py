"""按镜头组创建、查询和重做视频片段的 HTTP 接口。"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.routes.projects import _run_response
from app.core.database import get_db
from app.schemas import FinalVideoResponse, VideoClipResponse, VideoGenerationRequest, WorkflowRunResponse
from app.services.final_video_service import (
    create_final_video_export_run,
    get_final_video_or_404,
    list_final_videos,
)
from app.services.storage import local_asset_storage
from app.services.video_service import create_video_generation_run, list_video_clips
from app.services.worker_runtime import dispatch_workflow


router = APIRouter(prefix="/api/v1", tags=["视频片段"])


def _video_clip_response(item) -> VideoClipResponse:
    """将数据库枚举和值对象转换成稳定的 API 输出。"""

    return VideoClipResponse(
        id=item.id,
        storyboard_package_id=item.storyboard_package_id,
        generation_run_id=item.generation_run_id,
        group_number=item.group_number,
        start_shot_number=item.start_shot_number,
        end_shot_number=item.end_shot_number,
        shots_per_group=item.shots_per_group,
        version=item.version,
        image_ids=item.image_ids,
        prompt=item.prompt,
        video_url=item.video_url,
        provider_task_id=item.provider_task_id,
        status=item.status.value,
        error_message=item.error_message,
        created_at=item.created_at,
    )


def _final_video_response(item) -> FinalVideoResponse:
    """只暴露受项目范围保护的下载地址，不暴露底层 storage_key。"""

    download_url = item.output_url if item.output_url and item.output_url.startswith("https://") else (
        f"/api/v1/projects/{item.project_id}/final-videos/{item.id}/download"
        if item.storage_key and item.status.value == "SUCCEEDED"
        else None
    )
    return FinalVideoResponse(
        id=item.id,
        storyboard_package_id=item.storyboard_package_id,
        generation_run_id=item.generation_run_id,
        version=item.version,
        clip_ids=item.clip_ids,
        video_url=item.output_url,
        download_url=download_url,
        status=item.status.value,
        error_message=item.error_message,
        created_at=item.created_at,
        finished_at=item.finished_at,
    )


@router.post(
    "/projects/{project_id}/video-runs",
    response_model=WorkflowRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_video_generation(
    project_id: str,
    payload: VideoGenerationRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> WorkflowRunResponse:
    """投递全量或指定视频组；模型执行始终放入后台。"""

    run = create_video_generation_run(
        db,
        project_id,
        payload.shots_per_group,
        payload.group_numbers,
    )
    dispatch_workflow(background_tasks, run.workflow_key, run.id)
    return _run_response(run)


@router.get("/projects/{project_id}/video-clips", response_model=list[VideoClipResponse])
def list_all_video_clips(
    project_id: str,
    db: Session = Depends(get_db),
) -> list[VideoClipResponse]:
    """返回全部片段版本，让前端能够展示最新版本与重做历史。"""

    return [_video_clip_response(item) for item in list_video_clips(db, project_id)]


@router.post(
    "/projects/{project_id}/final-video-runs",
    response_model=WorkflowRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_final_video_export(
    project_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> WorkflowRunResponse:
    """冻结当前完整片段方案并投递完整成片合成任务。"""

    run = create_final_video_export_run(db, project_id)
    dispatch_workflow(background_tasks, run.workflow_key, run.id)
    return _run_response(run)


@router.get("/projects/{project_id}/final-videos", response_model=list[FinalVideoResponse])
def list_all_final_videos(project_id: str, db: Session = Depends(get_db)) -> list[FinalVideoResponse]:
    """返回完整成片的版本历史；页面首项为最近一次导出。"""

    return [_final_video_response(item) for item in list_final_videos(db, project_id)]


@router.get("/projects/{project_id}/final-videos/{final_video_id}/download")
def download_final_video(
    project_id: str,
    final_video_id: str,
    db: Session = Depends(get_db),
) -> FileResponse:
    """下载本地存储的真实 MP4；模拟结果与未完成成片不可下载。"""

    final_video = get_final_video_or_404(db, project_id, final_video_id)
    if final_video.status.value != "SUCCEEDED" or not final_video.storage_key:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="完整成片尚未生成可下载文件")
    try:
        path = local_asset_storage.final_video_path(final_video.storage_key)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="完整成片文件不存在") from exc
    return FileResponse(path, media_type="video/mp4", filename=f"完整成片-v{final_video.version}.mp4")
