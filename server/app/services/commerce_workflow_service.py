"""Commerce Phase 2 的状态机、编排及无供应商依赖的节点执行边界。

本模块是 Commerce API 和 Worker 的唯一写入口。它复用 ``WorkflowRun`` /
``WorkflowStep``，并通过 0012 专属的 Commerce 关联/attempt 表防止同一 StoryRun 的
重复活动任务；审核事实只写入 ``ReviewDecision``，不会把虚构的结果塞入 stage_data。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from time import perf_counter, sleep
from typing import Any, Protocol

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import (
    ChapterPlan,
    CommerceChapterAttemptChapter,
    CommerceCreativeBatch,
    CommerceCreativeIdea,
    CommerceCreativeIdeaStatus,
    CommerceReferenceIntake,
    CommerceStoryRunInput,
    CommerceWorkflowLink,
    CommerceWorkflowStep,
    DialogueLine,
    ModelInvocation,
    ModelProfile,
    ModelSlot,
    OutlineVersionStatus,
    ProductPlacementMethod,
    ProductPlacementPlan,
    ProductPlacementStrength,
    ProductAssetVersion,
    ProductAssetVersionStatus,
    Project,
    ProjectProductSelection,
    RenderBatch,
    RenderBatchStatus,
    ReviewDecision,
    RunStatus,
    SceneMappingVersion,
    SegmentPlanStatus,
    StoryOutlineVersion,
    StoryRun,
    StoryRunMode,
    StoryRunState,
    StoryRunStage,
    StoryRunStatus,
    SubShotPlan,
    TopicCandidate,
    VideoPromptVersion,
    VideoSegmentPlan,
    WorkflowRun,
    WorkflowStep,
)
from app.services.commerce_configuration_service import (
    COMMERCE_WORKFLOW_CODE,
    ensure_commerce_foundation,
)
from app.services.commerce_domain_service import (
    CommerceDomainValidationError,
    add_sub_shot_plan,
    create_chapter_plan,
    create_dialogue_line,
    create_next_story_outline_version,
    create_product_placement_plan,
    create_render_batch,
    create_scene_mapping_version,
    create_story_run,
    create_video_segment_plan,
    transition_story_outline_version_status,
    update_story_outline_version,
    validate_story_run_bindings,
)
from app.services.v1_configuration_service import enabled_profiles_for_slot
from app.services.v1_model_adapter_service import assert_supported, generate_structured_text, is_mock_adapter
from app.services.model_parameter_service import profile_parameter_config, resolve_effective_model_parameters
from app.services.provider_config_security import redact_provider_config
from app.services.prompt_template_service import (
    ensure_prompt_template_foundation,
    freeze_active_prompt,
    freeze_prompt_version,
)
from app.services.commerce_workflow_preset_service import (
    copy_story_run_workflow_config,
    freeze_story_run_workflow_config,
    resolve_story_run_workflow_config,
    story_run_workflow_config_snapshot,
)


# 一个 StoryRun 只拥有一个长期存在的 Commerce 父运行。每个阶段和人工重做均在
# 该父运行下追加 WorkflowStep(attempt)，避免 API 查询、当前步骤和幂等语义分散到
# 多个“看起来同级”的 WorkflowRun。
COMMERCE_WORKFLOW_KEY = "commerce_story_run"
STAGES: tuple[StoryRunStage, ...] = (
    StoryRunStage.TOPIC,
    StoryRunStage.OUTLINE,
    StoryRunStage.CHAPTERS,
    StoryRunStage.STORYBOARD,
    StoryRunStage.VISUAL_ASSETS,
    StoryRunStage.VIDEO_PROMPTS,
    StoryRunStage.SEGMENT_RENDER,
)
REVIEW_GATES = {
    StoryRunStage.OUTLINE,
    StoryRunStage.STORYBOARD,
    StoryRunStage.VISUAL_ASSETS,
    StoryRunStage.SEGMENT_RENDER,
}
NODE_MODEL_SPECS: dict[StoryRunStage, tuple[str | None, str | None]] = {
    StoryRunStage.TOPIC: (None, None),
    StoryRunStage.OUTLINE: ("STORY_GENERATE", "STORY_GENERATE"),
    StoryRunStage.CHAPTERS: ("STORY_GENERATE", "STORY_GENERATE"),
    StoryRunStage.STORYBOARD: ("DIRECTOR_PLAN", "DIRECTOR_PLAN"),
    StoryRunStage.VISUAL_ASSETS: ("SHOT_KEYFRAME_GENERATE", "IMAGE_GENERATE"),
    StoryRunStage.VIDEO_PROMPTS: ("VIDEO_GENERATE", "VIDEO_GENERATE"),
    StoryRunStage.SEGMENT_RENDER: ("VIDEO_GENERATE", "VIDEO_GENERATE"),
}

PROMPT_KEY_BY_STAGE: dict[StoryRunStage, str | None] = {
    StoryRunStage.TOPIC: None,
    StoryRunStage.OUTLINE: "commerce.story_outline",
    StoryRunStage.CHAPTERS: "commerce.story_outline",
    StoryRunStage.STORYBOARD: "commerce.director_storyboard",
    StoryRunStage.VISUAL_ASSETS: "commerce.keyframe_prompt_organize",
    StoryRunStage.VIDEO_PROMPTS: "commerce.video_prompt_generate",
    StoryRunStage.SEGMENT_RENDER: "v1.video_prompt_generate",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _error(detail: str, code: int = status.HTTP_409_CONFLICT) -> None:
    raise HTTPException(status_code=code, detail=detail)


def _frozen_prompt_text(prompt: dict[str, Any], field: str) -> str:
    """拒绝执行时回读活动模板或回退为旧硬编码正文。"""

    value = prompt.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Commerce 工作流缺少冻结 Prompt 字段：{field}")
    return value.strip()


def _safe_error(_: Exception) -> str:
    """Worker 错误只暴露可行动的摘要，绝不回传供应商原始响应或密钥。"""

    return "Commerce 节点执行失败；请检查冻结的模型配置或人工结果后重试"


def _next_stage(stage: StoryRunStage) -> StoryRunStage:
    if stage == StoryRunStage.SEGMENT_RENDER:
        return StoryRunStage.COMPLETED
    return STAGES[STAGES.index(stage) + 1]


def _stage_name(value: StoryRunStage) -> str:
    return value.value


def _locked_story_run(db: Session, story_run_id: str) -> StoryRun:
    # API/Worker 使用不同 Session。显式刷新避免调用方持有的 identity-map 把 Worker
    # 已落库的 FAILED/PAUSED 状态误判成旧 RUNNING，从而将 retry 或人工审核拒绝掉。
    run = db.scalars(
        select(StoryRun)
        .where(StoryRun.id == story_run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if run is None:
        _error("StoryRun 不存在", status.HTTP_404_NOT_FOUND)
    db.refresh(run)
    db.refresh(run.state)
    return run


def _require_not_terminal(story_run: StoryRun) -> None:
    if story_run.state.status in {StoryRunStatus.COMPLETED, StoryRunStatus.CANCELLED}:
        _error("已完成或已取消的 StoryRun 不能再修改")


def _profile_snapshot(
    db: Session,
    binding,
    slot_key: str,
    *,
    execution_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # ModelSlotProfileBinding 保持轻量外键模型，没有 ORM relationship；在冻结点显式
    # 读取后将完整快照写进 WorkflowRun，Worker 后续不再回查。
    profile = db.get(ModelProfile, binding.model_profile_id)
    if profile is None:
        _error("模型槽位绑定引用的模型配置不存在", status.HTTP_503_SERVICE_UNAVAILABLE)
    slot = db.get(ModelSlot, binding.slot_id)
    if slot is None:
        _error("模型槽位绑定引用的槽位不存在", status.HTTP_503_SERVICE_UNAVAILABLE)
    profile_snapshot = {
        "profile_id": profile.id,
        "adapter_key": profile.adapter_key or profile.provider_key,
        "provider_key": profile.provider_key,
        "model_key": profile.model_key,
        "model_version": profile.model_version or profile.model_key,
        "display_name": profile.display_name or profile.model_key,
        "version": profile.version,
        "provider_config": redact_provider_config(profile.provider_config),
    }
    parameter_config, _ = profile_parameter_config(
        profile_snapshot["adapter_key"],
        profile_snapshot["provider_config"],
        profile.parameter_config,
    )
    profile_snapshot["parameter_config"] = parameter_config
    profile_snapshot["parameter_resolution"] = resolve_effective_model_parameters(
        profile_snapshot,
        preset="standard",
        execution_context=execution_context,
    )
    return {
        "position": binding.priority,
        "slot_id": binding.slot_id,
        "slot_key": slot_key,
        "slot_snapshot": {
            "id": slot.id,
            "slot_key": slot.slot_key,
            "capability": slot.capability,
            "selection_mode": slot.selection_mode.value,
            "description": slot.description,
        },
        "model_profile_id": profile.id,
        "profile_snapshot": profile_snapshot,
        # Adapter 是业务能力到供应商协议的唯一边界，必须随任务冻结，不能仅靠
        # 后续的 profile.adapter_key 再推导。
        "adapter_snapshot": {
            "key": profile.adapter_key or profile.provider_key,
            "provider_key": profile.provider_key,
            "model_key": profile.model_key,
            "model_version": profile.model_version or profile.model_key,
        },
    }


def _freeze_execution_snapshot(db: Session, story_run: StoryRun, stage: StoryRunStage) -> dict[str, Any]:
    """创建任务时冻结定义、模型和 Prompt；Worker 绝不回读中心当前配置。"""

    definition = ensure_commerce_foundation(db)
    slot_key, task_type = NODE_MODEL_SPECS[stage]
    bindings: list[dict[str, Any]] = []
    prompt: dict[str, Any] | None = None
    workflow_config = story_run_workflow_config_snapshot(story_run)
    if slot_key:
        if workflow_config is not None:
            frozen_bindings = (workflow_config.get("model_bindings") or {}).get(slot_key)
            if not isinstance(frozen_bindings, list) or not frozen_bindings:
                _error(f"StoryRun 缺少已冻结的模型槽位 {slot_key}", status.HTTP_409_CONFLICT)
            # 已配置的 StoryRun 只能复制创建时的完整 Binding/Profile/参数快照；不允许
            # 后续切换模型中心、质量预设或槽位绑定影响已开始的工作流。
            bindings = deepcopy(frozen_bindings)
        else:
            # 0024 前的历史 StoryRun 没有预设冻结行，维持既有行为以保证历史恢复可读。
            raw_bindings = enabled_profiles_for_slot(db, slot_key)
            if not raw_bindings:
                _error(f"模型槽位 {slot_key} 没有启用的模型配置", status.HTTP_503_SERVICE_UNAVAILABLE)
            bindings = [
                _profile_snapshot(
                    db,
                    binding,
                    slot_key,
                    execution_context={"operation": stage.value},
                )
                for binding in raw_bindings
            ]
    selection = db.get(ProjectProductSelection, story_run.project_product_selection_id)
    product_version = db.get(ProductAssetVersion, story_run.product_asset_version_id)
    if selection is None or product_version is None:
        _error("StoryRun 的冻结产品选择或产品版本不存在", status.HTTP_409_CONFLICT)
    snapshot = {
        "frozen_at": utcnow().isoformat(),
        "commerce": {
            "story_run_id": story_run.id,
            "project_id": story_run.project_id,
            "topic_candidate_id": story_run.topic_candidate_id,
            "project_product_selection_id": story_run.project_product_selection_id,
            "product_asset_version_id": story_run.product_asset_version_id,
            "mode": story_run.mode.value,
            "stage": stage.value,
            "project_product_selection_snapshot": {
                "id": selection.id,
                "project_id": selection.project_id,
                "product_asset_id": selection.product_asset_id,
                "product_asset_version_id": selection.product_asset_version_id,
                "selected_at": selection.selected_at.isoformat(),
            },
            "product_asset_version_snapshot": {
                "id": product_version.id,
                "product_asset_id": product_version.product_asset_id,
                "source_analysis_version_id": product_version.source_analysis_version_id,
                "version": product_version.version,
                "product_name": product_version.product_name,
                "appearance_description": product_version.appearance_description,
                "selling_points": deepcopy(product_version.selling_points),
                "user_pain_points": deepcopy(product_version.user_pain_points),
                "usage_scenarios": deepcopy(product_version.usage_scenarios),
                "package_ocr": deepcopy(product_version.package_ocr),
                "reference_images": deepcopy(product_version.reference_images),
                "status": product_version.status.value,
                "frozen_at": product_version.frozen_at.isoformat() if product_version.frozen_at else None,
            },
        },
        "workflow_definition": {
            "id": definition.id,
            "workflow_code": definition.workflow_code,
            "version": definition.version,
            "definition_json": deepcopy(definition.definition_json),
        },
        "model_bindings": {slot_key: bindings} if slot_key else {},
        "prompt_templates": {},
    }
    if workflow_config is not None:
        snapshot["workflow_config"] = workflow_config
    # Slice 1 的 StoryRun 有一对一的上游输入版本快照。这里复制它，不再执行时回读
    # ScriptAnalysis、产品或创意表；旧 Commerce StoryRun 保持原有快照结构。
    if story_run.mainline_input is not None:
        snapshot["commerce_mainline"] = deepcopy(story_run.mainline_input.input_snapshot)
    prompt_key = PROMPT_KEY_BY_STAGE.get(stage)
    if prompt_key is not None:
        # 模型绑定/Provider 配置属于运行审计，不属于模型业务输入。Prompt 只消费
        # 已冻结的故事、商品与审核结果，避免任何历史异常配置字段被送入模板渲染。
        business_context = {
            "commerce": deepcopy(snapshot["commerce"]),
            "commerce_mainline": deepcopy(snapshot.get("commerce_mainline") or {}),
        }
        if stage in {StoryRunStage.OUTLINE, StoryRunStage.CHAPTERS}:
            variables = {"frozen_input": business_context}
        elif stage == StoryRunStage.STORYBOARD:
            variables = {"commerce_context": business_context}
        elif stage == StoryRunStage.VISUAL_ASSETS:
            variables = {"shot": business_context}
        elif stage == StoryRunStage.VIDEO_PROMPTS:
            variables = {"video_context": business_context}
        else:
            variables = {"shot": business_context}
        frozen_prompt = ((workflow_config or {}).get("prompt_templates") or {}).get(prompt_key)
        if workflow_config is not None:
            version_id = frozen_prompt.get("prompt_version_id") if isinstance(frozen_prompt, dict) else None
            if not isinstance(version_id, str) or not version_id:
                _error(f"StoryRun 缺少已冻结的 Prompt {prompt_key}", status.HTTP_409_CONFLICT)
            prompt = freeze_prompt_version(
                db,
                prompt_key=prompt_key,
                prompt_version_id=version_id,
                variables=variables,
                legacy_task_type=task_type,
            )
        else:
            prompt = freeze_active_prompt(db, prompt_key, variables, legacy_task_type=task_type)
        if task_type is not None:
            prompt["task_type"] = task_type
            # 仅供已发布的旧 API/测试读取。实际 Worker 只读取 rendered_* 与
            # prompt_version_id，绝不会把它当成未冻结的当前 Prompt。
            prompt["content"] = prompt["rendered_system_template"]
            snapshot["prompt_templates"] = {task_type: prompt}
    return snapshot


def create_next_story_run(
    db: Session,
    *,
    project_id: str,
    topic_candidate_id: str,
    project_product_selection_id: str,
    mode: StoryRunMode | None = None,
    preset_key: str | None = None,
    preset_version_id: str | None = None,
    run_overrides: dict[str, Any] | None = None,
) -> StoryRun:
    """集中计算重跑编号，并从项目选择读取冻结产品版本。"""

    # 旧的服务调用方可能只初始化了 V1/Commerce 工作流定义，而没有经过
    # 应用启动时的全量 foundation。创建新 Run 前补齐系统 Prompt 目录，保证
    # 新增的冻结链对这些兼容调用同样可用；函数本身是幂等的，不会重置人工版本。
    ensure_prompt_template_foundation(db)
    if db.get(Project, project_id) is None:
        _error("项目不存在", status.HTTP_404_NOT_FOUND)
    selection = db.get(ProjectProductSelection, project_product_selection_id)
    if selection is None:
        _error("项目产品选择不存在", status.HTTP_404_NOT_FOUND)
    # 锁定同一选题既有运行，唯一约束是并发下的最终兜底。
    db.scalars(
        select(StoryRun.id)
        .where(StoryRun.project_id == project_id, StoryRun.topic_candidate_id == topic_candidate_id)
        .with_for_update()
    ).all()
    next_number = int(
        db.scalar(
            select(func.max(StoryRun.run_number)).where(
                StoryRun.project_id == project_id, StoryRun.topic_candidate_id == topic_candidate_id
            )
        )
        or 0
    ) + 1
    requested_overrides = deepcopy(run_overrides or {})
    if mode is not None:
        requested_overrides.setdefault("execution_mode", mode.value)
    resolved_config = resolve_story_run_workflow_config(
        db, preset_key=preset_key, preset_version_id=preset_version_id, run_overrides=requested_overrides
    )
    effective_mode = StoryRunMode(resolved_config["effective_workflow_config"]["execution_mode"])
    try:
        story_run = create_story_run(
            db,
            project_id=project_id,
            topic_candidate_id=topic_candidate_id,
            project_product_selection_id=selection.id,
            product_asset_version_id=selection.product_asset_version_id,
            run_number=next_number,
            mode=effective_mode,
        )
        freeze_story_run_workflow_config(db, story_run=story_run, resolved=resolved_config)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _error("同一选题的 StoryRun 创建冲突，请重新读取后重试")
        raise exc  # pragma: no cover
    db.refresh(story_run)
    return story_run


def _locked_rerun_source(db: Session, source_story_run_id: str) -> tuple[StoryRun, CommerceStoryRunInput, CommerceCreativeIdea]:
    """读取并锁定可重跑的 Commerce 主线输入。

    ``CommerceCreativeIdea`` 也必须行锁：PostgreSQL 中所有从同一创意发起的重跑
    都会在此处串行，从而在计算 ``max(run_number) + 1`` 前避免竞争。SQLite 不支持
    行锁时，0021 的复合唯一约束仍是最终并发兜底，调用方会在受控事务里重新分配。
    """

    source = _locked_story_run(db, source_story_run_id)
    if source.state.status == StoryRunStatus.CANCELLED:
        _error("已取消的 StoryRun 不能作为重跑来源")
    source_input = db.scalars(
        select(CommerceStoryRunInput)
        .where(CommerceStoryRunInput.story_run_id == source.id)
        .with_for_update()
    ).first()
    if source_input is None:
        _error("源 StoryRun 缺少冻结创意输入，不能重跑")
    if source_input.run_number != source.run_number:
        _error("源 StoryRun 的冻结输入编号不一致，不能重跑")

    idea = db.scalars(
        select(CommerceCreativeIdea)
        .where(CommerceCreativeIdea.id == source_input.creative_idea_id)
        .with_for_update()
    ).first()
    if idea is None or idea.status != CommerceCreativeIdeaStatus.SELECTED:
        _error("源 StoryRun 没有关联有效的已选创意，不能重跑")
    if idea.project_id != source.project_id or idea.topic_candidate_id != source.topic_candidate_id:
        _error("源 StoryRun 的创意归属不一致，不能重跑")

    batch = db.get(CommerceCreativeBatch, source_input.creative_batch_id)
    intake = db.get(CommerceReferenceIntake, batch.reference_intake_id) if batch is not None else None
    product = db.get(ProductAssetVersion, source_input.product_asset_version_id)
    selection = db.get(ProjectProductSelection, source.project_product_selection_id)
    frozen = source_input.input_snapshot if isinstance(source_input.input_snapshot, dict) else {}
    expected_snapshot_ids = {
        "reference_analysis": source_input.reference_analysis_id,
        "script_analysis": source_input.script_analysis_version_id,
        "product_asset_version": source_input.product_asset_version_id,
        "creative_idea": source_input.creative_idea_id,
    }
    if (
        batch is None
        or intake is None
        or product is None
        or selection is None
        or source.product_asset_version_id != source_input.product_asset_version_id
        or selection.product_asset_version_id != source.product_asset_version_id
        or batch.project_id != source.project_id
        or product.status != ProductAssetVersionStatus.CONFIRMED
        or product.frozen_at is None
        or any(
            not isinstance(frozen.get(key), dict) or frozen[key].get("id") != value
            for key, value in expected_snapshot_ids.items()
        )
        or not isinstance(frozen.get("creative_batch"), dict)
        or frozen["creative_batch"].get("id") != batch.id
    ):
        _error("源 StoryRun 的冻结输入不完整或已失效，不能重跑")
    return source, source_input, idea


def _next_creative_rerun_number(db: Session, creative_idea_id: str) -> int:
    """从已持久化的创意运行输入计算下一个编号。

    编号权威仍然是 ``StoryRun.run_number``。0021 仅在输入快照上维护经过触发器
    校验的同值镜像，使这一查询与复合唯一约束能覆盖 PostgreSQL 和 SQLite。
    """

    return int(
        db.scalar(
            select(func.max(CommerceStoryRunInput.run_number)).where(
                CommerceStoryRunInput.creative_idea_id == creative_idea_id
            )
        )
        or 0
    ) + 1


def rerun_story_run(
    db: Session,
    *,
    source_story_run_id: str,
    use_current_preset: bool = False,
    preset_key: str | None = None,
    preset_version_id: str | None = None,
    run_overrides: dict[str, Any] | None = None,
) -> tuple[StoryRun, WorkflowRun]:
    """用同一已选创意的新编号创建完全独立的 StoryRun。

    此操作只复制冻结业务输入并创建长期 Commerce 父 ``WorkflowRun``。它绝不创建
    ``WorkflowStep``、ModelInvocation、下游资产或队列投递；新 Run 保持正常的
    ``TOPIC/PENDING`` 初始状态，后续由用户显式 start/continue 触发。
    """

    # PostgreSQL 的创意行锁让正常并发请求顺序分配。SQLite 的 SELECT FOR UPDATE
    # 是无操作，因此在唯一冲突时只回滚本次未提交事务并重新读取编号；不会留下
    # 半成品 StoryRun、输入快照或父 WorkflowRun。
    for retry_index in range(3):
        try:
            source, source_input, idea = _locked_rerun_source(db, source_story_run_id)
            run_number = _next_creative_rerun_number(db, idea.id)
            if use_current_preset:
                resolved_config = resolve_story_run_workflow_config(
                    db,
                    preset_key=preset_key,
                    preset_version_id=preset_version_id,
                    run_overrides=run_overrides,
                )
                rerun_mode = StoryRunMode(resolved_config["effective_workflow_config"]["execution_mode"])
            else:
                resolved_config = None
                rerun_mode = source.mode
            new_run = create_story_run(
                db,
                project_id=source.project_id,
                topic_candidate_id=source.topic_candidate_id,
                project_product_selection_id=source.project_product_selection_id,
                product_asset_version_id=source.product_asset_version_id,
                run_number=run_number,
                mode=rerun_mode,
            )
            if resolved_config is not None:
                freeze_story_run_workflow_config(db, story_run=new_run, resolved=resolved_config)
            else:
                copy_story_run_workflow_config(db, source_story_run=source, target_story_run=new_run)
            frozen_input = deepcopy(source_input.input_snapshot)
            frozen_input["rerun"] = {
                "source_story_run_id": source.id,
                "source_run_number": source.run_number,
                "created_at": utcnow().isoformat(),
            }
            db.add(
                CommerceStoryRunInput(
                    story_run_id=new_run.id,
                    creative_batch_id=source_input.creative_batch_id,
                    creative_idea_id=source_input.creative_idea_id,
                    run_number=run_number,
                    reference_analysis_id=source_input.reference_analysis_id,
                    script_analysis_version_id=source_input.script_analysis_version_id,
                    product_asset_version_id=source_input.product_asset_version_id,
                    input_snapshot=frozen_input,
                )
            )
            # 先 flush 输入复合唯一约束，再创建父运行。冲突时不会产生父运行或
            # 下游资产；成功路径中二者与 StoryRun 同一事务提交。
            db.flush()
            workflow_run, created = _ensure_commerce_workflow(db, new_run)
            if not created:
                raise RuntimeError("新 StoryRun 未能创建独立 Commerce 父工作流")
            db.commit()
            db.refresh(new_run)
            return new_run, workflow_run
        except IntegrityError:
            db.rollback()
            if retry_index == 2:
                _error("同一创意的 StoryRun 重跑并发冲突，请稍后重试")
        except OperationalError as exc:
            # SQLite 没有行锁；两个写事务恰好重叠时可能短暂返回 database is
            # locked。该分支只重试尚未提交的本地事务，绝不重复提交已成功创建的 Run。
            db.rollback()
            if "locked" not in str(exc).lower() or retry_index == 2:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="创建重跑 StoryRun 时数据库暂不可用，请稍后重试",
                ) from exc
            sleep(0.02 * (retry_index + 1))
        except HTTPException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="无法创建重跑的 Commerce 父工作流，请检查工作流基础设施后重试",
            ) from exc
    raise AssertionError("unreachable")  # pragma: no cover


def _active_run(db: Session, story_run_id: str) -> WorkflowRun | None:
    """保留旧名字供状态服务使用，返回单一 Commerce 父运行而非“活动阶段”。"""

    return db.scalars(
        select(WorkflowRun)
        .join(CommerceWorkflowLink, CommerceWorkflowLink.workflow_run_id == WorkflowRun.id)
        .where(
            CommerceWorkflowLink.story_run_id == story_run_id,
            WorkflowRun.workflow_key == COMMERCE_WORKFLOW_KEY,
        )
    ).first()


def _attempt_for_stage(db: Session, story_run_id: str, stage: StoryRunStage) -> int:
    return int(
        db.scalar(
        select(func.max(WorkflowStep.attempt))
        .join(WorkflowRun, WorkflowStep.workflow_run_id == WorkflowRun.id)
        .join(CommerceWorkflowStep, CommerceWorkflowStep.workflow_step_id == WorkflowStep.id)
        .where(CommerceWorkflowStep.story_run_id == story_run_id, CommerceWorkflowStep.stage == stage.value)
        )
        or 0
    ) + 1


def _ensure_commerce_workflow(db: Session, story_run: StoryRun) -> tuple[WorkflowRun, bool]:
    """创建一次且仅一次的 Commerce 父运行；不在此处创建节点 attempt。"""

    existing = _active_run(db, story_run.id)
    if existing is not None:
        return existing, False
    definition = ensure_commerce_foundation(db)
    root_snapshot = {
        "frozen_at": utcnow().isoformat(),
        "commerce": {
            "story_run_id": story_run.id,
            "project_id": story_run.project_id,
            "topic_candidate_id": story_run.topic_candidate_id,
            "project_product_selection_id": story_run.project_product_selection_id,
            "product_asset_version_id": story_run.product_asset_version_id,
            "mode": story_run.mode.value,
        },
        "workflow_definition": {
            "id": definition.id,
            "workflow_code": definition.workflow_code,
            "version": definition.version,
            "definition_json": deepcopy(definition.definition_json),
        },
    }
    workflow_config = story_run_workflow_config_snapshot(story_run)
    if workflow_config is not None:
        # 父运行保存同一份不可变配置；子步骤只从此/自身冻结快照读取，Worker 不会
        # 重新查询当前活动预设、Prompt 或模型中心。
        root_snapshot["workflow_config"] = workflow_config
    semantic = f"commerce:{story_run.id}:workflow"
    run = WorkflowRun(
        project_id=story_run.project_id,
        workflow_key=COMMERCE_WORKFLOW_KEY,
        workflow_definition_id=definition.id,
        workflow_version=definition.version,
        idempotency_key=f"run:{sha256(semantic.encode()).hexdigest()}",
        input_snapshot=root_snapshot,
        status=RunStatus.PENDING,
    )
    db.add(run)
    try:
        db.flush()
        db.add(CommerceWorkflowLink(workflow_run_id=run.id, story_run_id=story_run.id))
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = _active_run(db, story_run.id)
        if existing is not None:
            return existing, False
        _error("创建 Commerce 工作流冲突，请重试")
    return run, True


def _active_step(db: Session, workflow_run_id: str) -> WorkflowStep | None:
    return db.scalars(
        select(WorkflowStep)
        .where(
            WorkflowStep.workflow_run_id == workflow_run_id,
            WorkflowStep.status.in_((RunStatus.PENDING, RunStatus.RUNNING)),
        )
        .order_by(WorkflowStep.position, WorkflowStep.attempt.desc(), WorkflowStep.created_at.desc())
    ).first()


def _set_commerce_step_status(db: Session, workflow_step_id: str, status_value: RunStatus) -> None:
    """在同一事务内同步 0012 Commerce sidecar 的真实步骤状态。

    生产迁移会由 ``workflow_steps`` 触发器完成这项同步；但 ``Base.metadata``
    的快速测试库没有触发器。先 ``flush`` 真实 ``WorkflowStep``，再更新 sidecar，
    既令两类数据库行为一致，也确保 sidecar 的部分唯一索引不会比真实步骤更早或
    更晚看到状态变化。若 Commerce 步骤缺少 sidecar，直接抛错并由调用方事务回滚，
    不允许形成不可审计的半状态。
    """

    db.flush()
    updated = db.execute(
        update(CommerceWorkflowStep)
        .where(CommerceWorkflowStep.workflow_step_id == workflow_step_id)
        .values(status=status_value.value)
    ).rowcount
    if updated != 1:
        raise RuntimeError("Commerce WorkflowStep 缺少唯一 sidecar 关联")


def _claim_story_run_state(
    db: Session,
    story_run: StoryRun,
    *,
    expected_status: StoryRunStatus,
    expected_stage: StoryRunStage,
) -> tuple[StoryRun, WorkflowRun | None]:
    """原子领取阶段推进权，避免 SQLite 忽略 ``FOR UPDATE`` 时出现双 attempt。

    PostgreSQL 行锁仍然减少竞争；条件 UPDATE 则同时覆盖 SQLite 和多个 API/Worker
    进程。领取失败时若另一个请求已经创建当前活动步骤，调用方必须直接返回它。
    """

    claimed = db.execute(
        update(StoryRunState)
        .where(
            StoryRunState.story_run_id == story_run.id,
            StoryRunState.status == expected_status,
            StoryRunState.current_stage == expected_stage,
        )
        .values(status=StoryRunStatus.RUNNING, stage_data={"blocked_reason": None})
    ).rowcount
    if claimed == 1:
        db.flush()
        db.refresh(story_run)
        return story_run, None
    db.rollback()
    refreshed = _locked_story_run(db, story_run.id)
    parent = _active_run(db, refreshed.id)
    if parent is not None and _active_step(db, parent.id) is not None:
        return refreshed, parent
    _error("当前状态不能领取阶段任务")
    raise AssertionError("unreachable")  # pragma: no cover


def _create_stage_step(db: Session, story_run: StoryRun, stage: StoryRunStage) -> tuple[WorkflowRun, bool]:
    """在单一父 WorkflowRun 中追加当前阶段的新 attempt。"""

    run, _ = _ensure_commerce_workflow(db, story_run)
    if _active_step(db, run.id) is not None:
        return run, False
    snapshot = _freeze_execution_snapshot(db, story_run, stage)
    attempt = _attempt_for_stage(db, story_run.id, stage)
    semantic = f"commerce:{story_run.id}:{stage.value}:{attempt}"
    step = WorkflowStep(
        workflow_run=run,
        step_key=stage.value,
        position=STAGES.index(stage) + 1,
        attempt=attempt,
        input_payload=deepcopy(snapshot),
        model_profile_snapshot={"model_bindings": deepcopy(snapshot["model_bindings"]), "prompt_templates": deepcopy(snapshot["prompt_templates"])},
        idempotency_key=f"step:{sha256(semantic.encode()).hexdigest()}",
    )
    db.add(step)
    try:
        db.flush()
        db.add(
            CommerceWorkflowStep(
                workflow_step_id=step.id,
                workflow_run_id=run.id,
                story_run_id=story_run.id,
                stage=stage.value,
                attempt=attempt,
                status=RunStatus.PENDING.value,
            )
        )
        db.flush()
    except IntegrityError:
        db.rollback()
        run = _active_run(db, story_run.id)
        if run is not None and _active_step(db, run.id) is not None:
            return run, False
        _error("创建 Commerce 节点任务冲突，请重试")
    return run, True


def _validate_start_bindings(db: Session, story_run: StoryRun) -> None:
    validate_story_run_bindings(
        db,
        project_id=story_run.project_id,
        topic_candidate_id=story_run.topic_candidate_id,
        project_product_selection_id=story_run.project_product_selection_id,
        product_asset_version_id=story_run.product_asset_version_id,
    )


def start_story_run(db: Session, story_run_id: str) -> tuple[StoryRun, WorkflowRun, bool]:
    """确认 TOPIC 输入并创建首个 OUTLINE attempt；重复 start 返回活动任务。"""

    story_run = _locked_story_run(db, story_run_id)
    _require_not_terminal(story_run)
    parent = _active_run(db, story_run.id)
    if parent is not None:
        active_step = _active_step(db, parent.id)
        if active_step is not None:
            return story_run, parent, False
    if story_run.state.current_stage != StoryRunStage.TOPIC or story_run.state.status != StoryRunStatus.PENDING:
        _error("当前 StoryRun 不能再次启动")
    _validate_start_bindings(db, story_run)
    story_run, concurrent_parent = _claim_story_run_state(
        db, story_run, expected_status=StoryRunStatus.PENDING, expected_stage=StoryRunStage.TOPIC
    )
    if concurrent_parent is not None:
        return story_run, concurrent_parent, False
    # TOPIC 虽不调用模型，也必须有可查询的 WorkflowStep，避免七节点中出现不可
    # 审计的“隐形阶段”。该 step 保存的是重新校验后的输入归属，而非自由文本标记。
    topic_run, topic_created = _create_stage_step(db, story_run, StoryRunStage.TOPIC)
    if topic_created:
        topic_step = _active_step(db, topic_run.id)
        assert topic_step is not None
        now = utcnow()
        topic_run.started_at = topic_run.started_at or now
        topic_step.status = RunStatus.SUCCEEDED
        _set_commerce_step_status(db, topic_step.id, RunStatus.SUCCEEDED)
        topic_step.progress = 100
        topic_step.started_at = now
        topic_step.finished_at = now
        topic_step.output_payload = {
            "artifact_references": {
                "topic_candidate_id": story_run.topic_candidate_id,
                "project_product_selection_id": story_run.project_product_selection_id,
                "product_asset_version_id": story_run.product_asset_version_id,
            },
            "structured_output": {"validated": True},
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "cost": {"amount": 0, "currency": "CNY"},
            "provider_task": None,
        }
        # TOPIC 的 attempt 已完成，再创建 OUTLINE attempt。父 WorkflowRun 保持
        # PENDING，直到 StoryRun 完整完成/取消。
        db.flush()
    db.add(
        ReviewDecision(
            project_id=story_run.project_id,
            target_type="COMMERCE_TOPIC_INPUT",
            target_id=story_run.id,
            decision="ACCEPTED",
            reviewer_label="系统输入校验",
            note="选题和冻结产品版本已校验",
        )
    )
    story_run.state.current_stage = StoryRunStage.OUTLINE
    story_run.state.status = StoryRunStatus.RUNNING
    story_run.state.stage_data = {"blocked_reason": None}
    run, created = _create_stage_step(db, story_run, StoryRunStage.OUTLINE)
    db.commit()
    db.refresh(story_run)
    return story_run, run, created


def continue_story_run(db: Session, story_run_id: str) -> tuple[StoryRun, WorkflowRun, bool]:
    """显式投递当前待执行阶段；STEPWISE 必须经确认后才可调用。"""

    story_run = _locked_story_run(db, story_run_id)
    _require_not_terminal(story_run)
    parent = _active_run(db, story_run.id)
    if parent is not None and _active_step(db, parent.id) is not None:
        return story_run, parent, False
    if story_run.state.status != StoryRunStatus.PENDING or story_run.state.current_stage in {
        StoryRunStage.TOPIC,
        StoryRunStage.COMPLETED,
    }:
        _error("当前状态不能继续投递节点")
    story_run, concurrent_parent = _claim_story_run_state(
        db,
        story_run,
        expected_status=StoryRunStatus.PENDING,
        expected_stage=story_run.state.current_stage,
    )
    if concurrent_parent is not None:
        return story_run, concurrent_parent, False
    run, created = _create_stage_step(db, story_run, story_run.state.current_stage)
    db.commit()
    db.refresh(story_run)
    return story_run, run, created


def pause_story_run(db: Session, story_run_id: str) -> StoryRun:
    story_run = _locked_story_run(db, story_run_id)
    _require_not_terminal(story_run)
    parent = _active_run(db, story_run.id)
    if parent is not None and _active_step(db, parent.id) is not None:
        _error("当前节点正在执行，不能强制暂停；请等待其结束或取消")
    if story_run.state.status != StoryRunStatus.PENDING:
        _error("只有待投递状态可以手动暂停")
    story_run.state.status = StoryRunStatus.PAUSED
    story_run.state.stage_data = {"blocked_reason": "manual_pause"}
    db.commit()
    return story_run


def resume_story_run(db: Session, story_run_id: str) -> StoryRun:
    story_run = _locked_story_run(db, story_run_id)
    _require_not_terminal(story_run)
    if story_run.state.status != StoryRunStatus.PAUSED or (story_run.state.stage_data or {}).get("blocked_reason") != "manual_pause":
        _error("当前 StoryRun 并非手动暂停状态")
    story_run.state.status = StoryRunStatus.PENDING
    story_run.state.stage_data = {"blocked_reason": None}
    db.commit()
    return story_run


def cancel_story_run(db: Session, story_run_id: str) -> StoryRun:
    story_run = _locked_story_run(db, story_run_id)
    _require_not_terminal(story_run)
    active = _active_run(db, story_run.id)
    if active is not None:
        active.status = RunStatus.CANCELLED
        active.finished_at = utcnow()
        for step in active.steps:
            if step.status in {RunStatus.PENDING, RunStatus.RUNNING}:
                step.status = RunStatus.CANCELLED
                _set_commerce_step_status(db, step.id, RunStatus.CANCELLED)
                step.finished_at = active.finished_at
    story_run.state.status = StoryRunStatus.CANCELLED
    story_run.state.stage_data = {"blocked_reason": "cancelled_by_user"}
    db.commit()
    return story_run


def _latest_successful_step(db: Session, story_run_id: str, stage: StoryRunStage) -> WorkflowStep:
    step = db.scalars(
        select(WorkflowStep)
        .join(WorkflowRun, WorkflowStep.workflow_run_id == WorkflowRun.id)
        .join(CommerceWorkflowLink, CommerceWorkflowLink.workflow_run_id == WorkflowRun.id)
        .where(
            CommerceWorkflowLink.story_run_id == story_run_id,
            WorkflowStep.step_key == stage.value,
            WorkflowStep.status == RunStatus.SUCCEEDED,
        )
        .order_by(WorkflowStep.attempt.desc(), WorkflowStep.created_at.desc())
    ).first()
    if step is None:
        _error("当前阶段没有可审核的成功结果")
    return step


def _artifact_id(step: WorkflowStep, key: str) -> str | None:
    value = (step.output_payload or {}).get("artifact_references", {}).get(key)
    return value if isinstance(value, str) else None


def _chapter_attempt_chapters(
    db: Session,
    story_run: StoryRun,
    step: WorkflowStep,
    *,
    outline_id: str | None = None,
) -> list[ChapterPlan]:
    """读取一个 ``CHAPTERS`` attempt 专属的章节结果组。

    Phase 1 的 ``ChapterPlan`` 不带 attempt 列，且同一 StoryRun 章节号唯一。新表把
    追加章节关联到生成它的 ``WorkflowStep``，使被驳回 attempt 的章节永久可追溯、
    但不会被当前确认或后续分镜混入。``position`` 是当前结果组内的顺序；实体自身的
    ``chapter_number`` 继续保留 Phase 1 的全局唯一语义。
    """

    refs = (step.output_payload or {}).get("artifact_references", {})
    chapter_ids = refs.get("chapter_ids")
    if (
        step.step_key != StoryRunStage.CHAPTERS.value
        or not isinstance(chapter_ids, list)
        or not chapter_ids
        or len(set(chapter_ids)) != len(chapter_ids)
    ):
        _error("章节结果缺少当前 attempt 的章节引用", status.HTTP_422_UNPROCESSABLE_CONTENT)
    links = list(
        db.scalars(
            select(CommerceChapterAttemptChapter)
            .where(
                CommerceChapterAttemptChapter.workflow_step_id == step.id,
                CommerceChapterAttemptChapter.story_run_id == story_run.id,
            )
            .order_by(CommerceChapterAttemptChapter.position)
        ).all()
    )
    if (
        len(links) != len(chapter_ids)
        or {item.chapter_plan_id for item in links} != set(chapter_ids)
        or [item.position for item in links] != list(range(1, len(links) + 1))
    ):
        _error("章节结果与当前 attempt 的版本组不一致", status.HTTP_422_UNPROCESSABLE_CONTENT)
    if outline_id is not None and any(item.outline_version_id != outline_id for item in links):
        _error("章节结果没有全部基于当前锁定大纲", status.HTTP_422_UNPROCESSABLE_CONTENT)
    chapters = {item.id: item for item in db.scalars(select(ChapterPlan).where(ChapterPlan.id.in_(chapter_ids))).all()}
    if len(chapters) != len(chapter_ids):
        _error("章节结果引用了不存在的章节", status.HTTP_422_UNPROCESSABLE_CONTENT)
    ordered = [chapters[item.chapter_plan_id] for item in links]
    if any(
        item.story_run_id != story_run.id
        or item.outline_version_id != link.outline_version_id
        for item, link in zip(ordered, links)
    ):
        _error("章节结果归属或大纲版本无效", status.HTTP_422_UNPROCESSABLE_CONTENT)
    return ordered


def _validate_chapters(db: Session, story_run: StoryRun, step: WorkflowStep, outline_id: str) -> list[ChapterPlan]:
    return _chapter_attempt_chapters(db, story_run, step, outline_id=outline_id)


def _validate_storyboard(db: Session, story_run: StoryRun, step: WorkflowStep) -> None:
    refs = (step.output_payload or {}).get("artifact_references", {})
    mapping = db.get(SceneMappingVersion, refs.get("scene_mapping_id"))
    chapter_ids = refs.get("chapter_ids") or []
    segment_ids = refs.get("video_segment_ids") or []
    dialogue_ids = refs.get("dialogue_line_ids") or []
    placement_ids = refs.get("product_placement_ids") or []
    if (
        mapping is None
        or mapping.story_run_id != story_run.id
        or not isinstance(chapter_ids, list)
        or not chapter_ids
        or not isinstance(segment_ids, list)
        or not segment_ids
        or not isinstance(dialogue_ids, list)
        or not isinstance(placement_ids, list)
        or not placement_ids
        or len(set(chapter_ids)) != len(chapter_ids)
        or len(set(segment_ids)) != len(segment_ids)
        or len(set(placement_ids)) != len(placement_ids)
    ):
        _error("分镜结果缺少当前章节、场景映射、片段或产品植入引用", status.HTTP_422_UNPROCESSABLE_CONTENT)
    # AUTO 已在 CHAPTERS 成功时完成同一套完整性校验，但没有伪造人工
    # ReviewDecision；因此它采用当前成功 attempt。STEPWISE 仍严格只使用人工
    # 确认的 attempt。
    accepted_chapter_step = (
        _latest_successful_step(db, story_run.id, StoryRunStage.CHAPTERS)
        if story_run.mode == StoryRunMode.AUTO
        else _latest_approved_step(db, story_run.id, StoryRunStage.CHAPTERS)
    )
    current_chapters = _chapter_attempt_chapters(
        db,
        story_run,
        accepted_chapter_step,
        outline_id=_artifact_id(accepted_chapter_step, "outline_id"),
    )
    if set(chapter_ids) != {item.id for item in current_chapters}:
        _error("分镜只能使用当前已确认章节 attempt 的结果", status.HTTP_422_UNPROCESSABLE_CONTENT)
    segments = list(db.scalars(select(VideoSegmentPlan).where(VideoSegmentPlan.id.in_(segment_ids))).all())
    if len(segments) != len(segment_ids):
        _error("分镜结果引用了不存在的视频片段", status.HTTP_422_UNPROCESSABLE_CONTENT)
    segments_by_id = {segment.id: segment for segment in segments}
    for segment in segments:
        if segment.story_run_id != story_run.id or segment.target_duration_ms < 4000 or segment.target_duration_ms > 15000:
            _error("分镜视频片段归属或时长无效", status.HTTP_422_UNPROCESSABLE_CONTENT)
        chapter = db.get(ChapterPlan, segment.chapter_id)
        if chapter is None or chapter.story_run_id != story_run.id or chapter.id not in set(chapter_ids):
            _error("分镜视频片段引用了其他 StoryRun 的章节", status.HTTP_422_UNPROCESSABLE_CONTENT)
        for shot in segment.sub_shots:
            if shot.end_ms > segment.target_duration_ms:
                _error("子镜头超出所属片段时长", status.HTTP_422_UNPROCESSABLE_CONTENT)
    sub_shot_ids = [shot.id for segment in segments for shot in segment.sub_shots]
    placements = list(
        db.scalars(select(ProductPlacementPlan).where(ProductPlacementPlan.id.in_(placement_ids))).all()
    )
    if len(placements) != len(placement_ids):
        _error("分镜结果引用了不存在的产品植入", status.HTTP_422_UNPROCESSABLE_CONTENT)
    placement_by_id = {item.id: item for item in placements}
    if set(placement_by_id) != set(placement_ids):
        _error("分镜产品植入引用重复或不完整", status.HTTP_422_UNPROCESSABLE_CONTENT)
    for placement in placements:
        if (
            placement.story_run_id != story_run.id
            or placement.product_asset_version_id != story_run.product_asset_version_id
        ):
            _error("分镜产品植入引用了错误的冻结产品版本", status.HTTP_422_UNPROCESSABLE_CONTENT)
        if placement.chapter_id is not None and placement.chapter_id not in set(chapter_ids):
            _error("章节级产品植入不属于当前审核章节结果", status.HTTP_422_UNPROCESSABLE_CONTENT)
        if placement.video_segment_id is not None and placement.video_segment_id not in set(segment_ids):
            _error("片段级产品植入不属于当前审核分镜结果", status.HTTP_422_UNPROCESSABLE_CONTENT)
        if placement.sub_shot_id is not None and placement.sub_shot_id not in set(sub_shot_ids):
            _error("子镜头级产品植入不属于当前审核分镜结果", status.HTTP_422_UNPROCESSABLE_CONTENT)

    # ``product_placement_ids`` 是本次 Storyboard attempt 的明确采用集合。不能反向
    # 扫描同章节的全部历史植入：CHAPTERS/Storyboard 被驳回后，旧 attempt 的章节级
    # 植入仍应永久留档，但绝不能强迫新 attempt 再次采用它。后续阶段只从当前已确认
    # Storyboard 输出的这组 ID 读取植入，不会隐式混入历史记录。

    # 对白是可独立查询的生产数据，不能只因数据库中存在就被默认视为本次分镜的
    # 输出。节点必须显式列出本次采用的对白 ID；这样审核能够拒绝跨 StoryRun、跨
    # 片段或越出子镜头时间轴的引用，并且错误不会修改审核记录或阶段状态。
    dialogue_lines = list(
        db.scalars(select(DialogueLine).where(DialogueLine.id.in_(dialogue_ids))).all()
    ) if dialogue_ids else []
    if len(dialogue_lines) != len(dialogue_ids):
        _error("分镜结果引用了不存在的对白", status.HTTP_422_UNPROCESSABLE_CONTENT)
    for line in dialogue_lines:
        if line.video_segment_id is not None:
            owner_segment = segments_by_id.get(line.video_segment_id)
            if owner_segment is None or line.end_ms > owner_segment.target_duration_ms:
                _error("对白不属于当前分镜片段或超出片段时长", status.HTTP_422_UNPROCESSABLE_CONTENT)
            continue
        if line.sub_shot_id is None:
            _error("对白缺少片段或子镜头归属", status.HTTP_422_UNPROCESSABLE_CONTENT)
        owner_shot = db.get(SubShotPlan, line.sub_shot_id)
        if owner_shot is None:
            _error("对白引用的子镜头不存在", status.HTTP_422_UNPROCESSABLE_CONTENT)
        owner_segment = segments_by_id.get(owner_shot.video_segment_id)
        if (
            owner_segment is None
            or line.start_ms < owner_shot.start_ms
            or line.end_ms > owner_shot.end_ms
            or line.end_ms > owner_segment.target_duration_ms
        ):
            _error("对白不属于当前子镜头或超出子镜头时间范围", status.HTTP_422_UNPROCESSABLE_CONTENT)


def _validate_visual_references(db: Session, story_run: StoryRun, step: WorkflowStep) -> None:
    """资产由现有资产中心维护；若本节点声明引用，必须是本项目已锁定引用。"""

    refs = (step.output_payload or {}).get("artifact_references", {})
    character_ids = refs.get("project_character_reference_ids") or []
    scene_ids = refs.get("project_scene_reference_ids") or []
    if not isinstance(character_ids, list) or not isinstance(scene_ids, list):
        _error("视觉资产引用格式无效", status.HTTP_422_UNPROCESSABLE_CONTENT)
    if character_ids or scene_ids:
        from app.models import ProjectCharacterAssetReference, ProjectSceneAssetReference

        characters = list(db.scalars(select(ProjectCharacterAssetReference).where(ProjectCharacterAssetReference.id.in_(character_ids))).all())
        scenes = list(db.scalars(select(ProjectSceneAssetReference).where(ProjectSceneAssetReference.id.in_(scene_ids))).all())
        if len(characters) != len(character_ids) or len(scenes) != len(scene_ids):
            _error("视觉资产引用不存在", status.HTTP_422_UNPROCESSABLE_CONTENT)
        if any(item.project_id != story_run.project_id or item.locked_at is None for item in characters + scenes):
            _error("视觉资产必须是本项目已锁定的版本", status.HTTP_422_UNPROCESSABLE_CONTENT)


def _accepted_storyboard_segments(db: Session, story_run: StoryRun) -> list[VideoSegmentPlan]:
    """返回已确认 Storyboard 的完整片段集合，而不是任意历史片段。"""

    storyboard = _latest_approved_step(db, story_run.id, StoryRunStage.STORYBOARD)
    _validate_storyboard(db, story_run, storyboard)
    ids = (storyboard.output_payload or {}).get("artifact_references", {}).get("video_segment_ids") or []
    segments = {item.id: item for item in db.scalars(select(VideoSegmentPlan).where(VideoSegmentPlan.id.in_(ids))).all()}
    return [segments[item_id] for item_id in ids]


def _prompt_versions_for_step(db: Session, story_run: StoryRun, step: WorkflowStep) -> list[VideoPromptVersion]:
    ids = (step.output_payload or {}).get("artifact_references", {}).get("video_prompt_version_ids") or []
    versions = list(db.scalars(select(VideoPromptVersion).where(VideoPromptVersion.id.in_(ids))).all()) if isinstance(ids, list) else []
    if not versions or len(versions) != len(ids):
        _error("视频提示词结果不完整", status.HTTP_422_UNPROCESSABLE_CONTENT)
    if any(item.video_segment.story_run_id != story_run.id or item.workflow_step_id != step.id for item in versions):
        _error("视频提示词不能引用其他 StoryRun 的片段", status.HTTP_422_UNPROCESSABLE_CONTENT)
    return versions


def _validate_video_prompts(db: Session, story_run: StoryRun, step: WorkflowStep) -> list[VideoPromptVersion]:
    versions = _prompt_versions_for_step(db, story_run, step)
    segments = _accepted_storyboard_segments(db, story_run)
    expected_ids = {item.id for item in segments}
    actual_ids = [item.video_segment_id for item in versions]
    if len(set(actual_ids)) != len(actual_ids) or set(actual_ids) != expected_ids:
        _error("视频提示词必须完整覆盖已确认分镜的全部视频片段", status.HTTP_422_UNPROCESSABLE_CONTENT)
    if any(item.status not in {"DRAFT", "LOCKED"} for item in versions):
        _error("视频提示词版本已被驳回或替代，不能再次采用", status.HTTP_422_UNPROCESSABLE_CONTENT)
    return versions


def _lock_video_prompts(db: Session, story_run: StoryRun, step: WorkflowStep) -> list[VideoPromptVersion]:
    versions = _validate_video_prompts(db, story_run, step)
    now = utcnow()
    for item in versions:
        item.status = "LOCKED"
        item.locked_at = now
    return versions


def _adopted_video_prompt_step(db: Session, story_run: StoryRun) -> WorkflowStep:
    """返回可用于渲染的当前提示词 attempt。

    STEPWISE 的采用事实必须来自人工 ``APPROVED`` 审核；AUTO 没有伪造人工审核，
    因此只采用最新成功且已通过完整性校验并锁定的 attempt。两种模式都由
    ``_validate_video_prompts`` 复核完整覆盖，避免把历史 DRAFT/REJECTED 版本带入
    渲染。
    """

    step = (
        _latest_successful_step(db, story_run.id, StoryRunStage.VIDEO_PROMPTS)
        if story_run.mode == StoryRunMode.AUTO
        else _latest_approved_step(db, story_run.id, StoryRunStage.VIDEO_PROMPTS)
    )
    versions = _validate_video_prompts(db, story_run, step)
    if any(item.status != "LOCKED" or item.locked_at is None for item in versions):
        _error("批量渲染缺少当前已锁定的视频提示词", status.HTTP_409_CONFLICT)
    return step


def _validate_render(db: Session, story_run: StoryRun, step: WorkflowStep) -> RenderBatch:
    batch_id = _artifact_id(step, "render_batch_id")
    batch = db.get(RenderBatch, batch_id) if batch_id else None
    if batch is None or batch.story_run_id != story_run.id:
        _error("批量渲染结果不存在或不属于当前 StoryRun", status.HTTP_422_UNPROCESSABLE_CONTENT)
    accepted_segments = _accepted_storyboard_segments(db, story_run)
    if (
        batch.status != RenderBatchStatus.COMPLETED
        or batch.total_tasks <= 0
        or batch.running_tasks
        or batch.failed_tasks
        or batch.completed_tasks != batch.total_tasks
        or batch.total_tasks != len(accepted_segments)
    ):
        _error("批量渲染尚未全部成功，不能完成 StoryRun", status.HTTP_409_CONFLICT)
    if any(segment.status != SegmentPlanStatus.COMPLETED for segment in accepted_segments):
        _error("已接受分镜仍有片段没有成功渲染结果", status.HTTP_409_CONFLICT)
    return batch


def _ensure_no_review(db: Session, step: WorkflowStep) -> None:
    exists = db.scalar(
        select(ReviewDecision.id).where(
            ReviewDecision.target_type == f"COMMERCE_STAGE_{step.step_key}", ReviewDecision.target_id == step.id
        )
    )
    if exists is not None:
        _error("该阶段结果已经审核，不能重复确认或驳回")


def review_stage(
    db: Session,
    *,
    story_run_id: str,
    stage: StoryRunStage,
    decision: str,
    reviewer_label: str,
    note: str | None,
    quality_score: int | None,
    outline_id: str | None = None,
) -> tuple[StoryRun, bool]:
    """写入审核审计，并只按顺序推进当前阶段。返回是否需要 AUTO 后续投递。"""

    story_run = _locked_story_run(db, story_run_id)
    _require_not_terminal(story_run)
    if story_run.state.current_stage != stage or story_run.state.status != StoryRunStatus.PAUSED:
        _error("当前阶段或状态不允许审核")
    step = _latest_successful_step(db, story_run.id, stage)
    _ensure_no_review(db, step)
    accepted = decision.upper() in {"CONFIRMED", "APPROVED"}
    if not accepted and decision.upper() not in {"REJECTED", "REJECT"}:
        _error("审核决定仅支持 CONFIRMED 或 REJECTED", status.HTTP_422_UNPROCESSABLE_CONTENT)
    if quality_score is not None and not 1 <= quality_score <= 10:
        _error("质量评分必须为 1 到 10", status.HTTP_422_UNPROCESSABLE_CONTENT)

    if accepted:
        if stage == StoryRunStage.OUTLINE:
            selected_id = outline_id or _artifact_id(step, "outline_id")
            outline = db.get(StoryOutlineVersion, selected_id) if selected_id else None
            if outline is None or outline.story_run_id != story_run.id or outline.status != OutlineVersionStatus.DRAFT:
                _error("待锁定的大纲不存在、归属错误或不可再锁定", status.HTTP_422_UNPROCESSABLE_CONTENT)
            transition_story_outline_version_status(db, story_outline_version_id=outline.id, next_status=OutlineVersionStatus.LOCKED)
        elif stage == StoryRunStage.CHAPTERS:
            locked = db.scalars(select(StoryOutlineVersion).where(StoryOutlineVersion.story_run_id == story_run.id, StoryOutlineVersion.status == OutlineVersionStatus.LOCKED)).first()
            if locked is None:
                _error("章节必须基于已锁定大纲", status.HTTP_422_UNPROCESSABLE_CONTENT)
            if _artifact_id(step, "outline_id") != locked.id:
                _error("章节结果没有引用当前锁定大纲", status.HTTP_422_UNPROCESSABLE_CONTENT)
            _validate_chapters(db, story_run, step, locked.id)
        elif stage == StoryRunStage.STORYBOARD:
            _validate_storyboard(db, story_run, step)
        elif stage == StoryRunStage.VISUAL_ASSETS:
            _validate_visual_references(db, story_run, step)
        elif stage == StoryRunStage.VIDEO_PROMPTS:
            _lock_video_prompts(db, story_run, step)
        elif stage == StoryRunStage.SEGMENT_RENDER:
            _validate_render(db, story_run, step)
    else:
        if stage == StoryRunStage.OUTLINE:
            candidate_id = outline_id or _artifact_id(step, "outline_id")
            outline = db.get(StoryOutlineVersion, candidate_id) if candidate_id else None
            if outline is not None and outline.story_run_id == story_run.id and outline.status == OutlineVersionStatus.DRAFT:
                transition_story_outline_version_status(db, story_outline_version_id=outline.id, next_status=OutlineVersionStatus.SUPERSEDED)
        elif stage == StoryRunStage.VIDEO_PROMPTS:
            # 驳回不覆盖提示词版本；仅使它退出“当前可采用”集合。下一次 attempt 会
            # 创建新的版本并由 WorkflowStep 显式追溯。
            for prompt in _prompt_versions_for_step(db, story_run, step):
                prompt.status = "REJECTED"
                prompt.locked_at = None

    db.add(ReviewDecision(
        project_id=story_run.project_id,
        target_type=f"COMMERCE_STAGE_{stage.value}",
        target_id=step.id,
        decision="APPROVED" if accepted else "REJECTED",
        reviewer_label=(reviewer_label or "人工审核")[:120],
        note=(note or None),
        quality_score=quality_score,
    ))
    if not accepted:
        story_run.state.status = StoryRunStatus.PENDING
        story_run.state.stage_data = {"blocked_reason": "rejected", "rejected_step_id": step.id}
        db.commit()
        return story_run, False
    next_stage = _next_stage(stage)
    if next_stage == StoryRunStage.COMPLETED:
        story_run.state.current_stage = StoryRunStage.COMPLETED
        story_run.state.status = StoryRunStatus.COMPLETED
        story_run.state.stage_data = {"blocked_reason": None}
        parent = _active_run(db, story_run.id)
        if parent is not None:
            parent.status = RunStatus.SUCCEEDED
            parent.finished_at = utcnow()
        db.commit()
        return story_run, False
    story_run.state.current_stage = next_stage
    story_run.state.status = StoryRunStatus.PENDING
    story_run.state.stage_data = {"blocked_reason": None}
    should_dispatch = story_run.mode == StoryRunMode.AUTO
    db.commit()
    return story_run, should_dispatch


def retry_step(db: Session, story_run_id: str, step_id: str) -> tuple[StoryRun, WorkflowRun, bool]:
    story_run = _locked_story_run(db, story_run_id)
    _require_not_terminal(story_run)
    step = db.get(WorkflowStep, step_id)
    if step is None or step.workflow_run.commerce_link is None or step.workflow_run.commerce_link.story_run_id != story_run.id:
        _error("WorkflowStep 不存在或不属于当前 StoryRun", status.HTTP_404_NOT_FOUND)
    # Worker 可能已在另一会话把同一 attempt 置为 FAILED。重试绝不能依据调用方
    # identity-map 中的旧 PENDING/RUNNING 值判断目标。
    db.refresh(step)
    stage = StoryRunStage(step.step_key)

    # 幂等返回之前必须先证明调用方传入的是“当前阶段的失败 attempt”。否则旧的
    # SUCCEEDED/REJECTED step，或 attempt 1 之后已出现 attempt 3 的 stale step，
    # 都可能被错误视为成功的重复请求。
    if step.status != RunStatus.FAILED or story_run.state.current_stage != stage:
        _error("只有当前失败节点可以重试", status.HTTP_409_CONFLICT)

    parent = _active_run(db, story_run.id)
    active_attempt = _active_step(db, parent.id) if parent is not None else None
    # 并发 retry 中第一个请求已经追加了同阶段的新 attempt 时，第二个请求必须复用
    # 它而非把状态变化误报为冲突，更不能再追加 attempt 3。
    if (
        active_attempt is not None
        and active_attempt.step_key == step.step_key
        and active_attempt.attempt == step.attempt + 1
    ):
        return story_run, parent, False
    if active_attempt is not None:
        _error("当前阶段已存在更新的活动 attempt", status.HTTP_409_CONFLICT)
    if story_run.state.status != StoryRunStatus.FAILED:
        _error("当前 StoryRun 并非可重试的失败状态", status.HTTP_409_CONFLICT)
    latest_attempt = int(
        db.scalar(
            select(func.max(CommerceWorkflowStep.attempt)).where(
                CommerceWorkflowStep.story_run_id == story_run.id,
                CommerceWorkflowStep.stage == stage.value,
            )
        )
        or 0
    )
    if step.attempt != latest_attempt:
        _error("只能重试当前阶段最新失败 attempt", status.HTTP_409_CONFLICT)
    story_run, concurrent_parent = _claim_story_run_state(
        db, story_run, expected_status=StoryRunStatus.FAILED, expected_stage=stage
    )
    if concurrent_parent is not None:
        return story_run, concurrent_parent, False
    run, created = _create_stage_step(db, story_run, stage)
    if created:
        run.status = RunStatus.PENDING
        run.finished_at = None
    db.commit()
    return story_run, run, created


@dataclass(frozen=True)
class CommerceNodeContext:
    db: Session
    story_run: StoryRun
    workflow_run: WorkflowRun
    workflow_step: WorkflowStep
    stage: StoryRunStage


@dataclass(frozen=True)
class CommerceNodeResult:
    artifact_references: dict[str, Any]
    structured_output: dict[str, Any]
    usage: dict[str, Any]
    cost: dict[str, Any]
    provider_task: dict[str, Any] | None = None


class CommerceNodeExecutor(Protocol):
    def execute(self, context: CommerceNodeContext) -> CommerceNodeResult: ...


def _is_mock(context: CommerceNodeContext) -> bool:
    # 每个节点在创建 attempt 时冻结自身的模型/Prompt；父 run 仅冻结工作流身份。
    bindings = (context.workflow_step.model_profile_snapshot or {}).get("model_bindings") or {}
    for items in bindings.values():
        if not isinstance(items, list) or not items:
            return False
        if any((item.get("profile_snapshot") or {}).get("adapter_key") != "mock_v1" for item in items):
            return False
    return True


class MockCommerceNodeExecutor:
    """无网络、确定性的 Phase 2 验收执行器；只建立领域候选，绝不替代人工审核。"""

    def execute(self, context: CommerceNodeContext) -> CommerceNodeResult:
        has_mainline_outline = (
            context.stage == StoryRunStage.OUTLINE
            and isinstance((context.workflow_step.input_payload or {}).get("commerce_mainline"), dict)
        )
        if context.stage != StoryRunStage.TOPIC and not _is_mock(context) and not has_mainline_outline:
            raise RuntimeError("冻结的 Commerce 能力尚未配置受支持 Adapter")
        handler = getattr(self, f"_execute_{context.stage.value.lower()}")
        return handler(context)

    @staticmethod
    def _result(refs: dict[str, Any], data: dict[str, Any]) -> CommerceNodeResult:
        return CommerceNodeResult(refs, data, {"input_tokens": 0, "output_tokens": 0}, {"amount": 0, "currency": "CNY"})

    def _execute_topic(self, context: CommerceNodeContext) -> CommerceNodeResult:
        _validate_start_bindings(context.db, context.story_run)
        return self._result({"topic_candidate_id": context.story_run.topic_candidate_id}, {"validated": True})

    def _execute_outline(self, context: CommerceNodeContext) -> CommerceNodeResult:
        mainline = (context.workflow_step.input_payload or {}).get("commerce_mainline")
        if isinstance(mainline, dict):
            return self._execute_mainline_outline(context, mainline)
        outline = create_next_story_outline_version(
            context.db,
            story_run_id=context.story_run.id,
            title="Mock 带货短剧大纲",
            premise="围绕已冻结产品的原创人物冲突与解决方案。",
            story_beats=[{"beat": "hook", "content": "主角遇到痛点"}, {"beat": "resolution", "content": "自然体验产品"}],
            product_placement_strategy={"method": "SOFT_PROP", "strength": "LIGHT"},
        )
        return self._result({"outline_id": outline.id}, {"outline_version": outline.version})

    def _execute_mainline_outline(
        self, context: CommerceNodeContext, frozen_input: dict[str, Any]
    ) -> CommerceNodeResult:
        """以 Slice 1 的冻结脚本、商品、创意生成正式大纲与融入策略。

        Mock 和真实模型仅在 Adapter 调用处区别；两条路径保存相同版本产物和引用，避免
        非 Mock 配置伪造成功。Worker 不读取当前商品、当前 Prompt 或当前创意。
        """

        product = frozen_input.get("product_asset_version")
        script = frozen_input.get("script_analysis")
        idea = frozen_input.get("creative_idea")
        if not all(isinstance(item, dict) for item in (product, script, idea)):
            raise RuntimeError("大纲节点缺少冻结脚本、商品或创意")
        content = idea.get("content") if isinstance(idea.get("content"), dict) else {}
        invocation: ModelInvocation | None = None
        if _is_mock(context):
            outline_payload = {
                "title": str(content.get("title") or "带货短剧大纲"),
                "premise": str(content.get("synopsis") or "围绕真实痛点与产品体验的原创故事。"),
                "story_beats": [
                    {"beat": "hook", "content": str(content.get("opening_hook") or "异常事件推动人物行动")},
                    {"beat": "conflict", "content": "人物的真实痛点升级，不能靠虚构功效解决。"},
                    {"beat": "integration", "content": "根据已确认产品事实安排自然体验。"},
                    {"beat": "resolution", "content": "关系或目标发生可验证变化。"},
                ],
                "product_placement_strategy": deepcopy(content.get("product_integration") or {"method": "SOFT_PROP"}),
            }
        else:
            bindings = (context.workflow_step.model_profile_snapshot or {}).get("model_bindings") or {}
            items = bindings.get("STORY_GENERATE")
            if not isinstance(items, list) or not items:
                raise RuntimeError("大纲节点缺少冻结的故事模型")
            profile = items[0].get("profile_snapshot") or {}
            prompts = (context.workflow_step.input_payload or {}).get("prompt_templates") or {}
            prompt = prompts.get("STORY_GENERATE") or {}
            if not isinstance(profile, dict) or not isinstance(prompt, dict):
                raise RuntimeError("大纲节点冻结模型或 Prompt 无效")
            invocation_key = f"{context.workflow_step.idempotency_key}:model:{items[0].get('model_profile_id')}"
            invocation = context.db.scalar(
                select(ModelInvocation).where(ModelInvocation.idempotency_key == invocation_key)
            )
            if invocation is None:
                invocation = ModelInvocation(
                    project_id=context.story_run.project_id,
                    workflow_run_id=context.workflow_run.id,
                    workflow_step_id=context.workflow_step.id,
                    model_slot_id=items[0].get("slot_id"),
                    model_profile_id=items[0].get("model_profile_id"),
                    prompt_template_id=prompt.get("id"),
                    prompt_template_version_id=prompt.get("prompt_version_id"),
                    task_type="STORY_GENERATE",
                    model_profile_snapshot=deepcopy(profile),
                    prompt_snapshot=deepcopy(prompt),
                    input_snapshot={
                        "execution_mode": "commerce_workflow",
                        "story_run_id": context.story_run.id,
                        "workflow_step_id": context.workflow_step.id,
                        "generation_parameters": deepcopy(profile.get("parameter_resolution") or {}),
                    },
                    idempotency_key=invocation_key,
                    status=RunStatus.RUNNING,
                )
                context.db.add(invocation)
                # 在真正调用前先落库审计。Worker 中断或供应商异常时，调用事实和冻结
                # 参数仍可追溯；这不会重新读取模型中心，也不会产生第二次请求。
                context.db.commit()
            started = perf_counter()
            try:
                assert_supported(profile, "STORY_GENERATE")
                outline_payload = generate_structured_text(
                    profile,
                    task_type="STORY_GENERATE",
                    system_instruction=_frozen_prompt_text(prompt, "rendered_system_template"),
                    user_instruction=_frozen_prompt_text(prompt, "rendered_user_template"),
                    user_payload={"frozen_input": deepcopy(frozen_input)},
                    output_contract=(
                        '{"title":"string","premise":"string","story_beats":[{"beat":"string","content":"string"}],'
                        '"product_placement_strategy":{"method":"string"}}'
                    ),
                )
            except Exception:
                invocation.status = RunStatus.FAILED
                invocation.error_code = "COMMERCE_WORKFLOW_MODEL_FAILED"
                invocation.output_reference = {
                    "failure": {
                        "code": "COMMERCE_WORKFLOW_MODEL_FAILED",
                        "message": "冻结模型调用未成功完成；请检查模型配置后重试",
                    }
                }
                invocation.latency_ms = max(0, int((perf_counter() - started) * 1000))
                invocation.finished_at = utcnow()
                context.db.commit()
                raise
            # 模型调用和后续领域契约校验是两个可区分事实：模型正常返回但结构不合规
            # 时，审计仍应如实标记模型调用成功，由 WorkflowStep 记录契约失败。
            invocation.status = RunStatus.SUCCEEDED
            invocation.output_reference = {"workflow_step_id": context.workflow_step.id, "result": "structured_response_received"}
            invocation.latency_ms = max(0, int((perf_counter() - started) * 1000))
            invocation.finished_at = utcnow()
            context.db.commit()
        title = outline_payload.get("title") if isinstance(outline_payload, dict) else None
        premise = outline_payload.get("premise") if isinstance(outline_payload, dict) else None
        beats = outline_payload.get("story_beats") if isinstance(outline_payload, dict) else None
        strategy = outline_payload.get("product_placement_strategy") if isinstance(outline_payload, dict) else None
        if not isinstance(title, str) or not title.strip() or not isinstance(premise, str) or not premise.strip() or not isinstance(beats, list) or not beats or not isinstance(strategy, dict):
            raise RuntimeError("大纲模型返回不符合结构化契约")
        outline = create_next_story_outline_version(
            context.db,
            story_run_id=context.story_run.id,
            title=title.strip()[:180],
            premise=premise.strip()[:20_000],
            story_beats=deepcopy(beats),
            product_placement_strategy={
                **deepcopy(strategy),
                "frozen_product_asset_version_id": product.get("id"),
                "creative_idea_id": idea.get("id"),
                "script_analysis_version_id": script.get("id"),
            },
        )
        return self._result(
            {
                "outline_id": outline.id,
                "script_analysis_version_id": script.get("id"),
                "product_asset_version_id": product.get("id"),
                "creative_idea_id": idea.get("id"),
            },
            {"outline_version": outline.version, "mainline": True},
        )

    def _execute_chapters(self, context: CommerceNodeContext) -> CommerceNodeResult:
        outline = context.db.scalars(select(StoryOutlineVersion).where(StoryOutlineVersion.story_run_id == context.story_run.id, StoryOutlineVersion.status == OutlineVersionStatus.LOCKED)).first()
        if outline is None:
            raise RuntimeError("章节规划缺少已锁定大纲")
        chapter_number = int(
            context.db.scalar(
                select(func.max(ChapterPlan.chapter_number)).where(ChapterPlan.story_run_id == context.story_run.id)
            )
            or 0
        ) + 1
        chapter = create_chapter_plan(
            context.db, story_run_id=context.story_run.id, outline_version_id=outline.id, chapter_number=chapter_number,
            title="第一章：痛点出现", narrative_purpose="建立人物需求", content_summary="主角遇到真实问题并寻找解决方式", product_plan={"placement": "soft_prop"},
        )
        context.db.add(
            CommerceChapterAttemptChapter(
                workflow_step_id=context.workflow_step.id,
                story_run_id=context.story_run.id,
                outline_version_id=outline.id,
                chapter_plan_id=chapter.id,
                position=1,
            )
        )
        context.db.flush()
        return self._result(
            {
                "chapter_ids": [chapter.id],
                "outline_id": outline.id,
                "chapter_attempt_workflow_step_id": context.workflow_step.id,
            },
            {"chapter_count": 1, "chapter_attempt": context.workflow_step.attempt},
        )

    def _execute_storyboard(self, context: CommerceNodeContext) -> CommerceNodeResult:
        accepted_chapters = (
            _latest_successful_step(context.db, context.story_run.id, StoryRunStage.CHAPTERS)
            if context.story_run.mode == StoryRunMode.AUTO
            else _latest_approved_step(context.db, context.story_run.id, StoryRunStage.CHAPTERS)
        )
        chapters = _chapter_attempt_chapters(
            context.db,
            context.story_run,
            accepted_chapters,
            outline_id=_artifact_id(accepted_chapters, "outline_id"),
        )
        outline_id = chapters[0].outline_version_id
        mapping_version = int(context.db.scalar(select(func.max(SceneMappingVersion.version)).where(SceneMappingVersion.story_run_id == context.story_run.id)) or 0) + 1
        number = int(context.db.scalar(select(func.max(VideoSegmentPlan.segment_number)).where(VideoSegmentPlan.story_run_id == context.story_run.id)) or 0) + 1
        segment = create_video_segment_plan(context.db, story_run_id=context.story_run.id, chapter_id=chapters[0].id, segment_number=number, target_duration_ms=4000, narrative_target="用一个连续片段完成痛点与产品体验")
        sub_shot = add_sub_shot_plan(context.db, segment, shot_number=1, start_ms=0, end_ms=4000, action="主角拿起产品并体验", emotion="释然", shot_scale="中景", camera_move="缓慢推进", lighting="自然柔光", visual_description="产品与人物同框，画面连续")
        dialogue = create_dialogue_line(context.db, video_segment_id=None, sub_shot_id=sub_shot.id, speaker="主角", dialogue="这次终于解决了。", start_ms=300, end_ms=1800)
        placement = create_product_placement_plan(context.db, story_run_id=context.story_run.id, product_asset_version_id=context.story_run.product_asset_version_id, sub_shot_id=sub_shot.id, chapter_id=None, video_segment_id=None, placement_method=ProductPlacementMethod.SOFT_PROP, placement_strength=ProductPlacementStrength.LIGHT, pain_point_trigger="生活不便", product_action="自然拿起并使用", ad_entry_point="冲突解决时", story_recovery_point="体验后回到人物关系", planned_duration_ms=1500)
        mapping = create_scene_mapping_version(context.db, story_run_id=context.story_run.id, outline_version_id=outline_id, version=mapping_version, mapping_snapshot=[{"chapter_id": chapters[0].id, "video_segment_id": segment.id}], status_value="DRAFT")
        return self._result(
            {
                "scene_mapping_id": mapping.id,
                "chapter_ids": [chapter.id for chapter in chapters],
                "video_segment_ids": [segment.id],
                "dialogue_line_ids": [dialogue.id],
                "product_placement_ids": [placement.id],
            },
            {"segment_count": 1, "dialogue_line_count": 1},
        )

    def _execute_visual_assets(self, context: CommerceNodeContext) -> CommerceNodeResult:
        # Phase 2 不生成图片；空引用代表等待资产中心人工选择，若调用方提供引用会在审核时校验。
        return self._result({"project_character_reference_ids": [], "project_scene_reference_ids": []}, {"generation": "not_requested_in_phase_2"})

    def _execute_video_prompts(self, context: CommerceNodeContext) -> CommerceNodeResult:
        created: list[str] = []
        for segment in _accepted_storyboard_segments(context.db, context.story_run):
            version = int(context.db.scalar(select(func.max(VideoPromptVersion.version)).where(VideoPromptVersion.video_segment_id == segment.id)) or 0) + 1
            prompt = VideoPromptVersion(video_segment_id=segment.id, workflow_step_id=context.workflow_step.id, version=version, prompt="参考已锁定角色与场景，保持动作连续。", trace={"source": "commerce_mock", "stage": "VIDEO_PROMPTS"})
            context.db.add(prompt)
            context.db.flush()
            created.append(prompt.id)
        if not created:
            raise RuntimeError("视频提示词缺少已确认分镜片段")
        return self._result({"video_prompt_version_ids": created}, {"prompt_count": len(created)})

    def _execute_segment_render(self, context: CommerceNodeContext) -> CommerceNodeResult:
        prompts = _adopted_video_prompt_step(context.db, context.story_run)
        versions = _validate_video_prompts(context.db, context.story_run, prompts)
        if any(item.status != "LOCKED" or item.locked_at is None for item in versions):
            raise RuntimeError("批量渲染缺少锁定的视频提示词")
        batch_number = int(context.db.scalar(select(func.max(RenderBatch.batch_number)).where(RenderBatch.story_run_id == context.story_run.id)) or 0) + 1
        batch = create_render_batch(context.db, story_run_id=context.story_run.id, workflow_run_id=context.workflow_run.id, batch_number=batch_number, status=RenderBatchStatus.COMPLETED, total_tasks=len(versions), completed_tasks=len(versions), failed_tasks=0, running_tasks=0, model_config_snapshot=deepcopy((context.workflow_step.model_profile_snapshot or {}).get("model_bindings") or {}), generation_parameters_snapshot={"executor": "mock"}, estimated_cost=0, currency="CNY", started_at=utcnow(), finished_at=utcnow())
        for prompt in versions:
            prompt.video_segment.status = SegmentPlanStatus.COMPLETED
        return self._result({"render_batch_id": batch.id}, {"completed_segments": len(versions)})


class CommerceNodeRegistry:
    """七节点显式入口；未来真实 Adapter 只替换这里注册的实现。"""

    _executor: CommerceNodeExecutor = MockCommerceNodeExecutor()

    @classmethod
    def resolve(cls, stage: StoryRunStage) -> CommerceNodeExecutor:
        if stage not in STAGES:
            raise RuntimeError("未知 Commerce 节点")
        return cls._executor


def _latest_approved_step(db: Session, story_run_id: str, stage: StoryRunStage) -> WorkflowStep:
    step = db.scalars(
        select(WorkflowStep)
        .join(WorkflowRun, WorkflowStep.workflow_run_id == WorkflowRun.id)
        .join(CommerceWorkflowLink, CommerceWorkflowLink.workflow_run_id == WorkflowRun.id)
        .join(ReviewDecision, ReviewDecision.target_id == WorkflowStep.id)
        .where(
            CommerceWorkflowLink.story_run_id == story_run_id,
            WorkflowStep.step_key == stage.value,
            ReviewDecision.target_type == f"COMMERCE_STAGE_{stage.value}",
            ReviewDecision.decision == "APPROVED",
        )
        .order_by(ReviewDecision.created_at.desc())
    ).first()
    if step is None:
        raise RuntimeError(f"缺少已确认的 {stage.value} 结果")
    return step


def _finish_success(context: CommerceNodeContext, result: CommerceNodeResult) -> tuple[bool, str | None]:
    now = utcnow()
    step = context.workflow_step
    run = context.workflow_run
    step.output_payload = {
        "artifact_references": result.artifact_references,
        "structured_output": result.structured_output,
        "usage": result.usage,
        "cost": result.cost,
        "provider_task": result.provider_task,
    }
    if result.provider_task and result.provider_task.get("provider_task_id"):
        step.provider_task_id = str(result.provider_task["provider_task_id"])
    step.status = RunStatus.SUCCEEDED
    _set_commerce_step_status(context.db, step.id, RunStatus.SUCCEEDED)
    step.progress = 100
    step.finished_at = now
    # 父 WorkflowRun 是整条 StoryRun 的容器；单个 attempt 成功后仍会等待
    # 人工审核或下一个阶段，不能提前标记为 SUCCEEDED。
    run.status = RunStatus.PENDING
    run.finished_at = None
    state = context.story_run.state
    # STEPWISE 的视频提示词成功后只能停留在 DRAFT，必须由人工 confirm 才会
    # LOCKED。AUTO 则在相同完整性校验通过后自动锁定，避免两条路径采用不同产物。
    if context.stage == StoryRunStage.VIDEO_PROMPTS and context.story_run.mode == StoryRunMode.AUTO:
        _lock_video_prompts(context.db, context.story_run, step)
    # AUTO 不经过人工 CHAPTERS 闸门，因此在自动推进前同样校验当前 attempt 的完整
    # 章节组；不能回读被驳回的旧章节，也不能把不完整结果推进到 STORYBOARD。
    if context.stage == StoryRunStage.CHAPTERS and context.story_run.mode == StoryRunMode.AUTO:
        outline_id = _artifact_id(step, "outline_id")
        if outline_id is None:
            _error("章节结果缺少大纲引用", status.HTTP_422_UNPROCESSABLE_CONTENT)
        _validate_chapters(context.db, context.story_run, step, outline_id)
    if context.stage in REVIEW_GATES or context.story_run.mode == StoryRunMode.STEPWISE:
        state.status = StoryRunStatus.PAUSED
        state.stage_data = {"blocked_reason": "awaiting_review" if context.stage in REVIEW_GATES else "awaiting_continue"}
        context.db.commit()
        return False, None
    next_stage = _next_stage(context.stage)
    state.current_stage = next_stage
    state.status = StoryRunStatus.PENDING
    state.stage_data = {"blocked_reason": None}
    # 先把已完成 attempt 落到当前事务查询视图，再创建下一个 attempt。
    context.db.flush()
    next_run, created = _create_stage_step(context.db, context.story_run, next_stage)
    state.status = StoryRunStatus.RUNNING
    context.db.commit()
    return created, next_run.id


def _claim_attempt_finalization(db: Session, step_id: str) -> bool:
    """确认 Worker 仍拥有 ``RUNNING`` attempt 的终态写入权。

    模型调用可能耗时很久。若用户在调用期间取消了 StoryRun，``cancel`` 会先把
    该 Step 标为 ``CANCELLED``；迟到的 Worker 绝不能再把它改写为成功或失败。
    条件 UPDATE 同时是 PostgreSQL 行锁和 SQLite 的跨进程原子边界：一旦本函数
    成功，后续取消会在本事务提交后观察到非活动终态，而不是产生覆盖写入。
    """

    claimed = db.execute(
        update(WorkflowStep)
        .where(WorkflowStep.id == step_id, WorkflowStep.status == RunStatus.RUNNING)
        # SQLite 没有独立的行锁 API；无语义变化的自赋值会实际取得写入权。
        .values(progress=WorkflowStep.progress)
    ).rowcount
    return claimed == 1


def execute_commerce_workflow(run_id: str) -> None:
    """执行单一 Commerce 父运行中当前的 pending attempt。

    ``UPDATE ... WHERE status=PENDING`` 是跨 Worker 的执行领取点。第二个并发 Job
    读取到同一 attempt 时无法把它再次改为 RUNNING，因此不会重复调用供应商；已保存
    ``provider_task_id`` 的 attempt 也只允许未来 Adapter 走恢复查询，不会重新提交。
    """

    db: Session = SessionLocal()
    try:
        pending_id: str | None = run_id
        while pending_id:
            run = db.get(WorkflowRun, pending_id)
            link = db.get(CommerceWorkflowLink, run.id) if run is not None else None
            if run is None or link is None or run.workflow_key != COMMERCE_WORKFLOW_KEY:
                return
            step = db.scalars(
                select(WorkflowStep)
                .where(WorkflowStep.workflow_run_id == run.id, WorkflowStep.status == RunStatus.PENDING)
                .order_by(WorkflowStep.position, WorkflowStep.attempt.desc(), WorkflowStep.created_at.desc())
            ).first()
            story_run = _locked_story_run(db, link.story_run_id)
            if step is None or run.status in {RunStatus.SUCCEEDED, RunStatus.CANCELLED}:
                return
            if story_run.state.status == StoryRunStatus.CANCELLED:
                return
            # 只有一个 Worker 能成功领取 PENDING attempt；这是重复队列消息、页面
            # 重试与并发 Worker 共同依赖的幂等边界。
            now = utcnow()
            claimed = db.execute(
                update(WorkflowStep)
                .where(WorkflowStep.id == step.id, WorkflowStep.status == RunStatus.PENDING)
                .values(status=RunStatus.RUNNING, started_at=step.started_at or now)
            ).rowcount
            if claimed != 1:
                db.rollback()
                return
            _set_commerce_step_status(db, step.id, RunStatus.RUNNING)
            stage = StoryRunStage(step.step_key)
            run.status = RunStatus.RUNNING
            run.started_at = run.started_at or utcnow()
            story_run.state.status = StoryRunStatus.RUNNING
            db.commit()
            db.refresh(run)
            db.refresh(step)
            db.refresh(story_run)
            context = CommerceNodeContext(db=db, story_run=story_run, workflow_run=run, workflow_step=step, stage=stage)
            try:
                # 已提交供应商任务的未来真实 Adapter 只能恢复查询，不能因为 Worker
                # 重启再次执行提交动作。Phase 2 Mock 没有 provider_task_id，因此仍可
                # 安全恢复同步节点。
                if step.status == RunStatus.RUNNING and step.provider_task_id:
                    return
                result = CommerceNodeRegistry.resolve(stage).execute(context)
                # 真实模型返回前可能已经有取消请求提交。只有仍持有这个 RUNNING
                # attempt 的 Worker 才能写入输出和推进状态机。
                if not _claim_attempt_finalization(db, step.id):
                    db.rollback()
                    return
                db.refresh(run)
                if run.status == RunStatus.CANCELLED:
                    db.rollback()
                    return
                should_chain, pending_id = _finish_success(context, result)
                if not should_chain:
                    return
            except Exception as exc:
                db.rollback()
                run = db.get(WorkflowRun, run.id)
                if run is not None:
                    step = db.get(WorkflowStep, step.id)
                    # 和成功分支一样，不能让迟到失败覆盖已取消的 attempt。
                    if step is None or not _claim_attempt_finalization(db, step.id):
                        db.rollback()
                        return
                    db.refresh(run)
                    if run.status == RunStatus.CANCELLED:
                        db.rollback()
                        return
                    link = db.get(CommerceWorkflowLink, run.id)
                    story_run = _locked_story_run(db, link.story_run_id) if link is not None else None
                    now = utcnow()
                    run.status = RunStatus.FAILED
                    run.finished_at = now
                    if step is not None:
                        step.status = RunStatus.FAILED
                        _set_commerce_step_status(db, step.id, RunStatus.FAILED)
                        step.error_message = _safe_error(exc)
                        step.finished_at = now
                    if story_run is not None:
                        story_run.state.status = StoryRunStatus.FAILED
                        story_run.state.stage_data = {"blocked_reason": "execution_failed", "failed_step_id": step.id if step else None}
                    db.commit()
                return
    finally:
        db.close()


def workflow_for_story_run(db: Session, story_run_id: str) -> list[WorkflowRun]:
    _locked_story_run(db, story_run_id)
    return list(
        db.scalars(
        select(WorkflowRun)
        .join(CommerceWorkflowLink, CommerceWorkflowLink.workflow_run_id == WorkflowRun.id)
        .where(CommerceWorkflowLink.story_run_id == story_run_id, WorkflowRun.workflow_key == COMMERCE_WORKFLOW_KEY)
            .order_by(WorkflowRun.created_at, WorkflowRun.id)
        ).all()
    )


def reviews_for_story_run(db: Session, story_run_id: str) -> list[ReviewDecision]:
    story_run = _locked_story_run(db, story_run_id)
    step_ids = select(WorkflowStep.id).join(WorkflowRun, WorkflowStep.workflow_run_id == WorkflowRun.id).where(
        WorkflowRun.workflow_key == COMMERCE_WORKFLOW_KEY,
        WorkflowRun.id == CommerceWorkflowLink.workflow_run_id,
        CommerceWorkflowLink.story_run_id == story_run.id,
    )
    return list(
        db.scalars(
            select(ReviewDecision)
            .where(
                ReviewDecision.project_id == story_run.project_id,
                (
                    (ReviewDecision.target_type == "COMMERCE_TOPIC_INPUT") & (ReviewDecision.target_id == story_run.id)
                    | (ReviewDecision.target_type.like("COMMERCE_STAGE_%") & ReviewDecision.target_id.in_(step_ids))
                ),
            )
            .order_by(ReviewDecision.created_at, ReviewDecision.id)
        ).all()
    )


def get_story_run(db: Session, story_run_id: str) -> StoryRun:
    """返回带状态的 StoryRun；读取不修改 V1 或 Commerce 事实。"""

    return _locked_story_run(db, story_run_id)


def list_project_story_runs(db: Session, project_id: str) -> list[StoryRun]:
    if db.get(Project, project_id) is None:
        _error("项目不存在", status.HTTP_404_NOT_FOUND)
    return list(
        db.scalars(
            select(StoryRun).where(StoryRun.project_id == project_id).order_by(StoryRun.created_at, StoryRun.run_number, StoryRun.id)
        ).all()
    )


def outlines_for_story_run(db: Session, story_run_id: str) -> list[StoryOutlineVersion]:
    _locked_story_run(db, story_run_id)
    return list(db.scalars(select(StoryOutlineVersion).where(StoryOutlineVersion.story_run_id == story_run_id).order_by(StoryOutlineVersion.version)).all())


def create_manual_outline(db: Session, story_run_id: str, contents: dict[str, Any]) -> StoryOutlineVersion:
    story_run = _locked_story_run(db, story_run_id)
    _require_not_terminal(story_run)
    if story_run.state.current_stage != StoryRunStage.OUTLINE:
        _error("只有 OUTLINE 阶段可以新增大纲")
    outline = create_next_story_outline_version(db, story_run_id=story_run.id, **contents)
    db.commit()
    return outline


def patch_manual_outline(db: Session, story_run_id: str, outline_id: str, contents: dict[str, Any]) -> StoryOutlineVersion:
    story_run = _locked_story_run(db, story_run_id)
    outline = db.get(StoryOutlineVersion, outline_id)
    if outline is None or outline.story_run_id != story_run.id:
        _error("大纲不存在或不属于当前 StoryRun", status.HTTP_404_NOT_FOUND)
    updated = update_story_outline_version(db, story_outline_version_id=outline.id, **contents)
    db.commit()
    return updated
