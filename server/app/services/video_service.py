"""按连续镜头组生成短视频片段的领域服务。

本模块只编排分镜、图片版本、任务状态和视频适配器，不依赖任何一家中转站的
请求格式。生产接入时仅替换 `VideoGenerationProvider` 实现与步骤模型配置。
"""

from datetime import datetime, timezone
import time
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import (
    ImageStatus,
    RunStatus,
    StoryboardImage,
    StoryboardPackage,
    StoryboardStatus,
    VideoClip,
    VideoClipStatus,
    WorkflowRun,
    WorkflowStep,
)
from app.services.analysis_provider import (
    ConfigurableAsyncVideoProvider,
    MockVideoGenerationProvider,
    VolcengineArkVideoProvider,
    VideoGenerationInput,
    VideoGenerationProvider,
    VideoTaskResult,
)
from app.services.image_service import selected_board
from app.services.model_profile_service import get_active_profile_snapshot
from app.services.workflow_service import get_project_or_404, get_workflow_run_or_404


VIDEO_GENERATION_WORKFLOW = "video_generation"
VIDEO_GENERATION_STEP = "generate_storyboard_video_groups"


def utcnow() -> datetime:
    """为工作流与片段结果提供统一 UTC 时间。"""

    return datetime.now(timezone.utc)


def _latest_successful_images(
    db: Session, storyboard_package_id: str
) -> dict[int, StoryboardImage]:
    """读取每个镜头最新的成功图片版本。

    只挑选成功且存在地址的版本，避免重跑图片过程中把 PENDING 或 FAILED 版本
    意外传给视频模型。返回值以镜头号为 key，便于精确检查缺失镜头。
    """

    statement = (
        select(StoryboardImage)
        .where(
            StoryboardImage.storyboard_package_id == storyboard_package_id,
            StoryboardImage.status == ImageStatus.SUCCEEDED,
            StoryboardImage.image_url.is_not(None),
        )
        .order_by(StoryboardImage.shot_number, StoryboardImage.version.desc())
    )
    latest: dict[int, StoryboardImage] = {}
    for image in db.scalars(statement):
        latest.setdefault(image.shot_number, image)
    return latest


def _build_groups(
    board: StoryboardPackage,
    images_by_shot: dict[int, StoryboardImage],
    shots_per_group: int,
    group_numbers: Optional[list[int]],
) -> list[dict]:
    """将确认分镜按配置切分为连续组，并冻结本次采用的图片版本。"""

    shots = sorted(board.shots, key=lambda shot: int(shot["number"]))
    missing = [str(shot["number"]) for shot in shots if int(shot["number"]) not in images_by_shot]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"请先生成并确认以下镜头的成功图片：第 {', '.join(missing)} 镜",
        )

    all_groups: list[dict] = []
    for group_number, index in enumerate(range(0, len(shots), shots_per_group), start=1):
        group_shots = shots[index : index + shots_per_group]
        all_groups.append(
            {
                "group_number": group_number,
                "start_shot_number": int(group_shots[0]["number"]),
                "end_shot_number": int(group_shots[-1]["number"]),
                "shots": [
                    {
                        "number": int(shot["number"]),
                        "video_prompt": shot["video_prompt"],
                        "image_id": images_by_shot[int(shot["number"])].id,
                        "image_url": images_by_shot[int(shot["number"])].image_url,
                    }
                    for shot in group_shots
                ],
            }
        )

    if not group_numbers:
        return all_groups
    requested = set(group_numbers)
    result = [group for group in all_groups if group["group_number"] in requested]
    if len(result) != len(requested):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="未找到指定视频片段组")
    return result


def _group_prompt(group: dict) -> str:
    """将同一组镜头的规范视频提示词收敛为供应商无关的片段提示词。"""

    instructions = list(dict.fromkeys(shot["video_prompt"] for shot in group["shots"]))
    return (
        f"原创短剧连续片段，第 {group['start_shot_number']} 至 "
        f"{group['end_shot_number']} 镜；保持角色、场景、光线与运动方向连续。"
        f" {'；'.join(instructions)}"
    )


