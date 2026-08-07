"""LemonFlow V1 模型槽位与 Prompt 模板版本配置接口。"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import ModelSelectionMode
from app.schemas import (
    ModelSlotBindingRequest,
    ModelSlotBindingResponse,
    ModelSlotResponse,
    ModelSlotStrategyRequest,
    ModelQualityEvaluationResponse,
    ModelQualityRefreshRequest,
    PromptTemplateCreateRequest,
    PromptTemplateResponse,
    V1ModelProfileCreateRequest,
    V1ModelProfileResponse,
    V1ModelProfileUpdateRequest,
)
from app.services.v1_configuration_service import (
    activate_prompt_template,
    archive_prompt_template,
    bind_profile_to_slot,
    create_prompt_template,
    create_v1_model_profile,
    copy_v1_model_profile,
    delete_v1_model_profile,
    get_v1_definition,
    list_model_slots,
    list_v1_model_profiles,
    list_prompt_templates,
    set_slot_strategy,
    update_v1_model_profile,
    model_profile_deletion_status,
    profile_has_model_invocations,
)
from app.services.v1_quality_service import list_latest_quality_evaluations, refresh_quality_evaluations


router = APIRouter(prefix="/api/v1/production", tags=["V1 模型与 Prompt 配置"])


def _slot_response(item) -> ModelSlotResponse:
    return ModelSlotResponse(
        id=item.id,
        slot_key=item.slot_key,
        capability=item.capability,
        selection_mode=item.selection_mode.value,
        description=item.description,
        is_enabled=item.is_enabled,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _binding_response(item) -> ModelSlotBindingResponse:
    return ModelSlotBindingResponse(
        id=item.id,
        slot_id=item.slot_id,
        model_profile_id=item.model_profile_id,
        is_enabled=item.is_enabled,
        priority=item.priority,
        weight=float(item.weight) if item.weight is not None else None,
        created_at=item.created_at,
    )


def _v1_profile_response(db: Session, profile, binding) -> V1ModelProfileResponse:
    """模型中心只显示 V1 相关版本与其在槽位中的实际启用状态。"""

    has_model_invocations = profile_has_model_invocations(db, profile.id)
    active_run_count, delete_block_reason = model_profile_deletion_status(db, profile)
    return V1ModelProfileResponse(
        id=profile.id,
        slot_key=profile.step_key,
        adapter_key=profile.adapter_key or profile.provider_key,
        model_key=profile.model_key,
        display_name=profile.display_name or profile.model_key,
        model_version=profile.model_version,
        version=profile.version,
        provider_config=profile.provider_config,
        is_bound=binding is not None,
        is_enabled_in_slot=bool(binding and binding.is_enabled),
        priority=binding.priority if binding is not None else None,
        profile_status=profile.profile_status,
        has_model_invocations=has_model_invocations,
        can_edit=not has_model_invocations,
        active_run_count=active_run_count,
        can_delete=delete_block_reason is None,
        delete_block_reason=delete_block_reason,
        created_at=profile.created_at,
    )


def _prompt_response(item) -> PromptTemplateResponse:
    return PromptTemplateResponse(
        id=item.id,
        task_type=item.task_type,
        name=item.name,
        version=item.version,
        content=item.content,
        variables_schema=item.variables_schema,
        status=item.status.value,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _quality_response(item, profile, prompt) -> ModelQualityEvaluationResponse:
    """将不可变质量快照转换为模型中心可读的比较行，不附带任何自动切换动作。"""

    return ModelQualityEvaluationResponse(
        id=item.id,
        model_profile_id=item.model_profile_id,
        display_name=profile.display_name or profile.model_key,
        model_key=profile.model_key,
        model_version=profile.model_version or profile.model_key,
        prompt_template_id=item.prompt_template_id,
        prompt_name=prompt.name if prompt else None,
        prompt_version=prompt.version if prompt else None,
        task_type=item.task_type,
        scenario=item.scenario,
        sample_count=item.sample_count,
        success_count=item.success_count,
        success_rate=float(item.success_rate),
        average_cost_amount=float(item.average_cost_amount) if item.average_cost_amount is not None else None,
        currency=item.currency,
        average_latency_ms=item.average_latency_ms,
        average_human_score=float(item.average_human_score) if item.average_human_score is not None else None,
        adoption_rate=float(item.adoption_rate) if item.adoption_rate is not None else None,
        created_at=item.created_at,
    )


@router.get("/workflow-definition")
def get_v1_workflow_definition_endpoint(db: Session = Depends(get_db)) -> dict:
    """返回已发布 Workflow 定义快照，前端据此展示唯一 V1 阶段顺序。"""

    definition = get_v1_definition(db)
    return {
        "id": definition.id,
        "workflow_code": definition.workflow_code,
        "version": definition.version,
        "definition_json": definition.definition_json,
        "status": definition.status.value,
        "published_at": definition.published_at,
    }


@router.get("/model-slots", response_model=list[ModelSlotResponse])
def list_model_slots_endpoint(db: Session = Depends(get_db)) -> list[ModelSlotResponse]:
    """列出模型能力槽位；这里展示任务能力而不是将模型名写进业务流程。"""

    get_v1_definition(db)
    return [_slot_response(item) for item in list_model_slots(db)]


@router.post("/model-slots/{slot_key}/strategy", response_model=ModelSlotResponse)
def set_model_slot_strategy_endpoint(
    slot_key: str,
    payload: ModelSlotStrategyRequest,
    db: Session = Depends(get_db),
) -> ModelSlotResponse:
    """人工调整槽位策略；V1 将拒绝任何自动 A/B 分流策略。"""

    try:
        mode = ModelSelectionMode(payload.selection_mode.strip().upper())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="不支持的模型选择策略") from exc
    return _slot_response(set_slot_strategy(db, slot_key, mode))


@router.post(
    "/model-slots/{slot_key}/bindings",
    response_model=ModelSlotBindingResponse,
    status_code=status.HTTP_201_CREATED,
)
def bind_model_profile_endpoint(
    slot_key: str,
    payload: ModelSlotBindingRequest,
    db: Session = Depends(get_db),
) -> ModelSlotBindingResponse:
    """绑定已有的无密钥模型配置；真实 Key 始终只在部署环境中存在。"""

    get_v1_definition(db)
    return _binding_response(
        bind_profile_to_slot(
            db,
            slot_key=slot_key,
            model_profile_id=payload.model_profile_id,
            enabled=payload.enabled,
            priority=payload.priority,
            weight=payload.weight,
            replace_existing=payload.replace_existing,
        )
    )


@router.get("/v1-model-profiles", response_model=list[V1ModelProfileResponse])
def list_v1_model_profiles_endpoint(db: Session = Depends(get_db)) -> list[V1ModelProfileResponse]:
    """列出 V1 生产模型候选、当前启用状态和版本，不展示旧流程配置。"""

    return [_v1_profile_response(db, profile, binding) for profile, binding in list_v1_model_profiles(db)]


@router.get("/model-quality-evaluations", response_model=list[ModelQualityEvaluationResponse])
def list_model_quality_evaluations_endpoint(
    task_type: Optional[str] = None,
    db: Session = Depends(get_db),
) -> list[ModelQualityEvaluationResponse]:
    """读取最新质量报表快照；只帮助人比较，不会更改任何模型槽位。"""

    return [_quality_response(item, profile, prompt) for item, profile, prompt in list_latest_quality_evaluations(db, task_type=task_type)]


@router.post("/model-quality-evaluations/refresh", response_model=list[ModelQualityEvaluationResponse])
def refresh_model_quality_evaluations_endpoint(
    payload: ModelQualityRefreshRequest,
    db: Session = Depends(get_db),
) -> list[ModelQualityEvaluationResponse]:
    """根据已有调用和人工审核生成新快照；不重跑任务、更不自动替换模型。"""

    refresh_quality_evaluations(db, task_type=payload.task_type)
    return [_quality_response(item, profile, prompt) for item, profile, prompt in list_latest_quality_evaluations(db, task_type=payload.task_type)]


@router.post(
    "/v1-model-profiles",
    response_model=V1ModelProfileResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_v1_model_profile_endpoint(
    payload: V1ModelProfileCreateRequest,
    db: Session = Depends(get_db),
) -> V1ModelProfileResponse:
    """创建 V1 候选模型；仅在人工明确勾选时启用或替换当前槽位配置。"""

    profile = create_v1_model_profile(
        db,
        slot_key=payload.slot_key,
        adapter=payload.adapter_key,
        model_key=payload.model_key,
        display_name=payload.display_name,
        model_version=payload.model_version,
        provider_config=payload.provider_config,
        enable_in_slot=payload.enable_in_slot,
        replace_existing=payload.replace_existing,
        priority=payload.priority,
    )
    binding = next(
        (item for item_profile, item in list_v1_model_profiles(db) if item_profile.id == profile.id),
        None,
    )
    return _v1_profile_response(db, profile, binding)


@router.patch("/v1-model-profiles/{profile_id}", response_model=V1ModelProfileResponse)
def update_v1_model_profile_endpoint(
    profile_id: str,
    payload: V1ModelProfileUpdateRequest,
    db: Session = Depends(get_db),
) -> V1ModelProfileResponse:
    """编辑尚未产生调用记录的配置；已使用版本必须先复制。"""

    profile = update_v1_model_profile(
        db,
        profile_id=profile_id,
        adapter=payload.adapter_key,
        model_key=payload.model_key,
        display_name=payload.display_name,
        model_version=payload.model_version,
        provider_config=payload.provider_config,
    )
    binding = next(
        (item for item_profile, item in list_v1_model_profiles(db) if item_profile.id == profile.id),
        None,
    )
    return _v1_profile_response(db, profile, binding)


@router.post(
    "/v1-model-profiles/{profile_id}/copy",
    response_model=V1ModelProfileResponse,
    status_code=status.HTTP_201_CREATED,
)
def copy_v1_model_profile_endpoint(profile_id: str, db: Session = Depends(get_db)) -> V1ModelProfileResponse:
    """复制历史或已使用模型为可编辑的下一版 Draft。"""

    profile = copy_v1_model_profile(db, profile_id)
    binding = next(
        (item for item_profile, item in list_v1_model_profiles(db) if item_profile.id == profile.id),
        None,
    )
    return _v1_profile_response(db, profile, binding)


@router.delete("/v1-model-profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_v1_model_profile_endpoint(profile_id: str, db: Session = Depends(get_db)) -> None:
    """删除未使用模型候选；正在运行或已有历史调用的版本由服务端正式拒绝。"""

    delete_v1_model_profile(db, profile_id)


@router.get("/prompt-templates", response_model=list[PromptTemplateResponse])
def list_prompt_templates_endpoint(
    task_type: Optional[str] = None,
    db: Session = Depends(get_db),
) -> list[PromptTemplateResponse]:
    """列出 Prompt 历史版本，便于比较质量、成本和最终采用率。"""

    return [_prompt_response(item) for item in list_prompt_templates(db, task_type)]


@router.post(
    "/prompt-templates",
    response_model=PromptTemplateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_prompt_template_endpoint(
    payload: PromptTemplateCreateRequest,
    db: Session = Depends(get_db),
) -> PromptTemplateResponse:
    """创建 Prompt 新草稿版本，避免直接覆盖历史生产模板。"""

    return _prompt_response(
        create_prompt_template(
            db,
            task_type=payload.task_type,
            name=payload.name,
            content=payload.content,
            variables_schema=payload.variables_schema,
        )
    )


@router.post("/prompt-templates/{template_id}/activate", response_model=PromptTemplateResponse)
def activate_prompt_template_endpoint(
    template_id: str, db: Session = Depends(get_db)
) -> PromptTemplateResponse:
    """激活某个模板版本，并自动归档同任务的旧活动版本。"""

    return _prompt_response(activate_prompt_template(db, template_id))


@router.post("/prompt-templates/{template_id}/archive", response_model=PromptTemplateResponse)
def archive_prompt_template_endpoint(
    template_id: str, db: Session = Depends(get_db)
) -> PromptTemplateResponse:
    """归档 Prompt 版本；历史模型调用仍能通过快照完整复现。"""

    return _prompt_response(archive_prompt_template(db, template_id))
