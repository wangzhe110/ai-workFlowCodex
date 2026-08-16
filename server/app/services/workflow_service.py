"""视频分析工作流的领域规则和状态机实现。"""

from datetime import datetime, timedelta, timezone
import time
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import AssetKind, MediaAsset, Project, RunStatus, WorkflowRun, WorkflowStep
from app.services.analysis_provider import (
    MockVideoAnalysisProvider,
    OpenAICompatibleTranscriptionProvider,
    OpenAICompatibleVisionAnalysisProvider,
    VideoAnalysisInput,
    VideoAnalysisProvider,
)
from app.services.model_profile_service import get_active_profile_snapshot
from app.services.sensitive_data import sanitize_error_summary
from app.services.video_audio_service import extract_reference_audio
from app.services.video_frame_service import extract_sampled_video_frames


VIDEO_ANALYSIS_WORKFLOW = "video_analysis"
VIDEO_AUDIO_TRANSCRIPTION_STEP = "transcribe_reference_audio"
VIDEO_ANALYSIS_STEP = "analyze_reference_mechanisms"


def utcnow() -> datetime:
    """服务层统一的 UTC 时间来源。"""

    return datetime.now(timezone.utc)


def get_project_or_404(db: Session, project_id: str) -> Project:
    """读取项目；不存在时返回标准 404，避免各路由重复实现。"""

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    return project


def create_project(db: Session, title: str, description: Optional[str]) -> Project:
    """创建项目容器；素材和工作流必须属于已存在项目。"""

    project = Project(title=title.strip(), description=description.strip() if description else None)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def list_projects(db: Session) -> list[Project]:
    """按最近更新排序给项目列表页使用。"""

    return list(db.scalars(select(Project).order_by(Project.updated_at.desc())).all())


def add_source_video(
    db: Session,
    project_id: str,
    original_filename: str,
    content_type: str,
    byte_size: int,
    storage_key: str,
) -> MediaAsset:
    """登记已成功写入存储的原视频。

    先保存文件、后创建数据库记录；若写文件失败，数据库不会留下无效素材行。
    """

    get_project_or_404(db, project_id)
    asset = MediaAsset(
        project_id=project_id,
        kind=AssetKind.SOURCE_VIDEO,
        original_filename=original_filename[:255],
        content_type=content_type,
        byte_size=byte_size,
        storage_key=storage_key,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


def get_deletable_source_video(db: Session, project_id: str, asset_id: str) -> MediaAsset:
    """返回尚未被冻结为 V1 分析输入的源视频。

    上传列表允许制作人在分析前删掉误传素材；一旦素材 ID 已写进参考分析运行的
    ``input_snapshot``，它就是可复现历史的一部分，不能再物理删除。
    """

    asset = db.scalar(
        select(MediaAsset).where(
            MediaAsset.id == asset_id,
            MediaAsset.project_id == project_id,
            MediaAsset.kind == AssetKind.SOURCE_VIDEO,
        )
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="待删除的参考视频不存在")
    runs = db.scalars(
        select(WorkflowRun).where(
            WorkflowRun.project_id == project_id,
            WorkflowRun.workflow_key == "v1_reference_analysis",
        )
    ).all()
    for run in runs:
        context = (run.input_snapshot or {}).get("context")
        if isinstance(context, dict) and context.get("source_asset_id") == asset.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该视频已被冻结为分析任务输入，不能删除；历史任务会继续保留该素材",
            )
    return asset


def delete_source_video_record(db: Session, asset: MediaAsset) -> None:
    """删除通过冻结校验的素材数据库记录；对象删除由存储层在此之前完成。"""

    db.delete(asset)
    db.commit()


def _latest_source_video(db: Session, project_id: str) -> Optional[MediaAsset]:
    """仅供历史 ``video_analysis`` 工作流读取最近素材。

    LemonFlow V1 主流程绝不调用本函数：V1 参考视频分析必须由生产台提交并冻结
    ``source_asset_id``，不能根据上传时间猜测用户要分析哪一条视频。
    """

    statement = (
        select(MediaAsset)
        .where(MediaAsset.project_id == project_id, MediaAsset.kind == AssetKind.SOURCE_VIDEO)
        .order_by(MediaAsset.created_at.desc())
    )
    return db.scalars(statement).first()


