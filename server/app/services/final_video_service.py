"""完整成片的选择、合成与版本化服务。

视频片段生成完成并不代表已经有一条可交付视频。本模块固定选择同一确认分镜下
最近一次“完整分组方案”，再叠加每组最近的人工重做版本，按组号顺序合成为一版
成片。它不处理配音、字幕或音乐；这些将作为后续可插拔的独立后期步骤。
"""

from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, Optional
from urllib.request import Request, urlopen

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import (
    FinalVideo,
    FinalVideoStatus,
    RunStatus,
    StoryboardPackage,
    VideoClip,
    VideoClipStatus,
    WorkflowRun,
    WorkflowStep,
)
from app.services.image_service import selected_board
from app.services.model_profile_service import get_active_profile_snapshot
from app.services.sensitive_data import sanitize_error_summary
from app.services.storage import FinalVideoDeliveryResult, final_video_delivery
from app.services.workflow_service import get_project_or_404, get_workflow_run_or_404


FINAL_VIDEO_EXPORT_WORKFLOW = "final_video_export"
FINAL_VIDEO_EXPORT_STEP = "assemble_final_video"


def utcnow() -> datetime:
    """提供成片工作流使用的统一 UTC 时间。"""

    return datetime.now(timezone.utc)


def _valid_complete_group_plan(payload: Any, board: StoryboardPackage) -> Optional[list[dict[str, Any]]]:
    """验证某次视频生成运行是否覆盖当前确认分镜的全部连续镜头组。"""

    if not isinstance(payload, dict) or payload.get("storyboard_package_id") != board.id:
        return None
    groups = payload.get("groups")
    if not isinstance(groups, list) or not groups:
        return None
    expected_shots = sorted(int(shot["number"]) for shot in board.shots)
    actual_shots: list[int] = []
    normalized_groups: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            return None
        try:
            group_number = int(group["group_number"])
            start = int(group["start_shot_number"])
            end = int(group["end_shot_number"])
        except (KeyError, TypeError, ValueError):
            return None
        if group_number < 1 or start > end:
            return None
        actual_shots.extend(range(start, end + 1))
        normalized_groups.append(
            {
                "group_number": group_number,
                "start_shot_number": start,
                "end_shot_number": end,
                "shots_per_group": int(payload.get("shots_per_group", 0)),
            }
        )
    if sorted(actual_shots) != expected_shots:
        return None
    if len({group["group_number"] for group in normalized_groups}) != len(normalized_groups):
        return None
    if not all(group["shots_per_group"] >= 1 for group in normalized_groups):
        return None
    return sorted(normalized_groups, key=lambda group: group["group_number"])


def _latest_complete_group_plan(db: Session, board: StoryboardPackage) -> list[dict[str, Any]]:
    """读取最近成功的全量片段运行，排除只重做一组的局部运行。"""

    statement = (
        select(WorkflowStep)
        .join(WorkflowRun, WorkflowStep.workflow_run_id == WorkflowRun.id)
        .where(
            WorkflowRun.project_id == board.project_id,
            WorkflowRun.workflow_key == "video_generation",
            WorkflowRun.status == RunStatus.SUCCEEDED,
            WorkflowStep.step_key == "generate_storyboard_video_groups",
            WorkflowStep.status == RunStatus.SUCCEEDED,
        )
        .order_by(WorkflowRun.finished_at.desc())
    )
    for step in db.scalars(statement):
        plan = _valid_complete_group_plan(step.input_payload, board)
        if plan is not None:
            return plan
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="请先为当前确认分镜成功生成一整套视频片段，再合成完整成片",
    )


def _latest_succeeded_clip(db: Session, board_id: str, group: dict[str, Any]) -> Optional[VideoClip]:
    """取同一分组定义下最新成功版本，吸收用户对单组的局部重做。"""

    statement = (
        select(VideoClip)
        .where(
            VideoClip.storyboard_package_id == board_id,
            VideoClip.group_number == group["group_number"],
            VideoClip.start_shot_number == group["start_shot_number"],
            VideoClip.end_shot_number == group["end_shot_number"],
            VideoClip.shots_per_group == group["shots_per_group"],
            VideoClip.status == VideoClipStatus.SUCCEEDED,
        )
        .order_by(VideoClip.version.desc())
    )
    return db.scalars(statement).first()


