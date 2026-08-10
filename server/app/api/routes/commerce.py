"""Commerce Phase 2 控制面：路由只解析契约并委托状态服务。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import StoryRunMode, StoryRunStage, StoryRunStatus, WorkflowRun, WorkflowStep
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
    reviews_for_story_run,
    start_story_run,
    workflow_for_story_run,
)
from app.services.worker_runtime import dispatch_workflow


router = APIRouter(prefix="/api/v1/commerce", tags=["Commerce 带货短剧"])


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
    return CommerceWorkflowRunResponse(
        id=run.id, story_run_id=run.commerce_link.story_run_id if run.commerce_link else None,
        project_id=run.project_id, workflow_key=run.workflow_key,
        workflow_definition_id=run.workflow_definition_id, workflow_version=run.workflow_version,
        status=run.status.value, idempotency_key=run.idempotency_key, input_snapshot=run.input_snapshot,
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
    return CommerceStoryRunResponse(
        id=story_run.id, project_id=story_run.project_id, topic_candidate_id=story_run.topic_candidate_id,
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