def create_video_analysis_run(db: Session, project_id: str) -> WorkflowRun:
    """创建历史兼容的多模态分析运行及其两个可独立配置的步骤。

    该入口保留给旧项目，不是 LemonFlow V1 主生产链路。V1 使用
    ``create_v1_run(..., run_key='reference_analysis', source_asset_id=...)``，并且由
    用户明确勾选的素材 ID 驱动。历史工作流仍按旧行为读取最近素材以保持兼容。
    """

    get_project_or_404(db, project_id)
    source_video = _latest_source_video(db, project_id)
    if source_video is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="请先上传参考视频")

    run = WorkflowRun(project_id=project_id, workflow_key=VIDEO_ANALYSIS_WORKFLOW)
    transcription_step = WorkflowStep(
        workflow_run=run,
        step_key=VIDEO_AUDIO_TRANSCRIPTION_STEP,
        position=1,
        input_payload={"asset_id": source_video.id},
        model_profile_snapshot=get_active_profile_snapshot(db, VIDEO_AUDIO_TRANSCRIPTION_STEP),
    )
    analysis_step = WorkflowStep(
        workflow_run=run,
        step_key=VIDEO_ANALYSIS_STEP,
        position=2,
        input_payload={"asset_id": source_video.id},
        model_profile_snapshot=get_active_profile_snapshot(db, VIDEO_ANALYSIS_STEP),
    )
    db.add_all([run, transcription_step, analysis_step])
    db.commit()
    return get_workflow_run_or_404(db, run.id)


def get_workflow_run_or_404(db: Session, run_id: str) -> WorkflowRun:
    """读取包含步骤的运行记录，并解除已超过 Worker 上限的卡死状态。

    中转站提交请求存在“供应商已收到、客户端却超时”的不确定情况，因此系统不会
    自动重放任何模型调用。相反，当任务超过严格配置的 Worker 超时加缓冲时间时，
    它被标记为失败并保留错误原因，操作者确认后才能点击已有的“重试”按钮。这既
    防止页面永久转圈，也避免后台自动重复扣费或生成重复素材。
    """

    statement = select(WorkflowRun).options(selectinload(WorkflowRun.steps)).where(WorkflowRun.id == run_id)
    run = db.scalars(statement).first()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="工作流运行不存在")
    _mark_stale_run_failed(db, run)
    return run


def _mark_stale_run_failed(db: Session, run: WorkflowRun) -> None:
    """将超过安全阈值仍在运行的任务转为可人工复核的失败状态。

    只处理 ``RUNNING`` 状态，并只改变同样处于 ``RUNNING`` 的步骤；已成功的前置
    步骤保持原状，便于排查是哪个模型节点或 Worker 实例中断。SQLite 读取时间可能
    丢失时区，因此统一按 UTC 补齐后比较。
    """

    if run.status != RunStatus.RUNNING or run.started_at is None:
        return
    started_at = run.started_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    cutoff = utcnow() - timedelta(seconds=settings.workflow_stale_after_seconds)
    if started_at > cutoff:
        return

    now = utcnow()
    message = (
        f"任务超过 {settings.workflow_stale_after_seconds} 秒仍未结束，已停止等待。"
        "请检查 Worker、Redis 和模型平台后，再由人工确认是否重试。"
    )
    run.status = RunStatus.FAILED
    run.finished_at = now
    for step in run.steps:
        if step.status == RunStatus.RUNNING:
            step.status = RunStatus.FAILED
            step.error_message = message
            step.finished_at = now
    db.commit()


def _video_analysis_provider(snapshot: dict) -> VideoAnalysisProvider:
    """按本次运行冻结的配置选择视频分析适配器。"""

    provider_key = snapshot.get("provider_key")
    if provider_key == "mock_provider":
        return MockVideoAnalysisProvider()
    if provider_key == "openai_compatible_vision":
        return OpenAICompatibleVisionAnalysisProvider(snapshot)
    raise RuntimeError(f"视频分析步骤没有可执行的供应商适配器：{provider_key or '未配置'}")