def _clip_selection_for_export(db: Session, board: StoryboardPackage) -> list[VideoClip]:
    """冻结完整成片需要的有序片段；任何缺失组都会阻止产生不完整作品。"""

    plan = _latest_complete_group_plan(db, board)
    clips: list[VideoClip] = []
    missing_groups: list[str] = []
    for group in plan:
        clip = _latest_succeeded_clip(db, board.id, group)
        if clip is None:
            missing_groups.append(str(group["group_number"]))
        else:
            clips.append(clip)
    if missing_groups:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"以下视频组尚未成功：第 {', '.join(missing_groups)} 组；请重做后再合成",
        )
    return clips


def _next_version(db: Session, storyboard_package_id: str) -> int:
    """计算当前确认分镜下一版完整成片版本号。"""

    return (
        db.scalar(select(func.max(FinalVideo.version)).where(FinalVideo.storyboard_package_id == storyboard_package_id))
        or 0
    ) + 1


def create_final_video_export_run(db: Session, project_id: str) -> WorkflowRun:
    """创建一版成片导出任务，冻结按顺序选择的片段 ID 与渲染配置。"""

    get_project_or_404(db, project_id)
    board = selected_board(db, project_id)
    clips = _clip_selection_for_export(db, board)
    run = WorkflowRun(project_id=project_id, workflow_key=FINAL_VIDEO_EXPORT_WORKFLOW)
    db.add(run)
    db.flush()
    final_video = FinalVideo(
        project_id=project_id,
        storyboard_package_id=board.id,
        generation_run_id=run.id,
        version=_next_version(db, board.id),
        clip_ids=[clip.id for clip in clips],
        status=FinalVideoStatus.PENDING,
    )
    db.add(final_video)
    db.flush()
    step = WorkflowStep(
        workflow_run=run,
        step_key=FINAL_VIDEO_EXPORT_STEP,
        position=1,
        input_payload={
            "final_video_id": final_video.id,
            "storyboard_package_id": board.id,
            "clip_ids": final_video.clip_ids,
        },
        model_profile_snapshot=get_active_profile_snapshot(db, FINAL_VIDEO_EXPORT_STEP),
    )
    # ``run`` 已经提前 flush 以生成外键 ID，因此关系级 cascade 不会再自动将新
    # 步骤加入 Session；必须显式 add，保证 Worker 能读取到它。
    db.add(step)
    db.commit()
    return get_workflow_run_or_404(db, run.id)


def _ffmpeg_concat_settings(snapshot: dict[str, Any]) -> tuple[float, int, int, float]:
    """从冻结配置读取下载与渲染资源上限，并在 Worker 再次实施门禁。"""

    config = snapshot.get("provider_config") or {}
    timeout = config.get("download_timeout_seconds", 120)
    max_clip_bytes = config.get("max_clip_bytes", 500 * 1024 * 1024)
    max_output_bytes = config.get("max_output_bytes", 2 * 1024 * 1024 * 1024)
    render_timeout = config.get("render_timeout_seconds", 1800)
    if not isinstance(timeout, (int, float)) or not 5 <= float(timeout) <= 600:
        raise RuntimeError("download_timeout_seconds 必须在 5 至 600 秒之间")
    if not isinstance(max_clip_bytes, int) or not 1 * 1024 * 1024 <= max_clip_bytes <= 2 * 1024 * 1024 * 1024:
        raise RuntimeError("max_clip_bytes 必须在 1MB 至 2GB 之间")
    if not isinstance(max_output_bytes, int) or not 1 * 1024 * 1024 <= max_output_bytes <= 10 * 1024 * 1024 * 1024:
        raise RuntimeError("max_output_bytes 必须在 1MB 至 10GB 之间")
    if not isinstance(render_timeout, (int, float)) or not 30 <= float(render_timeout) <= 7200:
        raise RuntimeError("render_timeout_seconds 必须在 30 至 7200 秒之间")
    return float(timeout), max_clip_bytes, max_output_bytes, float(render_timeout)