def _validate_video_input_urls(snapshot: dict, groups: list[dict]) -> None:
    """在创建任务前验证真实视频模型所需的公网首帧。

    模拟模型可以使用本地 ``data:`` 占位图；通用异步视频适配器则会把每组首镜
    （以及配置了结束帧字段时的末镜）交给第三方中转站。因此必须在扣费请求之前
    确保这些图片均为 HTTPS 地址，而不是等后台 Worker 已创建任务后才失败。
    """

    if snapshot.get("provider_key") not in {"configurable_async_video", "volcengine_ark_video"}:
        return
    config = snapshot.get("provider_config") or {}
    needs_end_frame = bool(config.get("end_image_field")) or bool(config.get("use_last_frame"))
    invalid_sources: list[str] = []
    for group in groups:
        required_shots = [group["shots"][0]]
        if needs_end_frame and len(group["shots"]) > 1:
            required_shots.append(group["shots"][-1])
        for shot in required_shots:
            image_url = shot["image_url"]
            if not isinstance(image_url, str) or not image_url.startswith("https://"):
                invalid_sources.append(f"第 {group['group_number']} 组第 {shot['number']} 镜")
    if invalid_sources:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "当前真实视频模型需要公网 HTTPS 图片首帧；"
                f"以下镜头尚不满足：{', '.join(invalid_sources)}。"
                "请先使用真实图片模型，或将图片保存到对象存储后再生成视频。"
            ),
        )


def create_video_generation_run(
    db: Session,
    project_id: str,
    shots_per_group: int,
    group_numbers: Optional[list[int]] = None,
) -> WorkflowRun:
    """创建视频片段生成运行，但不在 HTTP 请求中执行模型调用。"""

    get_project_or_404(db, project_id)
    board = selected_board(db, project_id)
    groups = _build_groups(
        board,
        _latest_successful_images(db, board.id),
        shots_per_group,
        group_numbers,
    )
    model_profile_snapshot = get_active_profile_snapshot(db, VIDEO_GENERATION_STEP)
    _validate_video_input_urls(model_profile_snapshot, groups)
    run = WorkflowRun(project_id=project_id, workflow_key=VIDEO_GENERATION_WORKFLOW)
    WorkflowStep(
        workflow_run=run,
        step_key=VIDEO_GENERATION_STEP,
        position=1,
        input_payload={
            "storyboard_package_id": board.id,
            "shots_per_group": shots_per_group,
            "groups": groups,
        },
        model_profile_snapshot=model_profile_snapshot,
    )
    db.add(run)
    db.commit()
    return get_workflow_run_or_404(db, run.id)


def _video_provider(snapshot: dict) -> VideoGenerationProvider:
    """按运行时冻结的模型快照选择适配器，而非读取当前活动配置。

    这样管理员在任务执行期间切换模型，也不会把已经提交的任务切到另一家中转站。
    模型配置中心只允许启用已接通的适配器，未知键在这里仍会明确报错。
    """

    provider_key = snapshot.get("provider_key")
    if provider_key == "mock_provider":
        return MockVideoGenerationProvider()
    if provider_key == "configurable_async_video":
        return ConfigurableAsyncVideoProvider(snapshot)
    if provider_key == "volcengine_ark_video":
        return VolcengineArkVideoProvider(snapshot)
    raise RuntimeError(f"视频步骤没有可执行的供应商适配器：{provider_key or '未配置'}")


def _polling_settings(snapshot: dict) -> tuple[float, float]:
    """读取轮询间隔和最长等待时间，避免单次任务无限占用 Worker。"""

    config = snapshot.get("provider_config") or {}
    try:
        interval = float(config.get("poll_interval_seconds", 4))
        maximum = float(config.get("max_poll_seconds", 900))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("poll_interval_seconds 和 max_poll_seconds 必须为数字") from exc
    return min(max(interval, 1), 60), min(max(maximum, 10), 1800)


