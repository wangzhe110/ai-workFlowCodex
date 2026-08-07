"""LemonFlow V1 生成任务编排与 Adapter 运行边界。

本服务只按模型槽位选择已绑定配置，并把每次调用写入 ``ModelInvocation``。开发期的
``mock_v1`` Adapter 让完整审核闭环可在无密钥环境验证；真实供应商必须新增独立
Adapter，而不是在业务状态机中判断 Gemini、Claude、Banana 或 Seedance 的名称。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from time import perf_counter
from typing import Any, Callable

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import (
    AssetKind,
    CharacterDefinition,
    CharacterReferenceImage,
    DesignStatus,
    DirectorPlan,
    DirectorPlanStatus,
    FinalVideo,
    FinalVideoStatus,
    MediaAsset,
    ModelInvocation,
    ModelProfile,
    ModelSlot,
    ProductionStage,
    PromptTemplate,
    PromptTemplateStatus,
    ReferenceAnalysis,
    ReviewStatus,
    RunStatus,
    SceneDefinition,
    SceneReferenceImage,
    ShotAssetBinding,
    ShotKeyframe,
    ShotPlan,
    StoryGenerationBatch,
    StoryProposal,
    VideoClip,
    VideoClipAssetBinding,
    VideoClipStatus,
    VideoReviewStatus,
    WorkflowRun,
    WorkflowStep,
)
from app.services.v1_configuration_service import (
    V1_WORKFLOW_VERSION,
    enabled_profiles_for_slot,
    get_v1_definition,
)
from app.services.v1_production_service import (
    get_project_production_state,
    mark_director_plan_ready,
    mark_reference_analysis_ready,
    mark_story_batch_ready,
)
from app.services.v1_model_adapter_service import (
    adapter_key,
    analyze_reference_video,
    assert_supported,
    create_video_request,
    generate_image,
    generate_structured_text,
    is_mock_adapter,
    persist_v1_image,
    video_provider,
    wait_for_video_result,
)
from app.services.final_video_service import _compose_real_video
from app.services.workflow_service import get_project_or_404


V1_WORKFLOW_PREFIX = "v1_"

RUN_SPECS: dict[str, tuple[str, str, str]] = {
    "reference_analysis": ("VIDEO_ANALYSIS", "VIDEO_ANALYSIS", "Gemini 视频分析"),
    "story_generation": ("STORY_GENERATE", "STORY_GENERATE", "多模型原创故事生成"),
    "character_design": ("CHARACTER_DESIGN", "CHARACTER_DESIGN", "角色资产设计"),
    "character_images": ("CHARACTER_IMAGE_GENERATE", "IMAGE_GENERATE", "角色参考图生成"),
    "scene_design": ("SCENE_DESIGN", "SCENE_DESIGN", "场景资产设计"),
    "scene_images": ("SCENE_IMAGE_GENERATE", "IMAGE_GENERATE", "场景参考图生成"),
    "director_plan": ("DIRECTOR_PLAN", "DIRECTOR_PLAN", "AI 导演分镜"),
    "shot_keyframes": ("SHOT_KEYFRAME_GENERATE", "IMAGE_GENERATE", "分镜关键帧生成"),
    "video_generation": ("VIDEO_GENERATE", "VIDEO_GENERATE", "Seedance 视频片段生成"),
    "final_compose": ("FINAL_COMPOSE", "FINAL_COMPOSE", "审核片段合成成片"),
}

ALLOWED_STAGES: dict[str, set[ProductionStage]] = {
    "reference_analysis": {ProductionStage.REFERENCE_ANALYSIS},
    "story_generation": {ProductionStage.STORY_GENERATION},
    "character_design": {ProductionStage.CHARACTER_ASSETS},
    "character_images": {ProductionStage.CHARACTER_ASSETS},
    "scene_design": {ProductionStage.SCENE_ASSETS},
    "scene_images": {ProductionStage.SCENE_ASSETS},
    "director_plan": {ProductionStage.DIRECTOR_PLANNING},
    "shot_keyframes": {ProductionStage.SHOT_KEYFRAMES},
    "video_generation": {ProductionStage.VIDEO_GENERATION},
    "final_compose": {ProductionStage.FINAL_EXPORT},
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _conflict(message: str) -> None:
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)


def create_v1_run(
    db: Session,
    *,
    project_id: str,
    run_key: str,
    source_asset_id: str | None = None,
    shot_plan_ids: list[str] | None = None,
) -> WorkflowRun:
    """创建 V1 运行，并在数据库提交前冻结本次执行的全部可变配置。

    ``_created`` 是仅供路由/投递层读取的瞬时标记：重复请求返回现有运行而不再次
    投递。它不会写入数据库，因此不会污染历史可复现快照。
    """

    if run_key not in RUN_SPECS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="未知的 V1 生成任务")
    get_project_or_404(db, project_id)
    state = get_project_production_state(db, project_id)
    workflow_key = f"{V1_WORKFLOW_PREFIX}{run_key}"
    existing = db.scalars(
        select(WorkflowRun)
        .where(
            WorkflowRun.project_id == project_id,
            WorkflowRun.workflow_key == workflow_key,
            WorkflowRun.status.in_((RunStatus.PENDING, RunStatus.RUNNING)),
        )
        .order_by(WorkflowRun.created_at.asc())
    ).first()
    if existing is not None:
        setattr(existing, "_created", False)
        return existing
    if state.active_stage not in ALLOWED_STAGES[run_key]:
        _conflict(f"当前阶段为 {state.active_stage.value}，不能创建 {RUN_SPECS[run_key][2]} 任务")
    _validate_inputs(
        db,
        state,
        run_key,
        source_asset_id=source_asset_id,
        shot_plan_ids=shot_plan_ids,
    )

    slot_key, task_type, _ = RUN_SPECS[run_key]
    bindings = enabled_profiles_for_slot(db, slot_key)
    if not bindings:
        _conflict(f"模型槽位 {slot_key} 尚无启用配置，请先在模型中心绑定并验收模型")
    definition = get_v1_definition(db)
    frozen_context = _freeze_run_context(
        db,
        state,
        run_key,
        source_asset_id=source_asset_id,
        shot_plan_ids=shot_plan_ids,
    )
    frozen_models = _freeze_model_bindings(db, bindings, slot_key=slot_key)
    frozen_prompt = _freeze_prompt(db, task_type)
    input_snapshot = {
        "frozen_at": utcnow().isoformat(),
        "stage": state.active_stage.value,
        "run_key": run_key,
        "slot_key": slot_key,
        "task_type": task_type,
        "workflow_definition": {
            "id": definition.id,
            "workflow_code": definition.workflow_code,
            "version": definition.version,
            "definition_json": deepcopy(definition.definition_json),
        },
        "model_bindings": {slot_key: frozen_models},
        "prompt_templates": {task_type: frozen_prompt},
        "context": frozen_context,
    }
    run = WorkflowRun(
        project_id=project_id,
        workflow_key=workflow_key,
        workflow_definition_id=definition.id,
        workflow_version=definition.version,
        idempotency_key=_run_idempotency_key(project_id, run_key, frozen_context),
        input_snapshot=input_snapshot,
    )
    step = WorkflowStep(
        workflow_run=run,
        step_key=slot_key,
        position=1,
        input_payload=deepcopy(input_snapshot),
        # 多模型故事的逐模型快照会写入 ModelInvocation；这里保存完整有序列表摘要。
        model_profile_snapshot={"bindings": deepcopy(frozen_models)},
    )
    db.add_all([run, step])
    try:
        db.flush()
        if run_key == "video_generation":
            _create_video_child_steps(db, run, step)
        db.commit()
    except IntegrityError:
        # PostgreSQL 的部分唯一索引负责处理并发双击/网络重试的最终竞争条件。
        db.rollback()
        existing = db.scalars(
            select(WorkflowRun)
            .where(
                WorkflowRun.project_id == project_id,
                WorkflowRun.workflow_key == workflow_key,
                WorkflowRun.status.in_((RunStatus.PENDING, RunStatus.RUNNING)),
            )
            .order_by(WorkflowRun.created_at.asc())
        ).first()
        if existing is None:
            raise
        setattr(existing, "_created", False)
        return existing
    db.refresh(run)
    setattr(run, "_created", True)
    return run


def _create_video_child_steps(db: Session, run: WorkflowRun, parent_step: WorkflowStep) -> None:
    """为冻结的每个镜头创建独立子任务与待生成 VideoClip。

    子任务在供应商提交前就拥有稳定的 Clip ID 和幂等键；因此即使 Worker 中断，
    下一次只会基于已有记录查询或继续，不会盲目重提付费任务。
    """

    context = _frozen_context(run)
    binding = _frozen_bindings(run, "VIDEO_GENERATE")[0]
    shots = context.get("shots")
    if not isinstance(shots, list) or not shots:
        raise RuntimeError("视频任务没有可生成的冻结镜头")
    child_ids: list[str] = []
    for position, frozen_shot in enumerate(shots, start=2):
        if not isinstance(frozen_shot, dict):
            raise RuntimeError("冻结镜头快照格式无效")
        shot_id = frozen_shot.get("shot_plan_id")
        keyframe = frozen_shot.get("locked_keyframe")
        if not isinstance(shot_id, str) or not isinstance(keyframe, dict) or not isinstance(keyframe.get("id"), str):
            raise RuntimeError("冻结镜头缺少锁定关键帧")
        shot_number = frozen_shot.get("shot_number")
        if not isinstance(shot_number, int):
            raise RuntimeError("冻结镜头缺少编号")
        version = (db.scalar(select(func.max(VideoClip.version)).where(VideoClip.shot_plan_id == shot_id)) or 0) + 1
        child_key = f"{run.idempotency_key}:shot:{shot_id}:v{version}"
        clip = VideoClip(
            project_id=run.project_id,
            storyboard_package_id=None,
            shot_plan_id=shot_id,
            generation_run_id=run.id,
            group_number=shot_number,
            start_shot_number=shot_number,
            end_shot_number=shot_number,
            shots_per_group=1,
            version=version,
            image_ids=[keyframe["id"]],
            prompt=str(frozen_shot.get("video_action_prompt") or ""),
            status=VideoClipStatus.PENDING,
            generation_status=RunStatus.PENDING.value,
            review_status=VideoReviewStatus.PENDING_REVIEW.value,
            input_asset_snapshot={
                "shot_keyframe_id": keyframe["id"],
                "character_reference_image_ids": deepcopy(frozen_shot.get("character_reference_image_ids") or []),
                "scene_reference_image_ids": deepcopy(frozen_shot.get("scene_reference_image_ids") or []),
            },
            idempotency_key=child_key,
        )
        db.add(clip)
        db.flush()
        db.add(VideoClipAssetBinding(video_clip_id=clip.id, asset_type="SHOT_KEYFRAME", shot_keyframe_id=keyframe["id"]))
        for image_id in frozen_shot.get("character_reference_image_ids") or []:
            db.add(VideoClipAssetBinding(video_clip_id=clip.id, asset_type="CHARACTER_REFERENCE", character_reference_image_id=image_id))
        for image_id in frozen_shot.get("scene_reference_image_ids") or []:
            db.add(VideoClipAssetBinding(video_clip_id=clip.id, asset_type="SCENE_REFERENCE", scene_reference_image_id=image_id))
        child = WorkflowStep(
            workflow_run_id=run.id,
            step_key="VIDEO_SHOT",
            position=position,
            input_payload={"shot": deepcopy(frozen_shot), "binding": deepcopy(binding), "prompt": _frozen_prompt(run, "VIDEO_GENERATE")},
            model_profile_snapshot=deepcopy(binding["profile_snapshot"]),
            idempotency_key=child_key,
            shot_plan_id=shot_id,
            video_clip_id=clip.id,
        )
        db.add(child)
        db.flush()
        child_ids.append(child.id)
    parent_step.output_payload = {"child_step_ids": child_ids, "child_count": len(child_ids)}
    snapshot = deepcopy(run.input_snapshot or {})
    snapshot["video_child_step_ids"] = child_ids
    run.input_snapshot = snapshot


def _source_asset_for_reference_analysis(db: Session, project_id: str, source_asset_id: str | None) -> MediaAsset:
    """读取用户明确勾选的参考视频，绝不按上传时间猜测分析输入。"""

    if not source_asset_id:
        _conflict("请先在待分析视频列表中勾选一条参考视频")
    source = db.scalar(
        select(MediaAsset).where(
            MediaAsset.id == source_asset_id,
            MediaAsset.project_id == project_id,
            MediaAsset.kind == AssetKind.SOURCE_VIDEO,
        )
    )
    if source is None:
        _conflict("所选参考视频不存在、已删除或不属于当前项目")
    return source


def _validate_inputs(
    db: Session,
    state,
    run_key: str,
    *,
    source_asset_id: str | None = None,
    shot_plan_ids: list[str] | None = None,
) -> None:
    """集中校验每个生成任务的冻结前置条件，前端无法跳过这些判断。"""

    project_id = state.project_id
    if run_key == "reference_analysis":
        _source_asset_for_reference_analysis(db, project_id, source_asset_id)
    elif run_key == "story_generation" and state.locked_reference_analysis_id is None:
        _conflict("请先人工锁定创作简报")
    elif run_key.startswith(("character_", "scene_")) and state.selected_story_proposal_id is None:
        _conflict("请先人工选择原创故事")
    elif run_key == "character_images":
        if db.scalar(select(CharacterDefinition.id).where(CharacterDefinition.story_proposal_id == state.selected_story_proposal_id).limit(1)) is None:
            _conflict("请先生成角色文字资产")
    elif run_key == "scene_images":
        if db.scalar(select(SceneDefinition.id).where(SceneDefinition.story_proposal_id == state.selected_story_proposal_id).limit(1)) is None:
            _conflict("请先生成场景文字资产")
    elif run_key == "director_plan":
        chars = list(db.scalars(select(CharacterDefinition).where(CharacterDefinition.story_proposal_id == state.selected_story_proposal_id)).all())
        scenes = list(db.scalars(select(SceneDefinition).where(SceneDefinition.story_proposal_id == state.selected_story_proposal_id)).all())
        if not chars or not scenes or not all(item.locked_reference_image_id for item in chars) or not all(item.locked_reference_image_id for item in scenes):
            _conflict("请先锁定所有角色图和场景图")
    elif run_key == "shot_keyframes" and state.director_plan_id is None:
        _conflict("请先完成 AI 导演分镜")
    elif run_key == "video_generation":
        shots = list(db.scalars(select(ShotPlan).where(ShotPlan.director_plan_id == state.director_plan_id)).all())
        if not shots or not all(item.locked_keyframe_id for item in shots):
            _conflict("请先锁定所有分镜关键帧")
        if shot_plan_ids:
            current_ids = {item.id for item in shots}
            if any(item not in current_ids for item in shot_plan_ids):
                _conflict("指定镜头不属于当前导演方案")
    elif run_key == "final_compose":
        clips = _current_clips(db, state)
        shot_count = db.scalar(select(func.count(ShotPlan.id)).where(ShotPlan.director_plan_id == state.director_plan_id)) or 0
        if not clips or len(clips) != shot_count or not all(item.review_status == VideoReviewStatus.APPROVED.value for item in clips):
            _conflict("请先审核通过当前导演方案的全部视频片段")


def _run_idempotency_key(project_id: str, run_key: str, context: dict[str, Any]) -> str:
    """生成稳定、无密钥的任务语义键；供应商调用会使用其派生子键。"""

    raw = f"{project_id}:{run_key}:{context}".encode("utf-8")
    return f"run:{sha256(raw).hexdigest()}"


def _freeze_model_bindings(db: Session, bindings: list[Any], *, slot_key: str) -> list[dict[str, Any]]:
    """把槽位顺序、槽位 ID 与完整模型配置写进 WorkflowRun，Worker 不再回查中心。"""

    return [
        {
            "position": position,
            "slot_id": binding.slot_id,
            "slot_key": slot_key,
            "model_profile_id": binding.model_profile_id,
            "profile_snapshot": _profile_snapshot(binding.model_profile_id, db),
        }
        for position, binding in enumerate(bindings, start=1)
    ]


def _freeze_prompt(db: Session, task_type: str) -> dict[str, Any]:
    """将整份 Prompt（包括变量定义）冻结，而不仅记录一个可变 ID。"""

    prompt = _active_prompt(db, task_type)
    return {
        "id": prompt.id,
        "task_type": prompt.task_type,
        "name": prompt.name,
        "version": prompt.version,
        "content": prompt.content,
        "variables_schema": deepcopy(prompt.variables_schema),
        "status": prompt.status.value,
        "created_at": prompt.created_at.isoformat(),
        "updated_at": prompt.updated_at.isoformat(),
    }


def _freeze_run_context(
    db: Session,
    state,
    run_key: str,
    *,
    source_asset_id: str | None,
    shot_plan_ids: list[str] | None,
) -> dict[str, Any]:
    """冻结本次节点会消费的素材、审核选择和资产版本 ID。"""

    project_id = state.project_id
    # 只有参考视频分析消费源视频，并且它只能使用用户从待分析列表明确勾选的素材。
    # 后续阶段依赖已经锁定的分析/故事/资产快照，绝不因后来上传新视频而改变输入。
    source = (
        _source_asset_for_reference_analysis(db, project_id, source_asset_id)
        if run_key == "reference_analysis"
        else None
    )
    analysis = db.get(ReferenceAnalysis, state.locked_reference_analysis_id) if state.locked_reference_analysis_id else None
    story = db.get(StoryProposal, state.selected_story_proposal_id) if state.selected_story_proposal_id else None
    plan = db.get(DirectorPlan, state.director_plan_id) if state.director_plan_id else None
    characters = list(db.scalars(select(CharacterDefinition).where(CharacterDefinition.story_proposal_id == state.selected_story_proposal_id)).all()) if state.selected_story_proposal_id else []
    scenes = list(db.scalars(select(SceneDefinition).where(SceneDefinition.story_proposal_id == state.selected_story_proposal_id)).all()) if state.selected_story_proposal_id else []
    shots = list(db.scalars(select(ShotPlan).where(ShotPlan.director_plan_id == state.director_plan_id).order_by(ShotPlan.shot_number)).all()) if state.director_plan_id else []
    if run_key == "video_generation":
        requested = set(shot_plan_ids or [])
        # 没有明确指定时，只为“尚未有当前通过版本”的镜头建子任务，重做不会浪费
        # 已通过镜头的供应商费用。
        shots = [
            shot for shot in shots
            if (not requested and not _shot_has_selected_approved_clip(db, shot)) or (requested and shot.id in requested)
        ]
    return {
        "source_asset_id": source.id if source else None,
        "source_asset": _asset_snapshot(source),
        "locked_reference_analysis_id": analysis.id if analysis else None,
        "locked_reference_analysis": _analysis_snapshot(analysis),
        "selected_story_proposal_id": story.id if story else None,
        "selected_story": {"id": story.id, "content": deepcopy(story.content)} if story else None,
        "director_plan_id": plan.id if plan else None,
        "director_plan": {"id": plan.id, "visual_bible": deepcopy(plan.visual_bible)} if plan else None,
        # 所有资产生成阶段都把会进入模型 Prompt 的字段一起冻结。Worker 后续可以
        # 查询这些 ID 的输出版本号，但不得从资产表回读描述或替换成新锁定图片。
        "character_definitions": [_character_definition_snapshot(item) for item in characters],
        "scene_definitions": [_scene_definition_snapshot(item) for item in scenes],
        "locked_character_assets": [
            _locked_character_asset_snapshot(db, item) for item in characters if item.locked_reference_image_id
        ],
        "locked_scene_assets": [
            _locked_scene_asset_snapshot(db, item) for item in scenes if item.locked_reference_image_id
        ],
        "shots": [_shot_snapshot(db, shot) for shot in shots],
        "selected_video_clip_ids": [shot.selected_video_clip_id for shot in shots if shot.selected_video_clip_id],
        "requested_shot_plan_ids": [shot.id for shot in shots] if run_key == "video_generation" else [],
    }


def _asset_snapshot(source: MediaAsset | None) -> dict[str, Any] | None:
    if source is None:
        return None
    return {
        "id": source.id,
        "storage_key": source.storage_key,
        "original_filename": source.original_filename,
        "content_type": source.content_type,
        "byte_size": source.byte_size,
    }


def _analysis_snapshot(analysis: ReferenceAnalysis | None) -> dict[str, Any] | None:
    if analysis is None:
        return None
    return {"id": analysis.id, "locked_snapshot": deepcopy(analysis.locked_snapshot)}


def _character_definition_snapshot(character: CharacterDefinition) -> dict[str, Any]:
    """冻结角色图生成和导演分镜所需的全部角色描述。"""

    return {
        "definition_id": character.id,
        "character_code": character.character_code,
        "name": character.name,
        "age_description": character.age_description,
        "appearance": character.appearance,
        "costume": character.costume,
        "temperament": character.temperament,
        "locked_reference_image_id": character.locked_reference_image_id,
    }


def _scene_definition_snapshot(scene: SceneDefinition) -> dict[str, Any]:
    """冻结场景图生成和导演分镜所需的全部场景描述。"""

    return {
        "definition_id": scene.id,
        "scene_code": scene.scene_code,
        "name": scene.name,
        "location": scene.location,
        "environment": scene.environment,
        "visual_style": scene.visual_style,
        "mood": scene.mood,
        "locked_reference_image_id": scene.locked_reference_image_id,
    }


def _locked_character_asset_snapshot(db: Session, character: CharacterDefinition) -> dict[str, Any]:
    """冻结已选角色图的版本 ID 和地址，禁止后续锁图改写旧任务输入。"""

    image = db.get(CharacterReferenceImage, character.locked_reference_image_id)
    if image is None or not image.image_url:
        raise RuntimeError("锁定角色资产缺少可用参考图")
    return {
        **_character_definition_snapshot(character),
        "reference_image": {"id": image.id, "version": image.version, "image_url": image.image_url},
    }


def _locked_scene_asset_snapshot(db: Session, scene: SceneDefinition) -> dict[str, Any]:
    """冻结已选场景图的版本 ID 和地址，禁止后续锁图改写旧任务输入。"""

    image = db.get(SceneReferenceImage, scene.locked_reference_image_id)
    if image is None or not image.image_url:
        raise RuntimeError("锁定场景资产缺少可用参考图")
    return {
        **_scene_definition_snapshot(scene),
        "reference_image": {"id": image.id, "version": image.version, "image_url": image.image_url},
    }


def _shot_snapshot(db: Session, shot: ShotPlan) -> dict[str, Any]:
    keyframe = db.get(ShotKeyframe, shot.locked_keyframe_id) if shot.locked_keyframe_id else None
    bindings = list(db.scalars(select(ShotAssetBinding).where(ShotAssetBinding.shot_id == shot.id)).all())
    character_references = []
    scene_references = []
    for binding in bindings:
        if binding.character_reference_image_id:
            image = db.get(CharacterReferenceImage, binding.character_reference_image_id)
            if image is None or not image.image_url:
                raise RuntimeError("分镜绑定的角色参考图不存在或没有地址")
            character_references.append({"id": image.id, "image_url": image.image_url})
        if binding.scene_reference_image_id:
            image = db.get(SceneReferenceImage, binding.scene_reference_image_id)
            if image is None or not image.image_url:
                raise RuntimeError("分镜绑定的场景参考图不存在或没有地址")
            scene_references.append({"id": image.id, "image_url": image.image_url})
    return {
        "shot_plan_id": shot.id,
        "shot_number": shot.shot_number,
        "action_description": shot.action_description,
        "camera_description": shot.camera_description,
        "duration_seconds": float(shot.duration_seconds),
        "video_action_prompt": shot.video_action_prompt,
        "locked_keyframe": {"id": keyframe.id, "image_url": keyframe.image_url} if keyframe else None,
        "character_reference_images": character_references,
        "scene_reference_images": scene_references,
        "character_reference_image_ids": [item["id"] for item in character_references],
        "scene_reference_image_ids": [item["id"] for item in scene_references],
    }


def _shot_has_selected_approved_clip(db: Session, shot: ShotPlan) -> bool:
    clip = db.get(VideoClip, shot.selected_video_clip_id) if shot.selected_video_clip_id else None
    return bool(clip and clip.generation_status == RunStatus.SUCCEEDED.value and clip.review_status == VideoReviewStatus.APPROVED.value)


def execute_v1_workflow(run_id: str) -> None:
    """Worker 统一入口；每次真实/模拟调用都会生成独立 ModelInvocation 审计行。"""

    db = SessionLocal()
    try:
        run = db.get(WorkflowRun, run_id)
        if run is None or not run.workflow_key.startswith(V1_WORKFLOW_PREFIX) or run.status != RunStatus.PENDING:
            return
        run_key = run.workflow_key.removeprefix(V1_WORKFLOW_PREFIX)
        # 视频阶段由每镜头独立 Job 执行；父 Run 只聚合子任务状态，不能再走旧的
        # 单 Job 串行轮询路径。
        if run_key == "video_generation":
            return
        now = utcnow()
        run.status = RunStatus.RUNNING
        run.started_at = now
        step = db.scalars(select(WorkflowStep).where(WorkflowStep.workflow_run_id == run.id)).first()
        if step is not None:
            step.status = RunStatus.RUNNING
            step.started_at = now
            step.attempt += 1
        db.commit()
        _EXECUTORS[run_key](db, run)
        run.status = RunStatus.SUCCEEDED
        run.finished_at = utcnow()
        if step is not None:
            step.status = RunStatus.SUCCEEDED
            step.progress = 100
            step.finished_at = run.finished_at
        db.commit()
    except Exception as exc:
        db.rollback()
        run = db.get(WorkflowRun, run_id)
        if run is not None:
            # 运行失败也必须结束本次已创建但尚未完成的模型调用审计，避免后台看板
            # 将“供应商调用报错”误显示为一直运行。
            for invocation in db.scalars(
                select(ModelInvocation).where(
                    ModelInvocation.workflow_run_id == run.id,
                    ModelInvocation.status == RunStatus.RUNNING,
                )
            ):
                invocation.status = RunStatus.FAILED
                invocation.error_code = "ADAPTER_EXECUTION_FAILED"
                invocation.finished_at = utcnow()
            run.status = RunStatus.FAILED
            run.finished_at = utcnow()
            step = db.scalars(select(WorkflowStep).where(WorkflowStep.workflow_run_id == run.id)).first()
            if step is not None:
                step.status = RunStatus.FAILED
                step.error_message = str(exc)[:2000]
                step.finished_at = run.finished_at
            db.commit()
    finally:
        db.close()


def _profile_snapshot(profile_id: str, db: Session) -> dict[str, Any]:
    profile = db.get(ModelProfile, profile_id)
    if profile is None:
        raise RuntimeError("模型槽位绑定引用的模型配置不存在")
    return {
        "profile_id": profile.id,
        "adapter_key": profile.adapter_key or profile.provider_key,
        "provider_key": profile.provider_key,
        "model_key": profile.model_key,
        "model_version": profile.model_version or profile.model_key,
        "display_name": profile.display_name or profile.model_key,
        "version": profile.version,
        "provider_config": deepcopy(profile.provider_config),
    }


def _active_prompt(db: Session, task_type: str) -> PromptTemplate:
    prompt = db.scalars(
        select(PromptTemplate)
        .where(PromptTemplate.task_type == task_type, PromptTemplate.status == PromptTemplateStatus.ACTIVE)
        .order_by(PromptTemplate.version.desc())
    ).first()
    if prompt is None:
        raise RuntimeError(f"任务 {task_type} 没有活动 Prompt 模板")
    return prompt


def _frozen_bindings(run: WorkflowRun, slot_key: str) -> list[dict[str, Any]]:
    """返回创建时固化的有序模型列表；禁止 Worker 回读模型中心。"""

    snapshot = run.input_snapshot or {}
    bindings = ((snapshot.get("model_bindings") or {}).get(slot_key))
    if not isinstance(bindings, list) or not bindings:
        raise RuntimeError(f"运行 {run.id} 缺少冻结模型槽位 {slot_key}")
    normalized = [item for item in bindings if isinstance(item, dict) and isinstance(item.get("profile_snapshot"), dict)]
    if len(normalized) != len(bindings):
        raise RuntimeError("冻结模型快照格式无效")
    return normalized


def _frozen_prompt(run: WorkflowRun, task_type: str) -> dict[str, Any]:
    """返回创建时的完整 Prompt 版本；禁止 Worker 回读 ACTIVE 模板。"""

    snapshot = run.input_snapshot or {}
    prompt = ((snapshot.get("prompt_templates") or {}).get(task_type))
    if not isinstance(prompt, dict) or not isinstance(prompt.get("content"), str) or not prompt["content"].strip():
        raise RuntimeError(f"运行 {run.id} 缺少冻结 Prompt：{task_type}")
    return prompt


def _frozen_context(run: WorkflowRun) -> dict[str, Any]:
    context = (run.input_snapshot or {}).get("context")
    if not isinstance(context, dict):
        raise RuntimeError(f"运行 {run.id} 缺少冻结业务输入")
    return context


def _invoke(
    db: Session,
    *,
    run: WorkflowRun,
    slot_key: str,
    task_type: str,
    input_snapshot: dict[str, Any],
    binding: dict[str, Any],
    workflow_step_id: str | None = None,
    idempotency_key: str | None = None,
) -> ModelInvocation:
    """使用运行冻结的槽位/模型/Prompt 创建一次可计费调用审计。

    相同 ``idempotency_key`` 已存在时直接返回原调用，确保 Worker 重启、网络重试或
    并发任务不会重复向供应商提交请求。
    """

    if idempotency_key:
        existing = db.scalars(select(ModelInvocation).where(ModelInvocation.idempotency_key == idempotency_key)).first()
        if existing is not None:
            return existing
    profile_snapshot = binding.get("profile_snapshot")
    slot_id = binding.get("slot_id")
    profile_id = binding.get("model_profile_id")
    if not isinstance(profile_snapshot, dict) or not isinstance(slot_id, str) or not isinstance(profile_id, str):
        raise RuntimeError("冻结模型绑定格式无效")
    prompt = _frozen_prompt(run, task_type)
    invocation = ModelInvocation(
        project_id=run.project_id,
        workflow_run_id=run.id,
        workflow_step_id=workflow_step_id,
        model_slot_id=slot_id,
        model_profile_id=profile_id,
        prompt_template_id=prompt.get("id"),
        task_type=task_type,
        model_profile_snapshot=deepcopy(profile_snapshot),
        prompt_snapshot=deepcopy(prompt),
        input_snapshot=deepcopy(input_snapshot),
        idempotency_key=idempotency_key,
        status=RunStatus.RUNNING,
    )
    db.add(invocation)
    db.flush()
    return invocation


def _finish_invocation(
    db: Session,
    invocation: ModelInvocation,
    output: dict[str, Any],
    *,
    started_at: float | None = None,
    provider_task_id: str | None = None,
    media_units: dict[str, Any] | None = None,
) -> None:
    """完成一次模型调用审计，并记录可获得的时延、媒体计量和供应商任务号。"""

    invocation.status = RunStatus.SUCCEEDED
    invocation.finished_at = utcnow()
    invocation.output_reference = deepcopy(output)
    if started_at is not None:
        invocation.latency_ms = max(0, int((perf_counter() - started_at) * 1000))
    if provider_task_id:
        invocation.provider_task_id = provider_task_id
    if media_units:
        invocation.media_units = deepcopy(media_units)
    # 真实渠道的精确账单暂未统一开放时，允许在模型中心配置“单次预估成本”。这只用于
    # V1 的人工横向比较，绝不会据此自动切换模型；将来 Adapter 可写入真实用量覆盖它。
    config = invocation.model_profile_snapshot.get("provider_config", {})
    estimated_cost = config.get("estimated_cost_per_call") if isinstance(config, dict) else None
    if isinstance(estimated_cost, (int, float)) and not isinstance(estimated_cost, bool) and estimated_cost >= 0:
        invocation.cost_amount = float(estimated_cost)
        currency = config.get("currency", "CNY")
        invocation.currency = currency if isinstance(currency, str) and currency else "CNY"


def _fail_invocation(
    invocation: ModelInvocation,
    message: str,
    *,
    started_at: float | None = None,
    provider_task_id: str | None = None,
) -> None:
    """将一个已知失败的供应商调用记录为终态，保留任务号供人工排查。"""

    invocation.status = RunStatus.FAILED
    invocation.error_code = "PROVIDER_TASK_FAILED"
    invocation.finished_at = utcnow()
    if started_at is not None:
        invocation.latency_ms = max(0, int((perf_counter() - started_at) * 1000))
    if provider_task_id:
        invocation.provider_task_id = provider_task_id
    invocation.output_reference = {"error": message[:1000]}


def _is_mock(profile_snapshot: dict[str, Any]) -> bool:
    """保留显式本地模拟路径；真实路径统一由 V1 Adapter 层处理。"""

    return is_mock_adapter(profile_snapshot)


def _system_instruction(invocation: ModelInvocation, extra_rules: str) -> str:
    """从冻结 Prompt 快照取生产指令，避免业务代码内置可变 Prompt。"""

    content = invocation.prompt_snapshot.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("本次模型调用缺少冻结 Prompt 内容")
    return f"{content.strip()}\n\n{extra_rules.strip()}"


def _frozen_source(db: Session, run: WorkflowRun) -> MediaAsset:
    """由冻结素材快照重建 Adapter 输入，拒绝读取“最新上传素材”。"""

    del db  # 此函数刻意不回查 media_assets；执行输入只来自 WorkflowRun 快照。
    source = _frozen_context(run).get("source_asset")
    if not isinstance(source, dict):
        raise RuntimeError("创建任务时冻结的参考视频快照不存在")
    required = ("id", "storage_key", "original_filename", "content_type", "byte_size")
    if not all(isinstance(source.get(key), str) and source[key] for key in required[:-1]) or not isinstance(source.get("byte_size"), int):
        raise RuntimeError("创建任务时冻结的参考视频快照格式无效")
    # Adapter 只需要这五个不可变元数据字段；构造未持久化实体可以复用既有类型契约，
    # 又避免供应商执行时受数据库里“最新上传”记录影响。
    return MediaAsset(
        id=source["id"],
        project_id=run.project_id,
        kind=AssetKind.SOURCE_VIDEO,
        storage_key=source["storage_key"],
        original_filename=source["original_filename"],
        content_type=source["content_type"],
        byte_size=source["byte_size"],
    )


def _analysis_payload_from_adapter(result: dict[str, Any]) -> dict[str, Any]:
    """兼容 V1 视觉契约和早期机制分析契约，统一落为五类可审核结果。

    新的 V1 视觉配置应设置 ``result_contract=V1_REFERENCE_ANALYSIS``，从模型直接
    返回完整字段。此处保留旧视觉配置的安全降级映射，使已有已验收的视觉配置也能
    先接入 V1；映射只保留抽象机制，不导入原视频表达。
    """

    required = {
        "video_script_structure",
        "opening_analysis",
        "viral_elements",
        "scene_analysis",
        "creative_brief",
    }
    if required.issubset(result):
        return {key: deepcopy(result[key]) for key in required}
    summary = result.get("summary")
    opening = result.get("opening_mechanism")
    elements = result.get("viral_elements")
    pacing = result.get("pacing_notes")
    compliance = result.get("compliance_note")
    if not all(isinstance(value, str) and value.strip() for value in (summary, pacing, compliance)):
        raise RuntimeError("视觉模型返回结果不符合 V1 视频分析契约")
    if not isinstance(opening, list) or not isinstance(elements, list):
        raise RuntimeError("视觉模型返回结果缺少开头机制或爆款元素数组")
    opening_text = [item.strip() for item in opening if isinstance(item, str) and item.strip()]
    element_text = [item.strip() for item in elements if isinstance(item, str) and item.strip()]
    if not opening_text or not element_text:
        raise RuntimeError("视觉模型返回的开头机制或爆款元素为空")
    return {
        "video_script_structure": {"theme": summary.strip(), "structure": element_text[:8]},
        "opening_analysis": {
            "time_window": "前 3-10 秒",
            "hook_type": opening_text[0],
            "mechanism": "；".join(opening_text[:5]),
        },
        "viral_elements": [{"type": "mechanism", "description": item} for item in element_text[:12]],
        "scene_analysis": [
            {"role": "叙事与情绪承载", "visual_style": "由真实视觉模型的抽象机制分析推导"}
        ],
        "creative_brief": {
            "originality_rule": compliance.strip(),
            "recommended_rhythm": pacing.strip(),
            "target_format": "短视频竖屏（由导演在后续阶段确认）",
        },
    }


def _execute_reference_analysis(db: Session, run: WorkflowRun) -> None:
    source = _frozen_source(db, run)
    binding = _frozen_bindings(run, "VIDEO_ANALYSIS")[0]
    invocation = _invoke(db, run=run, slot_key="VIDEO_ANALYSIS", task_type="VIDEO_ANALYSIS", input_snapshot={"source_asset_id": source.id}, binding=binding)
    snapshot = invocation.model_profile_snapshot
    started_at = perf_counter()
    if _is_mock(snapshot):
        payload = {
            "video_script_structure": {"theme": "高压误会与关系反转", "structure": ["异常开场", "即时目标", "阻碍升级", "信息反转", "悬念收束"]},
            "opening_analysis": {"time_window": "3-10 秒", "hook_type": "异常信息 + 必须立刻行动", "mechanism": "先给不可解释事件，再给人物目标"},
            "viral_elements": [{"type": "conflict", "description": "目标和阻碍同时出现"}, {"type": "emotion", "description": "怀疑、压力、反转"}, {"type": "pacing", "description": "每段推进一条新信息"}],
            "scene_analysis": [{"role": "开场压力", "visual_style": "高信息密度近景"}, {"role": "关系冲突", "visual_style": "稳定场景中变化人物距离"}],
            "creative_brief": {"originality_rule": "只使用结构和情绪机制，必须创作新人设、关系、剧情与画面", "recommended_rhythm": "8-15 秒推进一次信息或关系变化", "target_format": "9:16 短剧"},
        }
        media_units = {"mode": "local_mock", "sampled_frame_count": 0}
    else:
        assert_supported(snapshot, "VIDEO_ANALYSIS")
        adapter_result, sampled_frame_count = analyze_reference_video(snapshot, source)
        payload = _analysis_payload_from_adapter(adapter_result)
        media_units = {"sampled_frame_count": sampled_frame_count}
    version = (db.scalar(select(func.max(ReferenceAnalysis.version)).where(ReferenceAnalysis.project_id == run.project_id)) or 0) + 1
    analysis = ReferenceAnalysis(
        project_id=run.project_id,
        workflow_run_id=run.id,
        version=version,
        video_script_structure=payload["video_script_structure"],
        opening_analysis=payload["opening_analysis"],
        viral_elements=payload["viral_elements"],
        scene_analysis=payload["scene_analysis"],
        creative_brief=payload["creative_brief"],
        generation_status=RunStatus.SUCCEEDED,
    )
    db.add(analysis)
    _finish_invocation(
        db,
        invocation,
        {"reference_analysis_id": analysis.id, "version": version},
        started_at=started_at,
        media_units=media_units,
    )
    db.commit()
    mark_reference_analysis_ready(db, analysis.id)


def _story_content(index: int, model_name: str) -> dict[str, Any]:
    titles = ["倒计时里的陌生来电", "只剩一晚的合约室友", "失物招领处的明日照片"]
    return {
        "title": titles[(index - 1) % len(titles)],
        "model_label": model_name,
        "premise": "异常事件逼迫主角在有限时间内行动；每次接近真相都要付出新的关系代价。",
        "outline": ["异常事件和人物目标同时出现", "第一条线索带来更大阻碍", "关系人身份发生反转", "主角做出有代价的原创选择"],
        "roles": [
            {"code": "LEAD", "name": "林知夏", "age": "28 岁", "appearance": "短发，目光坚定，干净利落", "costume": "浅色风衣与简约通勤装", "temperament": "敏锐、克制、愿意承担代价"},
            {"code": "ALLY", "name": "周予安", "age": "31 岁", "appearance": "深色短发，神情克制", "costume": "深色衬衫和长外套", "temperament": "理性、隐忍、立场复杂"},
        ],
        "scenes": [
            {"code": "CEREMONY", "name": "临时仪式现场", "location": "室内入口", "environment": "倒计时布置与来往人群", "visual_style": "暖色高压电影感", "mood": "紧张"},
            {"code": "STORE", "name": "深夜便利店", "location": "街角便利店", "environment": "冷白灯光与雨夜窗面", "visual_style": "克制现实主义", "mood": "怀疑"},
        ],
    }


def _required_text(value: Any, label: str, *, max_length: int = 4000) -> str:
    """规范化模型返回的必填文本，防止空字段或超大输出进入生产资产。"""

    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"模型结果缺少有效字段：{label}")
    return value.strip()[:max_length]


def _code(value: Any, fallback: str) -> str:
    """取得资产稳定编码；模型异常字符会替换为可读的系统回退编码。"""

    if isinstance(value, str):
        candidate = value.strip().upper().replace(" ", "_")
        if candidate and all(char.isalnum() or char in {"_", "-"} for char in candidate):
            return candidate[:80]
    return fallback


def _normalize_story_content(result: dict[str, Any], model_label: str) -> dict[str, Any]:
    """将文本模型输出校验成故事候选，禁止缺少原创资产所需角色/场景。"""

    roles = result.get("roles")
    scenes = result.get("scenes")
    outline = result.get("outline")
    if not isinstance(roles, list) or not roles:
        raise RuntimeError("故事模型结果缺少 roles 数组")
    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("故事模型结果缺少 scenes 数组")
    if not isinstance(outline, list) or not outline:
        raise RuntimeError("故事模型结果缺少 outline 数组")
    normalized_roles: list[dict[str, str]] = []
    for index, role in enumerate(roles[:12], start=1):
        if not isinstance(role, dict):
            raise RuntimeError("故事模型的 roles 项必须是对象")
        normalized_roles.append(
            {
                "code": _code(role.get("code"), f"ROLE_{index}"),
                "name": _required_text(role.get("name"), f"roles[{index}].name", max_length=160),
                "age": _required_text(role.get("age"), f"roles[{index}].age", max_length=120),
                "appearance": _required_text(role.get("appearance"), f"roles[{index}].appearance"),
                "costume": _required_text(role.get("costume"), f"roles[{index}].costume"),
                "temperament": _required_text(role.get("temperament"), f"roles[{index}].temperament"),
            }
        )
    normalized_scenes: list[dict[str, str]] = []
    for index, scene in enumerate(scenes[:12], start=1):
        if not isinstance(scene, dict):
            raise RuntimeError("故事模型的 scenes 项必须是对象")
        normalized_scenes.append(
            {
                "code": _code(scene.get("code"), f"SCENE_{index}"),
                "name": _required_text(scene.get("name"), f"scenes[{index}].name", max_length=160),
                "location": _required_text(scene.get("location"), f"scenes[{index}].location"),
                "environment": _required_text(scene.get("environment"), f"scenes[{index}].environment"),
                "visual_style": _required_text(scene.get("visual_style"), f"scenes[{index}].visual_style"),
                "mood": _required_text(scene.get("mood"), f"scenes[{index}].mood"),
            }
        )
    normalized_outline = [
        _required_text(item, f"outline[{index}]", max_length=1200)
        for index, item in enumerate(outline[:16], start=1)
    ]
    return {
        "title": _required_text(result.get("title"), "title", max_length=200),
        "model_label": model_label,
        "premise": _required_text(result.get("premise"), "premise"),
        "outline": normalized_outline,
        "roles": normalized_roles,
        "scenes": normalized_scenes,
    }


STORY_OUTPUT_CONTRACT = (
    '{"title":"string","premise":"string","outline":["string"],'
    '"roles":[{"code":"ROLE_CODE","name":"string","age":"string","appearance":"string",'
    '"costume":"string","temperament":"string"}],'
    '"scenes":[{"code":"SCENE_CODE","name":"string","location":"string","environment":"string",'
    '"visual_style":"string","mood":"string"}]}'
)


def _execute_story_generation(db: Session, run: WorkflowRun) -> None:
    frozen_analysis = _frozen_context(run).get("locked_reference_analysis")
    analysis_id = frozen_analysis.get("id") if isinstance(frozen_analysis, dict) else None
    locked_snapshot = frozen_analysis.get("locked_snapshot") if isinstance(frozen_analysis, dict) else None
    if not isinstance(analysis_id, str) or not isinstance(locked_snapshot, dict):
        raise RuntimeError("运行缺少创建时冻结的锁定创作简报")
    # 这里故意不读取 ReferenceAnalysis.locked_snapshot。即使后来有人修改展示字段、
    # 切换项目指针或归档旧分析，本次故事任务仍严格使用创建 WorkflowRun 时的副本。
    batch = StoryGenerationBatch(
        project_id=run.project_id,
        reference_analysis_id=analysis_id,
        workflow_run_id=run.id,
        request_snapshot=deepcopy(locked_snapshot),
        status=RunStatus.RUNNING,
    )
    db.add(batch)
    db.flush()
    for position, binding in enumerate(_frozen_bindings(run, "STORY_GENERATE"), start=1):
        creative_brief = locked_snapshot.get("creative_brief")
        if not isinstance(creative_brief, dict):
            raise RuntimeError("冻结创作简报缺少 creative_brief")
        invocation = _invoke(
            db,
            run=run,
            slot_key="STORY_GENERATE",
            task_type="STORY_GENERATE",
            input_snapshot={"analysis_id": analysis_id, "creative_brief": deepcopy(creative_brief)},
            binding=binding,
        )
        snapshot = invocation.model_profile_snapshot
        started_at = perf_counter()
        if _is_mock(snapshot):
            content = _story_content(position, snapshot["display_name"])
        else:
            result = generate_structured_text(
                snapshot,
                task_type="STORY_GENERATE",
                system_instruction=_system_instruction(
                    invocation,
                    "输出一个完全原创的短剧方案。只能使用已锁定简报中的结构和情绪机制，"
                    "不得复制参考视频的台词、人物、画面或具体剧情。",
                ),
                user_payload={"locked_reference_analysis": deepcopy(locked_snapshot)},
                output_contract=STORY_OUTPUT_CONTRACT,
            )
            content = _normalize_story_content(result, snapshot["display_name"])
        proposal = StoryProposal(batch_id=batch.id, project_id=run.project_id, model_invocation_id=invocation.id, candidate_number=position, content=content)
        db.add(proposal)
        _finish_invocation(
            db,
            invocation,
            {"candidate_number": position, "title": content["title"]},
            started_at=started_at,
        )
    batch.status = RunStatus.SUCCEEDED
    batch.finished_at = utcnow()
    db.commit()
    mark_story_batch_ready(db, batch.id)


def _frozen_story_id(run: WorkflowRun) -> str:
    """返回创建时冻结的故事 ID；不回查项目当前选中故事。"""

    story = _frozen_context(run).get("selected_story")
    story_id = story.get("id") if isinstance(story, dict) else None
    if not isinstance(story_id, str) or not story_id:
        raise RuntimeError("运行缺少创建时冻结的选中故事 ID")
    return story_id


def _frozen_story_content(run: WorkflowRun) -> dict[str, Any]:
    """读取创建时冻结的故事正文，避免执行时使用可变的 StoryProposal.content。"""

    story = _frozen_context(run).get("selected_story")
    content = story.get("content") if isinstance(story, dict) else None
    if not isinstance(content, dict):
        raise RuntimeError("运行缺少创建时冻结的选中故事正文")
    return deepcopy(content)


def _normalize_character_designs(result: dict[str, Any]) -> list[dict[str, str]]:
    """校验角色设计模型输出；每个角色都必须能形成稳定的参考图提示词。"""

    rows = result.get("characters")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("角色设计模型结果缺少 characters 数组")
    normalized: list[dict[str, str]] = []
    seen_codes: set[str] = set()
    for index, item in enumerate(rows[:12], start=1):
        if not isinstance(item, dict):
            raise RuntimeError("角色设计模型的 characters 项必须是对象")
        character_code = _code(item.get("code"), f"ROLE_{index}")
        if character_code in seen_codes:
            raise RuntimeError("角色设计模型返回了重复角色编码")
        seen_codes.add(character_code)
        normalized.append(
            {
                "code": character_code,
                "name": _required_text(item.get("name"), f"characters[{index}].name", max_length=160),
                "age": _required_text(item.get("age"), f"characters[{index}].age", max_length=120),
                "appearance": _required_text(item.get("appearance"), f"characters[{index}].appearance"),
                "costume": _required_text(item.get("costume"), f"characters[{index}].costume"),
                "temperament": _required_text(item.get("temperament"), f"characters[{index}].temperament"),
            }
        )
    return normalized


def _normalize_scene_designs(result: dict[str, Any]) -> list[dict[str, str]]:
    """校验场景设计模型输出；每个场景必须有一致性生成所需的环境与风格。"""

    rows = result.get("scenes")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("场景设计模型结果缺少 scenes 数组")
    normalized: list[dict[str, str]] = []
    seen_codes: set[str] = set()
    for index, item in enumerate(rows[:12], start=1):
        if not isinstance(item, dict):
            raise RuntimeError("场景设计模型的 scenes 项必须是对象")
        scene_code = _code(item.get("code"), f"SCENE_{index}")
        if scene_code in seen_codes:
            raise RuntimeError("场景设计模型返回了重复场景编码")
        seen_codes.add(scene_code)
        normalized.append(
            {
                "code": scene_code,
                "name": _required_text(item.get("name"), f"scenes[{index}].name", max_length=160),
                "location": _required_text(item.get("location"), f"scenes[{index}].location"),
                "environment": _required_text(item.get("environment"), f"scenes[{index}].environment"),
                "visual_style": _required_text(item.get("visual_style"), f"scenes[{index}].visual_style"),
                "mood": _required_text(item.get("mood"), f"scenes[{index}].mood"),
            }
        )
    return normalized


CHARACTER_DESIGN_OUTPUT_CONTRACT = (
    '{"characters":[{"code":"ROLE_CODE","name":"string","age":"string",'
    '"appearance":"string","costume":"string","temperament":"string"}]}'
)
SCENE_DESIGN_OUTPUT_CONTRACT = (
    '{"scenes":[{"code":"SCENE_CODE","name":"string","location":"string",'
    '"environment":"string","visual_style":"string","mood":"string"}]}'
)


def _execute_character_design(db: Session, run: WorkflowRun) -> None:
    story_id = _frozen_story_id(run)
    story_content = _frozen_story_content(run)
    if db.scalar(select(CharacterDefinition.id).where(CharacterDefinition.story_proposal_id == story_id).limit(1)):
        # 基础资产一旦已有记录不能被新的运行覆盖；前端在“设计已生成、图片未生成”
        # 的中断场景下可以安全重试，而真正重做应创建新的故事生产版本。
        return
    binding = _frozen_bindings(run, "CHARACTER_DESIGN")[0]
    invocation = _invoke(db, run=run, slot_key="CHARACTER_DESIGN", task_type="CHARACTER_DESIGN", input_snapshot={"story_id": story_id}, binding=binding)
    snapshot = invocation.model_profile_snapshot
    started_at = perf_counter()
    if _is_mock(snapshot):
        roles = story_content.get("roles", [])
    else:
        result = generate_structured_text(
            snapshot,
            task_type="CHARACTER_DESIGN",
            system_instruction=_system_instruction(
                invocation,
                "依据已选原创故事设计可长期复用的角色资产。角色必须是原创，"
                "并将外貌、服装和性格写成稳定、可供参考图生成的描述。",
            ),
            user_payload={"selected_story": story_content},
            output_contract=CHARACTER_DESIGN_OUTPUT_CONTRACT,
        )
        roles = _normalize_character_designs(result)
    if not isinstance(roles, list) or not roles:
        raise RuntimeError("故事方案缺少可设计的角色")
    for index, role in enumerate(roles, start=1):
        if not isinstance(role, dict):
            continue
        db.add(CharacterDefinition(project_id=run.project_id, story_proposal_id=story_id, character_code=str(role.get("code") or f"ROLE_{index}"), name=str(role.get("name") or f"角色 {index}"), age_description=str(role.get("age") or "成年人"), appearance=str(role.get("appearance") or "原创角色外貌"), costume=str(role.get("costume") or "原创服装"), temperament=str(role.get("temperament") or "有明确行动目标"), design_status=DesignStatus.READY))
    _finish_invocation(
        db,
        invocation,
        {"story_id": story_id, "kind": "CHARACTER_DESIGN", "count": len(roles)},
        started_at=started_at,
    )
    db.commit()


def _execute_scene_design(db: Session, run: WorkflowRun) -> None:
    story_id = _frozen_story_id(run)
    story_content = _frozen_story_content(run)
    if db.scalar(select(SceneDefinition.id).where(SceneDefinition.story_proposal_id == story_id).limit(1)):
        return
    binding = _frozen_bindings(run, "SCENE_DESIGN")[0]
    invocation = _invoke(db, run=run, slot_key="SCENE_DESIGN", task_type="SCENE_DESIGN", input_snapshot={"story_id": story_id}, binding=binding)
    snapshot = invocation.model_profile_snapshot
    started_at = perf_counter()
    if _is_mock(snapshot):
        scenes = story_content.get("scenes", [])
    else:
        result = generate_structured_text(
            snapshot,
            task_type="SCENE_DESIGN",
            system_instruction=_system_instruction(
                invocation,
                "依据已选原创故事设计可长期复用的场景资产。描述要便于持续保持地点、"
                "环境、视觉风格和氛围一致，且不得复制参考视频画面。",
            ),
            user_payload={"selected_story": story_content},
            output_contract=SCENE_DESIGN_OUTPUT_CONTRACT,
        )
        scenes = _normalize_scene_designs(result)
    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("故事方案缺少可设计的场景")
    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            continue
        db.add(SceneDefinition(project_id=run.project_id, story_proposal_id=story_id, scene_code=str(scene.get("code") or f"SCENE_{index}"), name=str(scene.get("name") or f"场景 {index}"), location=str(scene.get("location") or "原创地点"), environment=str(scene.get("environment") or "原创环境"), visual_style=str(scene.get("visual_style") or "电影感"), mood=str(scene.get("mood") or "紧张"), design_status=DesignStatus.READY))
    _finish_invocation(
        db,
        invocation,
        {"story_id": story_id, "kind": "SCENE_DESIGN", "count": len(scenes)},
        started_at=started_at,
    )
    db.commit()


def _mock_image_url(kind: str, object_id: str, version: int) -> str:
    return f"mock://v1-image/{kind}/{object_id}/v{version}"


def _image_prompt(instruction: str, subject: str) -> str:
    """把冻结图片 Prompt 与本次资产描述合成为一段可审计的最终提示词。"""

    return f"{instruction.strip()}\n\n生成对象：{subject.strip()}"


def _frozen_asset_rows(run: WorkflowRun, key: str) -> list[dict[str, Any]]:
    """读取创建时冻结的资产定义，拒绝由运行时数据库内容填充模型输入。"""

    rows = _frozen_context(run).get(key)
    if not isinstance(rows, list) or not rows or not all(isinstance(item, dict) for item in rows):
        raise RuntimeError(f"运行缺少冻结资产快照：{key}")
    return [deepcopy(item) for item in rows]


def _execute_character_images(db: Session, run: WorkflowRun) -> None:
    characters = _frozen_asset_rows(run, "character_definitions")
    binding = _frozen_bindings(run, "CHARACTER_IMAGE_GENERATE")[0]
    for character in characters:
        character_id = character.get("definition_id")
        if not isinstance(character_id, str):
            raise RuntimeError("冻结角色资产缺少 definition_id")
        invocation = _invoke(db, run=run, slot_key="CHARACTER_IMAGE_GENERATE", task_type="IMAGE_GENERATE", input_snapshot={"character_id": character_id}, binding=binding)
        snapshot = invocation.model_profile_snapshot
        version = (db.scalar(select(func.max(CharacterReferenceImage.version)).where(CharacterReferenceImage.character_id == character_id)) or 0) + 1
        prompt = _image_prompt(
            _system_instruction(invocation, "输出单人角色设定参考图，不出现文字、水印或其他未定义角色。"),
            f"角色编码：{character.get('character_code', '')}；姓名：{character.get('name', '')}；"
            f"年龄：{character.get('age_description', '')}；外貌：{character.get('appearance', '')}；"
            f"服装：{character.get('costume', '')}；气质：{character.get('temperament', '')}。",
        )
        started_at = perf_counter()
        if _is_mock(snapshot):
            image_url = _mock_image_url("character", character_id, version)
        else:
            provider_url = generate_image(snapshot, prompt=prompt)
            image_url = persist_v1_image(
                project_id=run.project_id,
                asset_kind="character-reference",
                asset_id=character_id,
                version=version,
                source_url=provider_url,
            )
        image = CharacterReferenceImage(character_id=character_id, project_id=run.project_id, generation_run_id=run.id, model_invocation_id=invocation.id, version=version, prompt_snapshot=prompt, image_url=image_url, generation_status=RunStatus.SUCCEEDED)
        db.add(image)
        _finish_invocation(
            db,
            invocation,
            {"image_id": image.id, "version": version},
            started_at=started_at,
            media_units={"images": 1},
        )
    db.commit()


def _execute_scene_images(db: Session, run: WorkflowRun) -> None:
    scenes = _frozen_asset_rows(run, "scene_definitions")
    binding = _frozen_bindings(run, "SCENE_IMAGE_GENERATE")[0]
    for scene in scenes:
        scene_id = scene.get("definition_id")
        if not isinstance(scene_id, str):
            raise RuntimeError("冻结场景资产缺少 definition_id")
        invocation = _invoke(db, run=run, slot_key="SCENE_IMAGE_GENERATE", task_type="IMAGE_GENERATE", input_snapshot={"scene_id": scene_id}, binding=binding)
        snapshot = invocation.model_profile_snapshot
        version = (db.scalar(select(func.max(SceneReferenceImage.version)).where(SceneReferenceImage.scene_id == scene_id)) or 0) + 1
        prompt = _image_prompt(
            _system_instruction(invocation, "输出无人场景设定参考图，不出现文字、水印或未定义人物。"),
            f"场景编码：{scene.get('scene_code', '')}；名称：{scene.get('name', '')}；地点：{scene.get('location', '')}；"
            f"环境：{scene.get('environment', '')}；视觉风格：{scene.get('visual_style', '')}；"
            f"氛围：{scene.get('mood', '')}。",
        )
        started_at = perf_counter()
        if _is_mock(snapshot):
            image_url = _mock_image_url("scene", scene_id, version)
        else:
            provider_url = generate_image(snapshot, prompt=prompt)
            image_url = persist_v1_image(
                project_id=run.project_id,
                asset_kind="scene-reference",
                asset_id=scene_id,
                version=version,
                source_url=provider_url,
            )
        image = SceneReferenceImage(scene_id=scene_id, project_id=run.project_id, generation_run_id=run.id, model_invocation_id=invocation.id, version=version, prompt_snapshot=prompt, image_url=image_url, generation_status=RunStatus.SUCCEEDED)
        db.add(image)
        _finish_invocation(
            db,
            invocation,
            {"image_id": image.id, "version": version},
            started_at=started_at,
            media_units={"images": 1},
        )
    db.commit()


DIRECTOR_PLAN_OUTPUT_CONTRACT = (
    '{"visual_bible":{"continuity":"string","style":"string"},'
    '"shots":[{"number":1,"scene_code":"SCENE_CODE","character_codes":["ROLE_CODE"],'
    '"action_description":"string","camera_description":"string","duration_seconds":3,'
    '"video_action_prompt":"string"}]}'
)


def _normalize_director_plan(
    result: dict[str, Any],
    *,
    characters: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """按冻结资产快照校验导演输出，不从运行时资产表读取 Prompt 输入。"""

    bible = result.get("visual_bible")
    shots = result.get("shots")
    if not isinstance(bible, dict) or not isinstance(shots, list) or not shots:
        raise RuntimeError("导演模型结果缺少 visual_bible 或 shots")
    visual_bible = {
        "continuity": _required_text(bible.get("continuity"), "visual_bible.continuity"),
        "style": _required_text(bible.get("style"), "visual_bible.style"),
    }
    characters_by_code = {item.get("character_code"): item for item in characters if isinstance(item.get("character_code"), str)}
    scenes_by_code = {item.get("scene_code"): item for item in scenes if isinstance(item.get("scene_code"), str)}
    normalized: list[dict[str, Any]] = []
    seen_numbers: set[int] = set()
    for index, item in enumerate(shots[:80], start=1):
        if not isinstance(item, dict):
            raise RuntimeError("导演模型的 shots 项必须是对象")
        number = item.get("number")
        if not isinstance(number, int) or number < 1 or number in seen_numbers:
            raise RuntimeError("导演模型的镜头编号必须为不重复的正整数")
        seen_numbers.add(number)
        scene_code = _code(item.get("scene_code"), "")
        scene = scenes_by_code.get(scene_code)
        if scene is None or not isinstance(scene.get("reference_image"), dict):
            raise RuntimeError(f"导演模型引用了未锁定或不存在的场景：{scene_code or '未填写'}")
        character_codes = item.get("character_codes")
        if not isinstance(character_codes, list):
            raise RuntimeError(f"第 {number} 镜的 character_codes 必须是数组")
        selected_characters: list[dict[str, Any]] = []
        for character_code in character_codes:
            character = characters_by_code.get(_code(character_code, ""))
            if character is None or not isinstance(character.get("reference_image"), dict):
                raise RuntimeError(f"第 {number} 镜引用了未锁定或不存在的角色")
            if character not in selected_characters:
                selected_characters.append(character)
        if not selected_characters:
            raise RuntimeError(f"第 {number} 镜必须引用至少一个已锁定角色")
        duration = item.get("duration_seconds")
        if not isinstance(duration, (int, float)) or not 0.5 <= float(duration) <= 30:
            raise RuntimeError(f"第 {number} 镜的 duration_seconds 必须在 0.5 至 30 秒之间")
        normalized.append(
            {
                "number": number,
                "scene": scene,
                "characters": selected_characters,
                "action_description": _required_text(item.get("action_description"), f"shots[{index}].action_description"),
                "camera_description": _required_text(item.get("camera_description"), f"shots[{index}].camera_description"),
                "duration_seconds": float(duration),
                "video_action_prompt": _required_text(item.get("video_action_prompt"), f"shots[{index}].video_action_prompt"),
            }
        )
    return visual_bible, sorted(normalized, key=lambda item: item["number"])


def _execute_director_plan(db: Session, run: WorkflowRun) -> None:
    story_id = _frozen_story_id(run)
    story_content = _frozen_story_content(run)
    binding = _frozen_bindings(run, "DIRECTOR_PLAN")[0]
    invocation = _invoke(db, run=run, slot_key="DIRECTOR_PLAN", task_type="DIRECTOR_PLAN", input_snapshot={"story_id": story_id}, binding=binding)
    snapshot = invocation.model_profile_snapshot
    characters = _frozen_asset_rows(run, "locked_character_assets")
    scenes = _frozen_asset_rows(run, "locked_scene_assets")
    started_at = perf_counter()
    if _is_mock(snapshot):
        visual_bible = {"continuity": "所有镜头只能引用锁定角色图和场景图", "style": "原创现实主义短剧"}
        planned_shots = [
            {
                "number": number,
                "scene": scenes[(number - 1) % len(scenes)],
                "characters": characters,
                "action_description": f"角色在{scenes[(number - 1) % len(scenes)].get('name', '场景')}中因新线索采取行动",
                "camera_description": "中近景，缓慢推进，强调关系变化",
                "duration_seconds": 3.0,
                "video_action_prompt": "角色动作自然克制，保持锁定角色与场景一致",
            }
            for number in range(1, 4)
        ]
    else:
        result = generate_structured_text(
            snapshot,
            task_type="DIRECTOR_PLAN",
            system_instruction=_system_instruction(
                invocation,
                "生成导演视觉方案和按顺序排列的分镜。只能引用输入中已经锁定的角色和场景编码，"
                "每镜必须给出动作、机位、时长和图生视频动作描述。",
            ),
            user_payload={
                "selected_story": story_content,
                "locked_characters": [
                    {"code": item.get("character_code"), "name": item.get("name"), "appearance": item.get("appearance"),
                     "costume": item.get("costume"), "reference_image_id": item["reference_image"].get("id")}
                    for item in characters
                ],
                "locked_scenes": [
                    {"code": item.get("scene_code"), "name": item.get("name"), "environment": item.get("environment"),
                     "visual_style": item.get("visual_style"), "reference_image_id": item["reference_image"].get("id")}
                    for item in scenes
                ],
            },
            output_contract=DIRECTOR_PLAN_OUTPUT_CONTRACT,
        )
        visual_bible, planned_shots = _normalize_director_plan(result, characters=characters, scenes=scenes)
    plan = DirectorPlan(project_id=run.project_id, story_proposal_id=story_id, workflow_run_id=run.id, visual_bible=visual_bible, status=DirectorPlanStatus.READY)
    db.add(plan)
    db.flush()
    for specification in planned_shots:
        scene = specification["scene"]
        shot = ShotPlan(director_plan_id=plan.id, project_id=run.project_id, shot_number=specification["number"], action_description=specification["action_description"], camera_description=specification["camera_description"], duration_seconds=specification["duration_seconds"], video_action_prompt=specification["video_action_prompt"])
        db.add(shot)
        db.flush()
        for character in specification["characters"]:
            character_image = character.get("reference_image")
            scene_image = scene.get("reference_image")
            character_id = character.get("definition_id")
            scene_id = scene.get("definition_id")
            if not isinstance(character_id, str) or not isinstance(scene_id, str) or not isinstance(character_image, dict) or not isinstance(scene_image, dict):
                raise RuntimeError("冻结导演资产快照不完整")
            db.add(ShotAssetBinding(shot_id=shot.id, character_id=character_id, character_reference_image_id=character_image.get("id"), scene_id=scene_id, scene_reference_image_id=scene_image.get("id")))
    _finish_invocation(
        db,
        invocation,
        {"director_plan_id": plan.id, "shot_count": len(planned_shots)},
        started_at=started_at,
    )
    db.commit()
    mark_director_plan_ready(db, plan.id)


def _execute_shot_keyframes(db: Session, run: WorkflowRun) -> None:
    shots = _frozen_asset_rows(run, "shots")
    binding = _frozen_bindings(run, "SHOT_KEYFRAME_GENERATE")[0]
    for shot in shots:
        shot_id = shot.get("shot_plan_id")
        shot_number = shot.get("shot_number")
        if not isinstance(shot_id, str) or not isinstance(shot_number, int):
            raise RuntimeError("冻结分镜缺少镜头 ID 或编号")
        character_references = shot.get("character_reference_images")
        scene_references = shot.get("scene_reference_images")
        if not isinstance(character_references, list) or not isinstance(scene_references, list):
            raise RuntimeError("冻结分镜缺少资产引用快照")
        reference_rows = [*character_references, *scene_references]
        reference_urls = list(
            dict.fromkeys(
                item.get("image_url") for item in reference_rows
                if isinstance(item, dict) and isinstance(item.get("image_url"), str) and item["image_url"]
            )
        )
        if not reference_urls:
            raise RuntimeError("分镜关键帧缺少冻结的角色图或场景图")
        invocation = _invoke(
            db,
            run=run,
            slot_key="SHOT_KEYFRAME_GENERATE",
            task_type="IMAGE_GENERATE",
            input_snapshot={
                "shot_id": shot_id,
                "character_reference_image_ids": deepcopy(shot.get("character_reference_image_ids") or []),
                "scene_reference_image_ids": deepcopy(shot.get("scene_reference_image_ids") or []),
            },
            binding=binding,
        )
        snapshot = invocation.model_profile_snapshot
        version = (db.scalar(select(func.max(ShotKeyframe.version)).where(ShotKeyframe.shot_id == shot_id)) or 0) + 1
        prompt = _image_prompt(
            _system_instruction(
                invocation,
                "必须以输入的锁定角色图和场景图为视觉参考，保持人物外观、服装、场景风格一致；"
                "输出这个镜头的一张关键画面，不出现文字或水印。",
            ),
            f"第 {shot_number} 镜；动作：{shot.get('action_description', '')}；"
            f"机位：{shot.get('camera_description', '')}；视频动作要求：{shot.get('video_action_prompt', '')}",
        )
        started_at = perf_counter()
        if _is_mock(snapshot):
            image_url = _mock_image_url("keyframe", shot_id, version)
        else:
            provider_url = generate_image(snapshot, prompt=prompt, reference_image_urls=reference_urls)
            image_url = persist_v1_image(
                project_id=run.project_id,
                asset_kind="shot-keyframe",
                asset_id=shot_id,
                version=version,
                source_url=provider_url,
            )
        frame = ShotKeyframe(shot_id=shot_id, project_id=run.project_id, generation_run_id=run.id, model_invocation_id=invocation.id, version=version, prompt_snapshot=prompt, image_url=image_url, input_asset_snapshot={"character_reference_image_ids": deepcopy(shot.get("character_reference_image_ids") or []), "scene_reference_image_ids": deepcopy(shot.get("scene_reference_image_ids") or [])}, generation_status=RunStatus.SUCCEEDED)
        db.add(frame)
        _finish_invocation(
            db,
            invocation,
            {"keyframe_id": frame.id, "version": version},
            started_at=started_at,
            media_units={"images": 1, "reference_image_count": len(reference_urls)},
        )
    db.commit()


def execute_v1_video_child(run_id: str, step_id: str) -> None:
    """执行一个独立视频镜头子任务；供应商任务号已存在时只恢复查询。"""

    db = SessionLocal()
    try:
        run = db.get(WorkflowRun, run_id)
        step = db.get(WorkflowStep, step_id)
        if run is None or step is None or step.workflow_run_id != run.id or step.step_key != "VIDEO_SHOT":
            return
        if step.status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
            return
        clip = db.get(VideoClip, step.video_clip_id)
        if clip is None:
            raise RuntimeError("视频子任务缺少 VideoClip")
        # 正在执行但尚未获得供应商任务号，说明另一 Worker 已占有提交权；绝不能重提。
        if step.status == RunStatus.RUNNING and not (step.provider_task_id or clip.provider_task_id):
            return
        now = utcnow()
        if run.status == RunStatus.PENDING:
            run.status = RunStatus.RUNNING
            run.started_at = now
        parent = db.scalars(select(WorkflowStep).where(WorkflowStep.workflow_run_id == run.id, WorkflowStep.position == 1)).first()
        if parent is not None and parent.status == RunStatus.PENDING:
            parent.status = RunStatus.RUNNING
            parent.started_at = now
            parent.attempt += 1
        step.status = RunStatus.RUNNING
        step.started_at = step.started_at or now
        step.attempt += 1
        db.commit()

        payload = step.input_payload or {}
        frozen_shot = payload.get("shot")
        binding = payload.get("binding")
        if not isinstance(frozen_shot, dict) or not isinstance(binding, dict):
            raise RuntimeError("视频子任务冻结输入损坏")
        keyframe = frozen_shot.get("locked_keyframe")
        if not isinstance(keyframe, dict) or not isinstance(keyframe.get("id"), str):
            raise RuntimeError("视频子任务缺少冻结关键帧")
        invocation = _invoke(
            db,
            run=run,
            slot_key="VIDEO_GENERATE",
            task_type="VIDEO_GENERATE",
            input_snapshot={"shot_id": frozen_shot.get("shot_plan_id"), "keyframe_id": keyframe["id"]},
            binding=binding,
            workflow_step_id=step.id,
            idempotency_key=step.idempotency_key,
        )
        clip.model_invocation_id = invocation.id
        snapshot = invocation.model_profile_snapshot
        started_at = perf_counter()
        if _is_mock(snapshot):
            clip.video_url = f"mock://v1-video/{clip.shot_plan_id}/v{clip.version}"
            clip.status = VideoClipStatus.SUCCEEDED
            clip.generation_status = RunStatus.SUCCEEDED.value
            _finish_invocation(db, invocation, {"video_clip_id": clip.id, "version": clip.version}, started_at=started_at, media_units={"video_clips": 1, "mode": "local_mock"})
            _finish_video_child(db, run, step, clip)
            return
        if not isinstance(keyframe.get("image_url"), str) or not keyframe["image_url"]:
            raise RuntimeError("冻结关键帧缺少可用图片地址")
        provider = video_provider(snapshot)
        provider_task_id = step.provider_task_id or clip.provider_task_id or invocation.provider_task_id
        if provider_task_id:
            # Worker 重启后的恢复路径：已有供应商号只查询，绝不会再次 submit。
            first_result = provider.poll(provider_task_id)
        else:
            submitted = provider.submit(
                create_video_request(
                    project_id=run.project_id,
                    shot_number=int(frozen_shot["shot_number"]),
                    prompt=str(frozen_shot.get("video_action_prompt") or ""),
                    image_urls=[keyframe["image_url"]],
                )
            )
            provider_task_id = submitted.provider_task_id
            if not provider_task_id:
                raise RuntimeError("视频供应商提交成功但未返回任务号")
            step.provider_task_id = provider_task_id
            clip.provider_task_id = provider_task_id
            invocation.provider_task_id = provider_task_id
            # 任务号必须在轮询前提交，进程中断后才能恢复查询而不产生第二次扣费。
            db.commit()
            first_result = submitted
        result = wait_for_video_result(provider, snapshot, first_result)
        clip.provider_task_id = result.provider_task_id or provider_task_id
        step.provider_task_id = clip.provider_task_id
        if result.status != "SUCCEEDED" or not result.video_url:
            message = result.error_message or "视频供应商任务失败"
            clip.status = VideoClipStatus.FAILED
            clip.generation_status = RunStatus.FAILED.value
            clip.error_message = message[:2000]
            _fail_invocation(invocation, message, started_at=started_at, provider_task_id=clip.provider_task_id)
            raise RuntimeError(message)
        clip.video_url = result.video_url
        clip.status = VideoClipStatus.SUCCEEDED
        clip.generation_status = RunStatus.SUCCEEDED.value
        clip.error_message = None
        _finish_invocation(db, invocation, {"video_clip_id": clip.id, "version": clip.version, "provider_task_id": clip.provider_task_id}, started_at=started_at, provider_task_id=clip.provider_task_id, media_units={"video_clips": 1})
        _finish_video_child(db, run, step, clip)
    except Exception as exc:
        db.rollback()
        run = db.get(WorkflowRun, run_id)
        step = db.get(WorkflowStep, step_id)
        if run is not None and step is not None:
            clip = db.get(VideoClip, step.video_clip_id)
            if clip is not None:
                clip.status = VideoClipStatus.FAILED
                clip.generation_status = RunStatus.FAILED.value
                clip.error_message = str(exc)[:2000]
            invocation = db.scalars(select(ModelInvocation).where(ModelInvocation.idempotency_key == step.idempotency_key)).first()
            if invocation is not None and invocation.status == RunStatus.RUNNING:
                _fail_invocation(invocation, str(exc), provider_task_id=step.provider_task_id)
            step.status = RunStatus.FAILED
            step.error_message = str(exc)[:2000]
            step.finished_at = utcnow()
            _aggregate_video_parent(db, run)
            db.commit()
    finally:
        db.close()


def _finish_video_child(db: Session, run: WorkflowRun, step: WorkflowStep, clip: VideoClip) -> None:
    step.status = RunStatus.SUCCEEDED
    step.progress = 100
    step.output_payload = {"video_clip_id": clip.id, "provider_task_id": clip.provider_task_id}
    step.finished_at = utcnow()
    _aggregate_video_parent(db, run)
    db.commit()


def _aggregate_video_parent(db: Session, run: WorkflowRun) -> None:
    """所有独立镜头到达终态才终结父 Run，避免父任务抢跑进入审核。"""

    children = list(db.scalars(select(WorkflowStep).where(WorkflowStep.workflow_run_id == run.id, WorkflowStep.step_key == "VIDEO_SHOT")).all())
    parent = db.scalars(select(WorkflowStep).where(WorkflowStep.workflow_run_id == run.id, WorkflowStep.position == 1)).first()
    if not children:
        run.status = RunStatus.FAILED
        run.finished_at = utcnow()
        if parent is not None:
            parent.status = RunStatus.FAILED
            parent.error_message = "视频运行没有镜头子任务"
            parent.finished_at = run.finished_at
        return
    if any(child.status == RunStatus.FAILED for child in children):
        run.status = RunStatus.FAILED
        run.finished_at = utcnow()
        if parent is not None:
            parent.status = RunStatus.FAILED
            parent.progress = int(sum(child.status == RunStatus.SUCCEEDED for child in children) / len(children) * 100)
            parent.error_message = "至少一个视频镜头生成失败；请人工选择失败镜头重新生成"
            parent.finished_at = run.finished_at
        return
    if all(child.status == RunStatus.SUCCEEDED for child in children):
        run.status = RunStatus.SUCCEEDED
        run.finished_at = utcnow()
        if parent is not None:
            parent.status = RunStatus.SUCCEEDED
            parent.progress = 100
            parent.finished_at = run.finished_at
        state = get_project_production_state(db, run.project_id)
        state.active_stage = ProductionStage.VIDEO_REVIEW
        return
    run.status = RunStatus.RUNNING
    if parent is not None:
        parent.status = RunStatus.RUNNING
        parent.progress = int(sum(child.status == RunStatus.SUCCEEDED for child in children) / len(children) * 100)


def _current_clips(db: Session, state) -> list[VideoClip]:
    """只返回每镜明确采用的版本；历史 REJECTED 片段永远不进入审核/合成。"""

    if not state.director_plan_id:
        return []
    return list(
        db.scalars(
            select(VideoClip)
            .join(ShotPlan, VideoClip.id == ShotPlan.selected_video_clip_id)
            .where(VideoClip.project_id == state.project_id, ShotPlan.director_plan_id == state.director_plan_id)
            .order_by(ShotPlan.shot_number)
        ).all()
    )


def _execute_video_generation(db: Session, run: WorkflowRun) -> None:
    """防止旧 Worker 入口误走串行视频逻辑。"""

    raise RuntimeError("视频生成必须通过 VIDEO_SHOT 子任务执行")


def _execute_final_compose(db: Session, run: WorkflowRun) -> None:
    context = _frozen_context(run)
    director_plan_id = context.get("director_plan_id")
    clip_ids = context.get("selected_video_clip_ids")
    if not isinstance(director_plan_id, str) or not isinstance(clip_ids, list):
        raise RuntimeError("成片任务缺少冻结导演方案或视频片段")
    clips = [db.get(VideoClip, clip_id) for clip_id in clip_ids]
    if any(clip is None or clip.review_status != VideoReviewStatus.APPROVED.value for clip in clips):
        raise RuntimeError("冻结视频片段已不存在或未经审核通过")
    selected_clips = [clip for clip in clips if clip is not None]
    binding = _frozen_bindings(run, "FINAL_COMPOSE")[0]
    invocation = _invoke(db, run=run, slot_key="FINAL_COMPOSE", task_type="FINAL_COMPOSE", input_snapshot={"clip_ids": [item.id for item in selected_clips]}, binding=binding)
    snapshot = invocation.model_profile_snapshot
    version = (db.scalar(select(func.max(FinalVideo.version)).where(FinalVideo.project_id == run.project_id, FinalVideo.director_plan_id == director_plan_id)) or 0) + 1
    final = FinalVideo(project_id=run.project_id, storyboard_package_id=None, director_plan_id=director_plan_id, workflow_definition_id=run.workflow_definition_id, workflow_version=run.workflow_version, generation_run_id=run.id, version=version, clip_ids=[item.id for item in selected_clips], approved_clip_ids=[item.id for item in selected_clips], input_snapshot={"approved_clip_ids": [item.id for item in selected_clips]}, status=FinalVideoStatus.PENDING)
    db.add(final)
    db.flush()
    started_at = perf_counter()
    if _is_mock(snapshot):
        final.output_url = f"mock://v1-final/{run.project_id}/v{version}"
        final.status = FinalVideoStatus.SUCCEEDED
        final.finished_at = utcnow()
        _finish_invocation(
            db,
            invocation,
            {"final_video_id": final.id, "version": version},
            started_at=started_at,
            media_units={"final_videos": 1, "mode": "local_mock"},
        )
    else:
        assert_supported(snapshot, "FINAL_COMPOSE")
        delivery = _compose_real_video(
            project_id=run.project_id,
            final_video_id=final.id,
            clips=selected_clips,
            snapshot=snapshot,
        )
        final.storage_key = delivery.storage_key
        final.output_url = delivery.public_url
        final.status = FinalVideoStatus.SUCCEEDED
        final.finished_at = utcnow()
        _finish_invocation(
            db,
            invocation,
            {"final_video_id": final.id, "version": version, "storage_key": delivery.storage_key},
            started_at=started_at,
            media_units={"final_videos": 1, "input_video_clips": len(selected_clips)},
        )
    state = get_project_production_state(db, run.project_id)
    state.active_stage = ProductionStage.COMPLETED
    db.commit()


_EXECUTORS: dict[str, Callable[[Session, WorkflowRun], None]] = {
    "reference_analysis": _execute_reference_analysis,
    "story_generation": _execute_story_generation,
    "character_design": _execute_character_design,
    "character_images": _execute_character_images,
    "scene_design": _execute_scene_design,
    "scene_images": _execute_scene_images,
    "director_plan": _execute_director_plan,
    "shot_keyframes": _execute_shot_keyframes,
    "final_compose": _execute_final_compose,
}