def _frame_extraction_settings(snapshot: dict) -> tuple[int, float, int]:
    """读取已经过模型配置门禁校验的抽帧参数，并提供运行时兜底。"""

    config = snapshot.get("provider_config") or {}
    frame_count = config.get("frame_sample_count", 6)
    timeout_seconds = config.get("frame_extraction_timeout_seconds", 120)
    max_frame_bytes = config.get("frame_max_bytes", 2 * 1024 * 1024)
    if not isinstance(frame_count, int) or not 1 <= frame_count <= 12:
        raise RuntimeError("frame_sample_count 必须是 1 至 12 的整数")
    if not isinstance(timeout_seconds, (int, float)) or not 5 <= float(timeout_seconds) <= 300:
        raise RuntimeError("frame_extraction_timeout_seconds 必须在 5 至 300 秒之间")
    if not isinstance(max_frame_bytes, int) or not 64 * 1024 <= max_frame_bytes <= 8 * 1024 * 1024:
        raise RuntimeError("frame_max_bytes 必须在 64KB 至 8MB 之间")
    return frame_count, float(timeout_seconds), max_frame_bytes


def _audio_extraction_settings(snapshot: dict) -> tuple[int, float, int]:
    """读取并二次约束音频提取预算，防止异常配置撑大转写请求体。"""

    config = snapshot.get("provider_config") or {}
    max_duration_seconds = config.get("audio_max_duration_seconds", 180)
    timeout_seconds = config.get("audio_extraction_timeout_seconds", 120)
    max_audio_bytes = config.get("audio_max_bytes", 8 * 1024 * 1024)
    if not isinstance(max_duration_seconds, int) or not 5 <= max_duration_seconds <= 600:
        raise RuntimeError("audio_max_duration_seconds 必须是 5 至 600 的整数")
    if not isinstance(timeout_seconds, (int, float)) or not 5 <= float(timeout_seconds) <= 300:
        raise RuntimeError("audio_extraction_timeout_seconds 必须在 5 至 300 秒之间")
    if not isinstance(max_audio_bytes, int) or not 64 * 1024 <= max_audio_bytes <= 50 * 1024 * 1024:
        raise RuntimeError("audio_max_bytes 必须在 64KB 至 50MB 之间")
    return max_duration_seconds, float(timeout_seconds), max_audio_bytes


def _step_by_key(run: WorkflowRun, step_key: str) -> WorkflowStep:
    """按业务键而非数组下标获取步骤，支持一个运行中存在多个节点。"""

    for step in run.steps:
        if step.step_key == step_key:
            return step
    raise RuntimeError(f"分析工作流缺少步骤：{step_key}")


def _start_step(db: Session, step: WorkflowStep, progress: int = 5) -> None:
    """统一启动步骤状态，避免每个模型节点各自遗漏审计字段。"""

    step.status = RunStatus.RUNNING
    step.started_at = utcnow()
    step.finished_at = None
    step.error_message = None
    step.attempt += 1
    step.progress = progress
    db.commit()


def _complete_step(db: Session, step: WorkflowStep, output_payload: dict) -> None:
    """以统一方式写入非敏感输出并结束步骤。"""

    step.output_payload = output_payload
    step.progress = 100
    step.status = RunStatus.SUCCEEDED
    step.finished_at = utcnow()
    db.commit()