def _latest_version(db: Session, storyboard_package_id: str, group_number: int) -> int:
    """计算单个片段组的下一个不可变版本号。"""

    return (
        db.scalar(
            select(func.max(VideoClip.version)).where(
                VideoClip.storyboard_package_id == storyboard_package_id,
                VideoClip.group_number == group_number,
            )
        )
        or 0
    )


def _apply_provider_result(clip: VideoClip, result: VideoTaskResult) -> None:
    """将适配器结果投影到片段表，不保存中转站的原始响应。"""

    if result.provider_task_id:
        clip.provider_task_id = result.provider_task_id
    if result.status == "SUCCEEDED":
        if not result.video_url:
            raise RuntimeError("视频适配器返回成功状态但没有视频地址")
        clip.status = VideoClipStatus.SUCCEEDED
        clip.video_url = result.video_url
        clip.error_message = None
    elif result.status == "FAILED":
        clip.status = VideoClipStatus.FAILED
        clip.error_message = (result.error_message or "视频供应商任务失败")[:2000]
    elif result.status != "PENDING":
        raise RuntimeError(f"视频适配器返回未知标准状态：{result.status}")


def _update_progress(step: WorkflowStep, clips: list[VideoClip]) -> None:
    """按终态片段比例更新工作流进度，让前端能区分轮询等待与无响应。"""

    terminal_count = sum(
        clip.status in {VideoClipStatus.SUCCEEDED, VideoClipStatus.FAILED} for clip in clips
    )
    step.progress = int(terminal_count / len(clips) * 100) if clips else 100


