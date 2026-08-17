"""Commerce Phase 2 控制面：路由只解析契约并委托状态服务。"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import ModelInvocation, StoryRunMode, StoryRunStage, StoryRunStatus, WorkflowRun, WorkflowStep
from app.schemas import (
    CommerceOutlineCreateRequest,
    CommerceOutlinePatchRequest,
    CommerceOutlineResponse,
    CommerceReviewRequest,
    CommerceReviewResponse,
    CommerceStoryRunCreateRequest,
    CommerceStoryRunResponse,
    CommerceWorkflowDefinitionResponse,
    CommerceWorkflowRunResponse,
    CommerceWorkflowStepResponse,
    CommerceFinalVideoResponse,
    CommerceProductionActionRequest,
    CommerceProductionAssetsResponse,
    CommerceProductionImageResponse,
    CommerceInternalSmokeBootstrapResponse,
    CommerceInternalSmokeConfirmRequest,
    CommerceInternalSmokeVideoPromptRequest,
    CommerceInternalSmokeVideoPromptResponse,
    CommerceProductionReviewRequest,
    CommerceProductionVersionResponse,
    CommerceVideoClipResponse,
    CommerceVideoPromptResponse,
)
from app.services.commerce_configuration_service import ensure_commerce_foundation
from app.services.commerce_workflow_service import (
    cancel_story_run,
    continue_story_run,
    create_manual_outline,
    create_next_story_run,
    get_story_run,
    list_project_story_runs,
    outlines_for_story_run,
    patch_manual_outline,
    pause_story_run,
    resume_story_run,
    retry_step,
    review_stage,
    rerun_story_run,
    reviews_for_story_run,
    start_story_run,
    workflow_for_story_run,
)
from app.services.worker_runtime import dispatch_workflow
from app.services.commerce_production_service import (
    create_production_run,
    list_story_run_assets,
    lock_character_design,
    lock_image,
    lock_scene_design,
    lock_storyboard,
    resume_video_clip_provider_task,
    review_video_clip,
)
from app.services.commerce_internal_smoke_service import (
    bootstrap_internal_smoke,
    create_internal_smoke_video_prompt,
)
from app.services.storage import local_asset_storage
from app.services.sensitive_data import redact_sensitive_data, sanitize_error_summary


router = APIRouter(prefix="/api/v1/commerce", tags=["Commerce 带货短剧"])


# 供应商生成 URL 常带时效性签名。它们只能在 Worker 下载阶段短暂使用，不能成为
# 正常用户接口的媒体地址或错误摘要的一部分。
_SIGNED_MEDIA_QUERY_KEYS = frozenset(
    {"signature", "sig", "token", "access_token", "credential", "expires", "policy"}
)
_SIGNED_MEDIA_URL = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)
_PROVIDER_ACCOUNT_REFERENCE = re.compile(r"(?i)\b(?:your\s+)?account\s+\d+")
_PROVIDER_REQUEST_REFERENCE = re.compile(r"(?i)\brequest\s+id\s*:\s*[a-z0-9_-]+")


def _safe_clip_media_url(value: object) -> str | None:
    """只对页面公开持久化本地媒体或无签名 HTTPS 媒体。"""

    if not isinstance(value, str) or not value:
        return None
    if value.startswith("/media/generated/"):
        return value
    # Mock URL 仅用于既有自动化测试；前端安全加载器仍会拒绝它，不能当作真实媒体展示。
    if value.startswith("mock://"):
        return value
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    query_names = {name.casefold() for name, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    if any(name in _SIGNED_MEDIA_QUERY_KEYS or name.startswith("x-amz-") for name in query_names):
        return None
    return value


def _safe_clip_error_summary(value: object) -> str:
    """保留可行动的错误摘要，但不回显供应商 URL、账户或请求追踪标识。"""

    sanitized = sanitize_error_summary(value, max_length=500)
    sanitized = _SIGNED_MEDIA_URL.sub("[链接已隐藏]", sanitized)
    sanitized = _PROVIDER_ACCOUNT_REFERENCE.sub("当前账户", sanitized)
    return _PROVIDER_REQUEST_REFERENCE.sub("供应商请求标识已隐藏", sanitized)


def _safe_snapshot(value):
    """审核页只能看到安全快照，历史异常 Data URL 也不得被接口回显。"""

    redacted = redact_sensitive_data(value or {})
    return redacted if isinstance(redacted, dict) else {}


def _production_version_response(item) -> CommerceProductionVersionResponse:
    return CommerceProductionVersionResponse(
        id=item.id, story_run_id=item.story_run_id, version=item.version, status=item.status,
        content=_safe_snapshot(item.content), input_snapshot=_safe_snapshot(item.input_snapshot), prompt_snapshot=_safe_snapshot(item.prompt_snapshot),
        locked_at=item.locked_at, stale_at=item.stale_at, created_at=item.created_at,
    )


def _production_image_response(item) -> CommerceProductionImageResponse:
    owner = getattr(item, "character_design_version_id", None) or getattr(item, "scene_design_version_id", None) or getattr(item, "storyboard_version_id", None)
    logical = getattr(item, "role_id", None) or getattr(item, "scene_id", None) or getattr(item, "shot_id", None)
    return CommerceProductionImageResponse(
        id=item.id, story_run_id=item.story_run_id, owner_version_id=owner, logical_id=logical,
        version=item.version, image_url=item.image_url, status=item.status, prompt_snapshot=item.prompt_snapshot,
        input_snapshot=_safe_snapshot(getattr(item, "input_snapshot", None) or getattr(item, "input_asset_snapshot", {})),
        error_message=item.error_message, locked_at=item.locked_at, stale_at=item.stale_at, created_at=item.created_at,
    )


def _video_prompt_response(item) -> CommerceVideoPromptResponse:
    return CommerceVideoPromptResponse(
        id=item.id, story_run_id=item.story_run_id, storyboard_version_id=item.storyboard_version_id,
        shot_id=item.shot_id, shot_number=item.shot_number, keyframe_version_id=item.keyframe_version_id,
        version=item.version, prompt=item.prompt, trace=_safe_snapshot(item.trace), status=item.status,
        locked_at=item.locked_at, stale_at=item.stale_at, created_at=item.created_at,
    )


def _clip_response(item, db: Session) -> CommerceVideoClipResponse:
    """编码视频片段的普通用户视图，不让页面重建恢复资格或读取供应商私有字段。"""

    invocation = db.get(ModelInvocation, item.model_invocation_id) if item.model_invocation_id else None
    file_size_bytes: int | None = None
    if isinstance(item.video_url, str) and item.video_url.startswith("/media/generated/"):
        try:
            file_size_bytes = local_asset_storage.generated_media_path(item.video_url).stat().st_size
        except (OSError, RuntimeError):
            # 已删除或尚未落盘的历史文件只显示“不可用”，不能因为只读展示接口报 500。
            file_size_bytes = None
    can_resume_provider_task = bool(
        item.status == "FAILED"
        and item.provider_task_id
        and not item.video_url
        and item.workflow_run_id
        and item.model_invocation_id
    )
    safe_video_url = _safe_clip_media_url(item.video_url)
    return CommerceVideoClipResponse(
        id=item.id, story_run_id=item.story_run_id, storyboard_version_id=item.storyboard_version_id,
        shot_id=item.shot_id, shot_number=item.shot_number, keyframe_version_id=item.keyframe_version_id,
        video_prompt_version_id=item.video_prompt_version_id, version=item.version, provider_task_id=item.provider_task_id,
        video_url=safe_video_url, status=item.status,
        can_resume_provider_task=can_resume_provider_task,
        error_code=invocation.error_code if invocation is not None else None,
        error_message=_safe_clip_error_summary(item.error_message) if item.error_message else None,
        retry_count=item.retry_count, duration_ms=item.duration_ms, file_size_bytes=file_size_bytes,
        media_metadata=_safe_snapshot(item.media_metadata), reviewed_at=item.reviewed_at,
        review_note=item.review_note, stale_at=item.stale_at, created_at=item.created_at, finished_at=item.finished_at,
    )


def _final_response(item) -> CommerceFinalVideoResponse:
    download_url = item.output_url if item.output_url and item.output_url.startswith("https://") else (
        f"/api/v1/commerce/story-runs/{item.story_run_id}/final-videos/{item.id}/download"
        if item.storage_key and item.status == "SUCCEEDED" else None
    )
    return CommerceFinalVideoResponse(
        id=item.id, story_run_id=item.story_run_id, storyboard_version_id=item.storyboard_version_id,
        version=item.version, clip_ids=item.clip_ids, output_url=item.output_url, download_url=download_url,
        status=item.status, error_message=item.error_message, media_metadata=item.media_metadata,
        stale_at=item.stale_at, created_at=item.created_at, finished_at=item.finished_at,
    )


def _dispatch_or_service_unavailable(background_tasks: BackgroundTasks, run: WorkflowRun) -> None:
    """投递发生在状态已提交之后；失败时返回可理解、可重试的服务错误。

    ``dispatch_workflow`` 会把父运行及当前 attempt 标记为 ``FAILED``，从而保留
    前端的 retry 入口。路由层不泄露 Redis/RQ 的内部异常，也不能把可恢复的投递
    问题伪装为未处理的 HTTP 500。
    """

    try:
        dispatch_workflow(background_tasks, run.workflow_key, run.id)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Commerce 任务暂时无法投递；任务已标记为失败，可在恢复后重试",
        ) from exc


def _step_response(step: WorkflowStep | None) -> CommerceWorkflowStepResponse | None:
    if step is None:
        return None
    return CommerceWorkflowStepResponse(
        id=step.id, step_key=step.step_key, position=step.position, status=step.status.value,
        attempt=step.attempt, progress=step.progress, output_payload=step.output_payload,
        error_message=step.error_message, provider_task_id=step.provider_task_id,
        created_at=step.created_at, started_at=step.started_at, finished_at=step.finished_at,
    )


def _workflow_response(run: WorkflowRun | None) -> CommerceWorkflowRunResponse | None:
    if run is None:
        return None
    # Slice 2 媒体子任务不使用 Phase 2 的 CommerceWorkflowLink sidecar（后者只
    # 表示唯一的 commerce_story_run 父运行），但其创建时冻结的上下文仍是 API
    # 返回中可信的 StoryRun 归属来源，避免前端需要从项目级状态反推。
    production_context = ((run.input_snapshot or {}).get("commerce_production") or {})
    frozen_story_run_id = production_context.get("story_run_id") if isinstance(production_context, dict) else None
    return CommerceWorkflowRunResponse(
        id=run.id, story_run_id=run.commerce_link.story_run_id if run.commerce_link else frozen_story_run_id,
        project_id=run.project_id, workflow_key=run.workflow_key,
        workflow_definition_id=run.workflow_definition_id, workflow_version=run.workflow_version,
        status=run.status.value, idempotency_key=run.idempotency_key, input_snapshot=_safe_snapshot(run.input_snapshot),
        created_at=run.created_at, started_at=run.started_at, finished_at=run.finished_at,
        steps=[_step_response(step) for step in run.steps if _step_response(step) is not None],
    )


def _outline_response(outline) -> CommerceOutlineResponse:
    return CommerceOutlineResponse(
        id=outline.id, story_run_id=outline.story_run_id, version=outline.version, title=outline.title,
        premise=outline.premise, story_beats=outline.story_beats,
        product_placement_strategy=outline.product_placement_strategy, status=outline.status.value,
        created_at=outline.created_at,
    )


def _review_response(item) -> CommerceReviewResponse:
    return CommerceReviewResponse(
        id=item.id, project_id=item.project_id, target_type=item.target_type, target_id=item.target_id,
        decision=item.decision, reviewer_label=item.reviewer_label, note=item.note,
        quality_score=item.quality_score, created_at=item.created_at,
    )


def _story_run_response(db: Session, story_run) -> CommerceStoryRunResponse:
    runs = workflow_for_story_run(db, story_run.id)
    active = next((item for item in runs if item.status.value in {"PENDING", "RUNNING"}), None)
    current = active or (runs[-1] if runs else None)
    current_step = next((step for step in (current.steps if current else []) if step.status.value in {"PENDING", "RUNNING"}), None)
    if current_step is None and current is not None and current.steps:
        current_step = current.steps[-1]
    latest_error = next((step.error_message for run in reversed(runs) for step in reversed(run.steps) if step.error_message), None)
    refs: dict[str, Any] = {}
    for run in runs:
        for step in run.steps:
            artifacts = (step.output_payload or {}).get("artifact_references")
            if isinstance(artifacts, dict):
                refs[step.step_key] = artifacts
    state = story_run.state
    mainline_snapshot = (story_run.mainline_input.input_snapshot if story_run.mainline_input else {}) or {}
    rerun_metadata = mainline_snapshot.get("rerun") if isinstance(mainline_snapshot, dict) else {}
    source_story_run_id = rerun_metadata.get("source_story_run_id") if isinstance(rerun_metadata, dict) else None
    parent_workflow_run_id = runs[0].id if runs else None
    return CommerceStoryRunResponse(
        id=story_run.id, project_id=story_run.project_id, topic_candidate_id=story_run.topic_candidate_id,
        creative_idea_id=story_run.mainline_input.creative_idea_id if story_run.mainline_input else None,
        workflow_run_id=parent_workflow_run_id,
        source_story_run_id=source_story_run_id if isinstance(source_story_run_id, str) else None,
        project_product_selection_id=story_run.project_product_selection_id,
        product_asset_version_id=story_run.product_asset_version_id, run_number=story_run.run_number,
        mode=story_run.mode.value, current_stage=state.current_stage.value, current_status=state.status.value,
        blocked_reason=(state.stage_data or {}).get("blocked_reason"),
        can_start=state.current_stage == StoryRunStage.TOPIC and state.status == StoryRunStatus.PENDING,
        can_continue=state.status == StoryRunStatus.PENDING and state.current_stage not in {StoryRunStage.TOPIC, StoryRunStage.COMPLETED},
        # STEPWISE 的非强制闸门同样允许人工确认，只是确认后仍需显式 continue；
        # manual_pause 则没有可审核结果，不能错误展示确认按钮。
        can_confirm=state.status == StoryRunStatus.PAUSED and (state.stage_data or {}).get("blocked_reason") in {"awaiting_review", "awaiting_continue"},
        current_workflow_run=_workflow_response(current), current_workflow_step=_step_response(current_step),
        latest_error=latest_error, stage_result_references=refs,
        created_at=story_run.created_at, updated_at=story_run.updated_at,
    )


def _stage(raw: str) -> StoryRunStage:
    try:
        return StoryRunStage(raw.strip().upper())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="不支持的 Commerce 阶段") from exc


@router.get("/workflow-definition", response_model=CommerceWorkflowDefinitionResponse)
def commerce_workflow_definition(db: Session = Depends(get_db)) -> CommerceWorkflowDefinitionResponse:
    definition = ensure_commerce_foundation(db)
    return CommerceWorkflowDefinitionResponse(id=definition.id, workflow_code=definition.workflow_code, version=definition.version, definition_json=definition.definition_json, status=definition.status.value, published_at=definition.published_at)


@router.post("/projects/{project_id}/story-runs", response_model=CommerceStoryRunResponse, status_code=status.HTTP_201_CREATED)
def create_story_run_endpoint(project_id: str, payload: CommerceStoryRunCreateRequest, db: Session = Depends(get_db)) -> CommerceStoryRunResponse:
    try:
        mode = StoryRunMode(payload.mode)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="运行模式仅支持 STEPWISE 或 AUTO") from exc
    return _story_run_response(db, create_next_story_run(db, project_id=project_id, topic_candidate_id=payload.topic_candidate_id, project_product_selection_id=payload.project_product_selection_id, mode=mode))


@router.get("/projects/{project_id}/story-runs", response_model=list[CommerceStoryRunResponse])
def list_story_runs_endpoint(project_id: str, db: Session = Depends(get_db)) -> list[CommerceStoryRunResponse]:
    return [_story_run_response(db, item) for item in list_project_story_runs(db, project_id)]


@router.get("/story-runs/{story_run_id}", response_model=CommerceStoryRunResponse)
def get_story_run_endpoint(story_run_id: str, db: Session = Depends(get_db)) -> CommerceStoryRunResponse:
    return _story_run_response(db, get_story_run(db, story_run_id))


@router.post("/story-runs/{story_run_id}/rerun", response_model=CommerceStoryRunResponse, status_code=status.HTTP_201_CREATED)
def rerun_story_run_endpoint(story_run_id: str, db: Session = Depends(get_db)) -> CommerceStoryRunResponse:
    """从已选创意的冻结输入创建独立新 Run；不启动或投递任何模型任务。"""

    story_run, _workflow_run = rerun_story_run(db, source_story_run_id=story_run_id)
    return _story_run_response(db, story_run)


@router.post("/story-runs/{story_run_id}/start", response_model=CommerceStoryRunResponse, status_code=status.HTTP_202_ACCEPTED)
def start_story_run_endpoint(story_run_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> CommerceStoryRunResponse:
    story_run, run, created = start_story_run(db, story_run_id)
    if created:
        _dispatch_or_service_unavailable(background_tasks, run)
    return _story_run_response(db, story_run)


@router.post("/story-runs/{story_run_id}/continue", response_model=CommerceStoryRunResponse, status_code=status.HTTP_202_ACCEPTED)
def continue_story_run_endpoint(story_run_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> CommerceStoryRunResponse:
    story_run, run, created = continue_story_run(db, story_run_id)
    if created:
        _dispatch_or_service_unavailable(background_tasks, run)
    return _story_run_response(db, story_run)


@router.post("/story-runs/{story_run_id}/pause", response_model=CommerceStoryRunResponse)
def pause_story_run_endpoint(story_run_id: str, db: Session = Depends(get_db)) -> CommerceStoryRunResponse:
    return _story_run_response(db, pause_story_run(db, story_run_id))


@router.post("/story-runs/{story_run_id}/resume", response_model=CommerceStoryRunResponse)
def resume_story_run_endpoint(story_run_id: str, db: Session = Depends(get_db)) -> CommerceStoryRunResponse:
    return _story_run_response(db, resume_story_run(db, story_run_id))


@router.post("/story-runs/{story_run_id}/cancel", response_model=CommerceStoryRunResponse)
def cancel_story_run_endpoint(story_run_id: str, db: Session = Depends(get_db)) -> CommerceStoryRunResponse:
    return _story_run_response(db, cancel_story_run(db, story_run_id))


def _review_endpoint(story_run_id: str, stage: str, payload: CommerceReviewRequest, decision: str, background_tasks: BackgroundTasks, db: Session) -> CommerceStoryRunResponse:
    story_run, should_dispatch = review_stage(db, story_run_id=story_run_id, stage=_stage(stage), decision=decision, reviewer_label=payload.reviewer_label, note=payload.note, quality_score=payload.quality_score, outline_id=payload.outline_id)
    if should_dispatch:
        story_run, run, created = continue_story_run(db, story_run.id)
        if created:
            _dispatch_or_service_unavailable(background_tasks, run)
    return _story_run_response(db, story_run)


@router.post("/story-runs/{story_run_id}/stages/{stage}/confirm", response_model=CommerceStoryRunResponse, status_code=status.HTTP_202_ACCEPTED)
def confirm_stage_endpoint(story_run_id: str, stage: str, payload: CommerceReviewRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> CommerceStoryRunResponse:
    return _review_endpoint(story_run_id, stage, payload, "CONFIRMED", background_tasks, db)


@router.post("/story-runs/{story_run_id}/stages/{stage}/reject", response_model=CommerceStoryRunResponse)
def reject_stage_endpoint(story_run_id: str, stage: str, payload: CommerceReviewRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> CommerceStoryRunResponse:
    return _review_endpoint(story_run_id, stage, payload, "REJECTED", background_tasks, db)


@router.post("/story-runs/{story_run_id}/steps/{step_id}/retry", response_model=CommerceStoryRunResponse, status_code=status.HTTP_202_ACCEPTED)
def retry_step_endpoint(story_run_id: str, step_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> CommerceStoryRunResponse:
    story_run, run, created = retry_step(db, story_run_id, step_id)
    if created:
        _dispatch_or_service_unavailable(background_tasks, run)
    return _story_run_response(db, story_run)


@router.get("/story-runs/{story_run_id}/workflow", response_model=list[CommerceWorkflowRunResponse])
def workflow_endpoint(story_run_id: str, db: Session = Depends(get_db)) -> list[CommerceWorkflowRunResponse]:
    return [_workflow_response(item) for item in workflow_for_story_run(db, story_run_id)]


@router.get("/story-runs/{story_run_id}/reviews", response_model=list[CommerceReviewResponse])
def reviews_endpoint(story_run_id: str, db: Session = Depends(get_db)) -> list[CommerceReviewResponse]:
    return [_review_response(item) for item in reviews_for_story_run(db, story_run_id)]


@router.get("/story-runs/{story_run_id}/outlines", response_model=list[CommerceOutlineResponse])
def outlines_endpoint(story_run_id: str, db: Session = Depends(get_db)) -> list[CommerceOutlineResponse]:
    return [_outline_response(item) for item in outlines_for_story_run(db, story_run_id)]


@router.post("/story-runs/{story_run_id}/outlines", response_model=CommerceOutlineResponse, status_code=status.HTTP_201_CREATED)
def create_outline_endpoint(story_run_id: str, payload: CommerceOutlineCreateRequest, db: Session = Depends(get_db)) -> CommerceOutlineResponse:
    return _outline_response(create_manual_outline(db, story_run_id, payload.model_dump()))


@router.patch("/story-runs/{story_run_id}/outlines/{outline_id}", response_model=CommerceOutlineResponse)
def patch_outline_endpoint(story_run_id: str, outline_id: str, payload: CommerceOutlinePatchRequest, db: Session = Depends(get_db)) -> CommerceOutlineResponse:
    changes = {key: value for key, value in payload.model_dump().items() if value is not None}
    if not changes:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="至少提供一个可修改字段")
    return _outline_response(patch_manual_outline(db, story_run_id, outline_id, changes))


# ---------------------------------------------------------------------------
# Commerce Slice 2 生产台：所有状态写入均委托服务层；路由只做请求解析、投递和
# 响应编码。这样浏览器不能跳过冻结输入、人工锁定或单镜头归属校验。
# ---------------------------------------------------------------------------


@router.get("/story-runs/{story_run_id}/production-assets", response_model=CommerceProductionAssetsResponse)
def production_assets_endpoint(story_run_id: str, db: Session = Depends(get_db)) -> CommerceProductionAssetsResponse:
    rows = list_story_run_assets(db, story_run_id)
    return CommerceProductionAssetsResponse(
        story_run_id=story_run_id,
        character_designs=[_production_version_response(item) for item in rows["character_designs"]],
        scene_designs=[_production_version_response(item) for item in rows["scene_designs"]],
        storyboards=[_production_version_response(item) for item in rows["storyboards"]],
        character_images=[_production_image_response(item) for item in rows["character_images"]],
        scene_images=[_production_image_response(item) for item in rows["scene_images"]],
        keyframes=[_production_image_response(item) for item in rows["keyframes"]],
        video_prompts=[_video_prompt_response(item) for item in rows["video_prompts"]],
        clips=[_clip_response(item, db) for item in rows["clips"]],
        finals=[_final_response(item) for item in rows["finals"]],
    )


@router.post(
    "/story-runs/{story_run_id}/internal-smoke/bootstrap",
    response_model=CommerceInternalSmokeBootstrapResponse,
    status_code=status.HTTP_201_CREATED,
)
def bootstrap_internal_smoke_endpoint(
    story_run_id: str, payload: CommerceInternalSmokeConfirmRequest, db: Session = Depends(get_db)
) -> CommerceInternalSmokeBootstrapResponse:
    """建立固定非真人内部验收输入；不读取模型配置也不投递 Worker。"""

    return CommerceInternalSmokeBootstrapResponse(
        story_run_id=story_run_id,
        **bootstrap_internal_smoke(db, story_run_id=story_run_id, confirm=payload.confirm),
    )


@router.post(
    "/story-runs/{story_run_id}/internal-smoke/video-prompt",
    response_model=CommerceInternalSmokeVideoPromptResponse,
    status_code=status.HTTP_201_CREATED,
)
def internal_smoke_video_prompt_endpoint(
    story_run_id: str, payload: CommerceInternalSmokeVideoPromptRequest, db: Session = Depends(get_db)
) -> CommerceInternalSmokeVideoPromptResponse:
    """只为已锁定的内部关键帧创建固定 Prompt；不调用导演文本模型。"""

    return CommerceInternalSmokeVideoPromptResponse(
        story_run_id=story_run_id,
        **create_internal_smoke_video_prompt(
            db, story_run_id=story_run_id, keyframe_id=payload.keyframe_id, confirm=payload.confirm
        ),
    )


@router.post("/story-runs/{story_run_id}/production/{operation}", response_model=CommerceWorkflowRunResponse, status_code=status.HTTP_202_ACCEPTED)
def create_production_action_endpoint(
    story_run_id: str,
    operation: str,
    payload: CommerceProductionActionRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> CommerceWorkflowRunResponse:
    run, created = create_production_run(
        db, story_run_id=story_run_id, operation=operation, target_id=payload.target_id, retry=payload.retry
    )
    if created:
        _dispatch_or_service_unavailable(background_tasks, run)
    return _workflow_response(run)


@router.post(
    "/story-runs/{story_run_id}/clips/{clip_id}/resume-provider-task",
    response_model=CommerceWorkflowRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def resume_video_clip_provider_task_endpoint(
    story_run_id: str,
    clip_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> CommerceWorkflowRunResponse:
    """只恢复一个已失败、已有供应商任务号的片段；服务端不接受外部 task ID。"""

    run, created = resume_video_clip_provider_task(
        db, story_run_id=story_run_id, source_clip_id=clip_id
    )
    if created:
        _dispatch_or_service_unavailable(background_tasks, run)
    return _workflow_response(run)


@router.post("/story-runs/{story_run_id}/character-designs/{version_id}/lock", response_model=CommerceProductionVersionResponse)
def lock_character_design_endpoint(story_run_id: str, version_id: str, payload: CommerceProductionReviewRequest, db: Session = Depends(get_db)) -> CommerceProductionVersionResponse:
    return _production_version_response(lock_character_design(db, story_run_id=story_run_id, version_id=version_id, reviewer_label=payload.reviewer_label, note=payload.note))


@router.post("/story-runs/{story_run_id}/scene-designs/{version_id}/lock", response_model=CommerceProductionVersionResponse)
def lock_scene_design_endpoint(story_run_id: str, version_id: str, payload: CommerceProductionReviewRequest, db: Session = Depends(get_db)) -> CommerceProductionVersionResponse:
    return _production_version_response(lock_scene_design(db, story_run_id=story_run_id, version_id=version_id, reviewer_label=payload.reviewer_label, note=payload.note))


@router.post("/story-runs/{story_run_id}/storyboards/{version_id}/lock", response_model=CommerceProductionVersionResponse)
def lock_storyboard_endpoint(story_run_id: str, version_id: str, payload: CommerceProductionReviewRequest, db: Session = Depends(get_db)) -> CommerceProductionVersionResponse:
    return _production_version_response(lock_storyboard(db, story_run_id=story_run_id, version_id=version_id, reviewer_label=payload.reviewer_label, note=payload.note))


@router.post("/story-runs/{story_run_id}/images/{image_kind}/{image_id}/lock", response_model=CommerceProductionImageResponse)
def lock_production_image_endpoint(story_run_id: str, image_kind: str, image_id: str, payload: CommerceProductionReviewRequest, db: Session = Depends(get_db)) -> CommerceProductionImageResponse:
    return _production_image_response(lock_image(db, story_run_id=story_run_id, image_id=image_id, kind=image_kind.upper(), reviewer_label=payload.reviewer_label, note=payload.note))


@router.post("/story-runs/{story_run_id}/clips/{clip_id}/review", response_model=CommerceVideoClipResponse)
def review_production_clip_endpoint(story_run_id: str, clip_id: str, decision: str, payload: CommerceProductionReviewRequest, db: Session = Depends(get_db)) -> CommerceVideoClipResponse:
    return _clip_response(review_video_clip(db, story_run_id=story_run_id, clip_id=clip_id, decision=decision, reviewer_label=payload.reviewer_label, note=payload.note), db)


@router.get("/story-runs/{story_run_id}/final-videos/{final_id}/download")
def download_commerce_final_video_endpoint(story_run_id: str, final_id: str, db: Session = Depends(get_db)) -> FileResponse:
    rows = list_story_run_assets(db, story_run_id)
    row = next((item for item in rows["finals"] if item.id == final_id), None)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Commerce 成片不存在")
    if row.status != "SUCCEEDED" or not row.storage_key:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Commerce 成片尚未生成可下载 MP4")
    try:
        path = local_asset_storage.final_video_path(row.storage_key)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Commerce 成片文件不存在") from exc
    return FileResponse(path, media_type="video/mp4", filename=f"带货短剧成片-v{row.version}.mp4")