def execute_video_analysis(run_id: str) -> None:
    """由 Worker 调用的实际执行函数。

    函数自行创建数据库 Session，绝不能复用 HTTP 请求的 Session；生产环境中它会
    在独立进程执行。所有预期异常都会持久化为 FAILED 状态，前端无需解析服务器日志。
    """

    db = SessionLocal()
    active_step_id: Optional[str] = None
    try:
        run = get_workflow_run_or_404(db, run_id)
        transcription_step = _step_by_key(run, VIDEO_AUDIO_TRANSCRIPTION_STEP)
        analysis_step = _step_by_key(run, VIDEO_ANALYSIS_STEP)

        # 幂等保护：重复投递已完成的任务不能覆盖旧结果，也不能重复消耗模型额度。
        if run.status == RunStatus.SUCCEEDED:
            return
        if run.status == RunStatus.RUNNING:
            return

        run.status = RunStatus.RUNNING
        run.started_at = utcnow()
        db.commit()

        asset_id = transcription_step.input_payload["asset_id"]
        asset = db.get(MediaAsset, asset_id)
        if asset is None:
            raise RuntimeError("分析素材不存在或已被删除")

        # 节点 1：真实 ASR 时只保留内存转写；模拟模式明确标注跳过，避免把示例
        # 内容误当真实来源。无音轨属于可降级情况，画面分析仍可继续。
        active_step_id = transcription_step.id
        _start_step(db, transcription_step)
        transcript: Optional[str] = None
        transcription_snapshot = transcription_step.model_profile_snapshot or {}
        if transcription_snapshot.get("provider_key") == "mock_provider":
            time.sleep(settings.simulated_step_delay_seconds)
            _complete_step(
                db,
                transcription_step,
                {
                    "mode": "mock_skipped",
                    "transcript_persisted": False,
                    "note": "本地模拟模式未读取或保存参考视频音轨。",
                },
            )
        else:
            max_audio_seconds, audio_timeout, max_audio_bytes = _audio_extraction_settings(transcription_snapshot)
            transcription_step.progress = 20
            db.commit()
            audio = extract_reference_audio(
                asset.storage_key,
                max_duration_seconds=max_audio_seconds,
                timeout_seconds=audio_timeout,
                max_audio_bytes=max_audio_bytes,
            )
            transcription_step.progress = 65
            db.commit()
            transcription = OpenAICompatibleTranscriptionProvider(transcription_snapshot).transcribe(audio)
            transcript = transcription.text or None
            _complete_step(
                db,
                transcription_step,
                {
                    "mode": "transcribed_in_memory",
                    "audio_seconds_budget": transcription.audio_seconds,
                    "has_speech": bool(transcript),
                    "transcript_persisted": False,
                },
            )

        # 节点 2：视觉/综合模型只返回抽象机制，不会把转写文本写进最终结果。
        active_step_id = analysis_step.id
        _start_step(db, analysis_step)
        snapshot = analysis_step.model_profile_snapshot or {}
        provider = _video_analysis_provider(snapshot)
        sampled_frames = []
        if snapshot.get("provider_key") == "mock_provider":
            # 保持无密钥开发模式的进度体验；不要求本机安装 FFmpeg 或调用真实模型。
            for progress in (20, 45, 70):
                time.sleep(settings.simulated_step_delay_seconds)
                analysis_step.progress = progress
                db.commit()
        else:
            frame_count, extraction_timeout, max_frame_bytes = _frame_extraction_settings(snapshot)
            analysis_step.progress = 20
            db.commit()
            sampled_frames = extract_sampled_video_frames(
                asset.storage_key,
                frame_count=frame_count,
                timeout_seconds=extraction_timeout,
                max_frame_bytes=max_frame_bytes,
            )
            analysis_step.progress = 55
            db.commit()

        result = provider.analyze(
            VideoAnalysisInput(
                asset_id=asset.id,
                filename=asset.original_filename,
                content_type=asset.content_type,
                sampled_frames=sampled_frames,
                transcript_for_mechanism_analysis=transcript,
            )
        )
        _complete_step(db, analysis_step, result)
        run.status = RunStatus.SUCCEEDED
        run.finished_at = utcnow()
        db.commit()
    except Exception as exc:  # Worker 必须把失败转换为用户可见的状态。
        db.rollback()
        error_message = sanitize_error_summary(exc, max_length=2000)
        run = db.get(WorkflowRun, run_id)
        if run is not None:
            step = db.get(WorkflowStep, active_step_id) if active_step_id else None
            run.status = RunStatus.FAILED
            run.finished_at = utcnow()
            if step is not None:
                step.status = RunStatus.FAILED
                step.error_message = error_message
                step.finished_at = utcnow()
            db.commit()
    finally:
        db.close()


def retry_workflow_run(db: Session, run_id: str) -> WorkflowRun:
    """将任意失败运行复位为待执行状态。

    重试保留 `attempt` 计数和旧失败原因，便于后续审计与模型稳定性评估。
    """

    run = get_workflow_run_or_404(db, run_id)
    if run.status not in (RunStatus.FAILED, RunStatus.CANCELLED):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="只有失败或取消的任务可以重试")
    steps = list(run.steps)
    run.status = RunStatus.PENDING
    run.started_at = None
    run.finished_at = None
    for step in steps:
        step.status = RunStatus.PENDING
        step.progress = 0
        step.started_at = None
        step.finished_at = None
        step.error_message = None
        step.output_payload = None
    db.commit()
    return get_workflow_run_or_404(db, run_id)