def execute_video_generation(run_id: str) -> None:
    """由后台 Worker 提交、轮询每个视频组，并保存可追溯版本。

    当前本地模式由进程内 BackgroundTask 运行，适合联调；生产必须以同一个函数
    作为 Redis/Celery/RQ Worker 的消费者，避免 Web 进程重启造成轮询中断。
    """

    db = SessionLocal()
    try:
        run = get_workflow_run_or_404(db, run_id)
        if run.workflow_key != VIDEO_GENERATION_WORKFLOW or run.status != RunStatus.PENDING:
            return

        step = run.steps[0]
        run.status = RunStatus.RUNNING
        run.started_at = utcnow()
        step.status = RunStatus.RUNNING
        step.started_at = utcnow()
        step.attempt += 1
        db.commit()

        snapshot = step.model_profile_snapshot or {}
        provider = _video_provider(snapshot)
        groups = step.input_payload["groups"]
        existing_clips = list(
            db.scalars(
                select(VideoClip)
                .where(VideoClip.generation_run_id == run.id)
                .order_by(VideoClip.group_number)
            ).all()
        )
        if existing_clips:
            # 重试同一个失败运行不新增版本：它是基础设施级重试，不是用户要求的创作重做。
            for clip in existing_clips:
                if clip.status == VideoClipStatus.FAILED:
                    clip.status = VideoClipStatus.PENDING
                    clip.provider_task_id = None
                    clip.video_url = None
                    clip.error_message = None
            db.commit()
        else:
            for group in groups:
                prompt = _group_prompt(group)
                clip = VideoClip(
                    project_id=run.project_id,
                    storyboard_package_id=step.input_payload["storyboard_package_id"],
                    generation_run_id=run.id,
                    group_number=group["group_number"],
                    start_shot_number=group["start_shot_number"],
                    end_shot_number=group["end_shot_number"],
                    shots_per_group=step.input_payload["shots_per_group"],
                    version=_latest_version(
                        db,
                        step.input_payload["storyboard_package_id"],
                        group["group_number"],
                    )
                    + 1,
                    image_ids=[shot["image_id"] for shot in group["shots"]],
                    prompt=prompt,
                    status=VideoClipStatus.PENDING,
                )
                db.add(clip)
            db.commit()
            existing_clips = list(
                db.scalars(
                    select(VideoClip)
                    .where(VideoClip.generation_run_id == run.id)
                    .order_by(VideoClip.group_number)
                ).all()
            )

        group_by_number = {group["group_number"]: group for group in groups}
        for clip in existing_clips:
            if clip.status != VideoClipStatus.PENDING or clip.provider_task_id:
                continue
            group = group_by_number[clip.group_number]
            prompt = _group_prompt(group)
            try:
                result = provider.submit(
                    VideoGenerationInput(
                        project_id=run.project_id,
                        group_number=group["group_number"],
                        start_shot_number=group["start_shot_number"],
                        end_shot_number=group["end_shot_number"],
                        prompt=prompt,
                        image_urls=[shot["image_url"] for shot in group["shots"]],
                    )
                )
                _apply_provider_result(clip, result)
            except Exception as exc:
                clip.status = VideoClipStatus.FAILED
                clip.error_message = str(exc)[:2000]
            _update_progress(step, existing_clips)
            db.commit()

        interval, maximum_wait = _polling_settings(snapshot)
        deadline = time.monotonic() + maximum_wait
        while any(clip.status == VideoClipStatus.PENDING for clip in existing_clips):
            if time.monotonic() >= deadline:
                for clip in existing_clips:
                    if clip.status == VideoClipStatus.PENDING:
                        clip.status = VideoClipStatus.FAILED
                        clip.error_message = "等待视频供应商结果超时；可检查模型配置后重试"
                _update_progress(step, existing_clips)
                db.commit()
                break
            for clip in existing_clips:
                if clip.status != VideoClipStatus.PENDING or not clip.provider_task_id:
                    continue
                try:
                    _apply_provider_result(clip, provider.poll(clip.provider_task_id))
                except Exception as exc:
                    clip.status = VideoClipStatus.FAILED
                    clip.error_message = str(exc)[:2000]
            _update_progress(step, existing_clips)
            db.commit()
            if any(clip.status == VideoClipStatus.PENDING for clip in existing_clips):
                time.sleep(interval)

        failed_clips = [clip for clip in existing_clips if clip.status == VideoClipStatus.FAILED]
        step.output_payload = {
            "video_clip_ids": [clip.id for clip in existing_clips],
            "succeeded_clip_ids": [clip.id for clip in existing_clips if clip.status == VideoClipStatus.SUCCEEDED],
            "failed_clip_ids": [clip.id for clip in failed_clips],
        }
        step.status = RunStatus.FAILED if failed_clips else RunStatus.SUCCEEDED
        step.finished_at = utcnow()
        step.error_message = (
            f"{len(failed_clips)} 个视频片段生成失败；可在视频页面重做单组或重试工作流"
            if failed_clips
            else None
        )
        run.status = RunStatus.FAILED if failed_clips else RunStatus.SUCCEEDED
        run.finished_at = utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        run = db.get(WorkflowRun, run_id)
        if run is not None:
            step = db.scalars(
                select(WorkflowStep)
                .where(WorkflowStep.workflow_run_id == run_id)
                .order_by(WorkflowStep.position)
            ).first()
            run.status = RunStatus.FAILED
            run.finished_at = utcnow()
            if step is not None:
                step.status = RunStatus.FAILED
                step.error_message = str(exc)[:2000]
                step.finished_at = utcnow()
            db.commit()
    finally:
        db.close()


def list_video_clips(db: Session, project_id: str) -> list[VideoClip]:
    """按片段组、版本倒序返回历史，前端首项即为当前最新版本。"""

    get_project_or_404(db, project_id)
    statement = (
        select(VideoClip)
        .where(VideoClip.project_id == project_id)
        .order_by(VideoClip.group_number, VideoClip.version.desc())
    )
    return list(db.scalars(statement).all())