def _download_clip(url: str, destination: Path, *, timeout: float, max_bytes: int) -> None:
    """下载单个 HTTPS 片段，并拒绝超大响应或明显不是视频的内容类型。"""

    if not isinstance(url, str) or not url.startswith("https://"):
        raise RuntimeError("真实完整成片只能合成供应商可公开访问的 HTTPS 视频地址")
    request = Request(url, headers={"Accept": "video/*,application/octet-stream"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            if content_type.startswith("text/") or content_type == "application/json":
                raise RuntimeError("视频地址返回了非视频内容，无法合成")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise RuntimeError("视频片段超过 max_clip_bytes 限制")
            with destination.open("wb") as target:
                remaining = max_bytes + 1
                while remaining:
                    chunk = response.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    target.write(chunk)
                    remaining -= len(chunk)
    except RuntimeError:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise RuntimeError("无法下载视频片段；请检查供应商结果地址、网络或有效期") from exc
    if not destination.is_file() or not destination.stat().st_size:
        raise RuntimeError("下载的视频片段为空")
    if destination.stat().st_size > max_bytes:
        destination.unlink(missing_ok=True)
        raise RuntimeError("视频片段超过 max_clip_bytes 限制")


def _concat_to_mp4(clip_paths: list[Path], output_path: Path, *, timeout: float) -> None:
    """以 FFmpeg 重新编码合并片段，增加不同来源编码参数的兼容性。"""

    if shutil.which("ffmpeg") is None:
        raise RuntimeError("真实完整成片需要安装系统依赖：ffmpeg")
    manifest_path = output_path.with_suffix(".txt")
    manifest_path.write_text(
        "".join(f"file '{path.as_posix()}'\n" for path in clip_paths),
        encoding="utf-8",
    )
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(manifest_path),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(output_path),
            ],
            capture_output=True,
            check=True,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("FFmpeg 合成失败；请确认所有片段可下载且编码格式兼容") from exc


def _compose_real_video(
    *,
    project_id: str,
    final_video_id: str,
    clips: list[VideoClip],
    snapshot: dict[str, Any],
) -> FinalVideoDeliveryResult:
    """下载、合成并保存完整 MP4，临时素材始终在函数退出时销毁。"""

    download_timeout, max_clip_bytes, max_output_bytes, render_timeout = _ffmpeg_concat_settings(snapshot)
    with TemporaryDirectory(prefix="ai-drama-final-video-") as directory:
        temp_dir = Path(directory)
        clip_paths: list[Path] = []
        for position, clip in enumerate(clips, start=1):
            clip_path = temp_dir / f"clip-{position:04d}.mp4"
            _download_clip(
                clip.video_url or "",
                clip_path,
                timeout=download_timeout,
                max_bytes=max_clip_bytes,
            )
            clip_paths.append(clip_path)
        output_path = temp_dir / "final.mp4"
        _concat_to_mp4(clip_paths, output_path, timeout=render_timeout)
        if not output_path.is_file() or not output_path.stat().st_size:
            raise RuntimeError("FFmpeg 未生成完整成片文件")
        if output_path.stat().st_size > max_output_bytes:
            raise RuntimeError("完整成片超过 max_output_bytes 限制")
        return final_video_delivery.persist(
            project_id=project_id,
            final_video_id=final_video_id,
            source_path=output_path,
        )


def execute_final_video_export(run_id: str) -> None:
    """由 Worker 执行完整成片任务；模拟模式不伪造 MP4，只提供可审计占位结果。"""

    db = SessionLocal()
    final_video: Optional[FinalVideo] = None
    try:
        run = get_workflow_run_or_404(db, run_id)
        if run.workflow_key != FINAL_VIDEO_EXPORT_WORKFLOW or run.status != RunStatus.PENDING:
            return
        step = run.steps[0]
        final_video_id = str(step.input_payload["final_video_id"])
        final_video = db.get(FinalVideo, final_video_id)
        if final_video is None:
            raise RuntimeError("完整成片记录不存在")
        clips = [db.get(VideoClip, clip_id) for clip_id in final_video.clip_ids]
        if any(clip is None or clip.status != VideoClipStatus.SUCCEEDED for clip in clips):
            raise RuntimeError("成片所需的视频片段已缺失或未成功，无法合成")
        ordered_clips = [clip for clip in clips if clip is not None]
        if [clip.id for clip in ordered_clips] != final_video.clip_ids:
            raise RuntimeError("成片片段顺序异常，已停止合成")

        run.status = RunStatus.RUNNING
        run.started_at = utcnow()
        step.status = RunStatus.RUNNING
        step.started_at = utcnow()
        step.attempt += 1
        step.progress = 10
        final_video.status = FinalVideoStatus.PENDING
        final_video.error_message = None
        final_video.output_url = None
        final_video.storage_key = None
        db.commit()

        snapshot = step.model_profile_snapshot or {}
        if snapshot.get("provider_key") == "mock_provider":
            # 模拟结果用于跑通审核/下载状态；不会伪造一个不可播放的 MP4 文件。
            final_video.output_url = f"mock://final-video/{final_video.id}"
            step.progress = 85
            db.commit()
        elif snapshot.get("provider_key") == "ffmpeg_concat":
            delivery_result = _compose_real_video(
                project_id=run.project_id,
                final_video_id=final_video.id,
                clips=ordered_clips,
                snapshot=snapshot,
            )
            final_video.storage_key = delivery_result.storage_key
            final_video.output_url = delivery_result.public_url
            step.progress = 90
            db.commit()
        else:
            raise RuntimeError(f"完整成片步骤没有可执行的适配器：{snapshot.get('provider_key') or '未配置'}")

        final_video.status = FinalVideoStatus.SUCCEEDED
        final_video.finished_at = utcnow()
        step.output_payload = {"final_video_id": final_video.id, "clip_ids": final_video.clip_ids}
        step.status = RunStatus.SUCCEEDED
        step.progress = 100
        step.finished_at = utcnow()
        run.status = RunStatus.SUCCEEDED
        run.finished_at = utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        error_message = sanitize_error_summary(exc, max_length=2000)
        run = db.get(WorkflowRun, run_id)
        if run is not None:
            step = db.scalars(
                select(WorkflowStep).where(WorkflowStep.workflow_run_id == run_id).order_by(WorkflowStep.position)
            ).first()
            final_video_id = step.input_payload.get("final_video_id") if step is not None else None
            final_video = db.get(FinalVideo, final_video_id) if isinstance(final_video_id, str) else None
            run.status = RunStatus.FAILED
            run.finished_at = utcnow()
            if step is not None:
                step.status = RunStatus.FAILED
                step.error_message = error_message
                step.finished_at = utcnow()
            if final_video is not None:
                final_video.status = FinalVideoStatus.FAILED
                final_video.error_message = error_message
                final_video.finished_at = utcnow()
            db.commit()
    finally:
        db.close()


def list_final_videos(db: Session, project_id: str) -> list[FinalVideo]:
    """按版本倒序列出项目的成片历史，支持审核、下载和版本回退。"""

    get_project_or_404(db, project_id)
    return list(
        db.scalars(
            select(FinalVideo).where(FinalVideo.project_id == project_id).order_by(FinalVideo.created_at.desc())
        ).all()
    )


def get_final_video_or_404(db: Session, project_id: str, final_video_id: str) -> FinalVideo:
    """按项目范围读取成片，避免任意 ID 跨项目下载。"""

    result = db.scalars(
        select(FinalVideo).where(FinalVideo.id == final_video_id, FinalVideo.project_id == project_id)
    ).first()
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="完整成片不存在")
    return result
