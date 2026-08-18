"""Model Lab 的内部比较接口。

路由仅做契约解析、服务委托和已有队列投递；状态迁移、冻结、评分与提升生产版本
全部由 ``model_lab_service`` 统一处理，避免页面绕过业务边界直接改写数据库。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import (
    ModelProfile,
    ModelSlot,
    ModelSlotProfileBinding,
    PromptTemplateDefinition,
    PromptTemplateVersion,
    PromptTemplateVersionStatus,
)
from app.schemas import (
    ModelLabEvaluationRequest,
    ModelLabExperimentCreateRequest,
    ModelLabExperimentResponse,
    ModelLabPreflightResponse,
    ModelLabPromotionRequest,
    ModelLabStartRequest,
)
from app.services.model_lab_service import (
    MODEL_LAB_WORKFLOW_KEY,
    create_experiment,
    get_experiment,
    list_experiments,
    pause_experiment,
    preflight_experiment,
    preflight_existing_experiment,
    promote_winner_to_production,
    response_payload,
    resume_experiment,
    resume_provider_task_variant,
    start_experiment,
    upsert_evaluation,
)
from app.services.model_parameter_service import profile_parameter_config
from app.services.worker_runtime import dispatch_workflow


router = APIRouter(prefix="/api/v1/model-lab", tags=["模型测试台"])


def _response(db: Session, item) -> ModelLabExperimentResponse:
    return ModelLabExperimentResponse(**response_payload(db, item))


@router.get("/catalog")
def catalog(
    operation_key: str = Query(min_length=1, max_length=120),
    model_slot_key: str = Query(min_length=1, max_length=80),
    capability: str = Query(pattern="^(text|image|video)$"),
    db: Session = Depends(get_db),
) -> dict:
    """返回可选的 Profile 和 Published Prompt，不返回连接或密钥配置。"""

    prompts = list(
        db.scalars(
            select(PromptTemplateVersion)
            .join(PromptTemplateDefinition, PromptTemplateVersion.prompt_template_id == PromptTemplateDefinition.id)
            .where(
                PromptTemplateDefinition.operation_key == operation_key,
                PromptTemplateDefinition.model_slot_key == model_slot_key,
                PromptTemplateDefinition.capability == capability,
                PromptTemplateVersion.status == PromptTemplateVersionStatus.PUBLISHED,
            )
            .order_by(PromptTemplateVersion.version.desc())
        ).all()
    )
    profiles = list(db.scalars(select(ModelProfile).where(ModelProfile.step_key == model_slot_key).order_by(ModelProfile.version.desc())).all())
    slot = db.scalar(select(ModelSlot).where(ModelSlot.slot_key == model_slot_key))
    active_profiles: list[dict] = []
    if slot is not None:
        active_profiles = [
            {
                "id": profile.id,
                "name": profile.display_name or profile.model_key,
                "version": profile.version,
            }
            for profile in db.scalars(
                select(ModelProfile)
                .join(ModelSlotProfileBinding, ModelSlotProfileBinding.model_profile_id == ModelProfile.id)
                .where(
                    ModelSlotProfileBinding.slot_id == slot.id,
                    ModelSlotProfileBinding.is_enabled.is_(True),
                )
                .order_by(ModelSlotProfileBinding.priority, ModelSlotProfileBinding.created_at)
            ).all()
        ]
    safe_profiles = []
    for profile in profiles:
        adapter = profile.adapter_key or profile.provider_key
        try:
            config, _ = profile_parameter_config(adapter, profile.provider_config, profile.parameter_config)
        except Exception:
            continue
        if adapter == "mock_v1":
            # 现有 V1 Mock 在三个能力槽位都已有模拟执行分支；仅在测试台明确标识，
            # 不把它伪装为真实模型，也不影响真实 Profile 的能力校验。
            config = {**config, "capability": capability}
        if config.get("capability") != capability:
            continue
        safe_profiles.append({
            "id": profile.id,
            "name": profile.display_name or profile.model_key,
            "version": profile.version,
            "model_key": profile.model_key,
            "profile_status": profile.profile_status,
            "is_mock": adapter == "mock_v1",
            "supported_presets": sorted(config.get("presets", {}).keys()),
            "supported_parameters": config.get("supported_parameters", {}),
        })
    return {
        "profiles": safe_profiles,
        "slot_selection_mode": slot.selection_mode.value if slot is not None else None,
        "active_profiles": active_profiles,
        "prompt_versions": [
            {"id": version.id, "prompt_key": definition.prompt_key, "display_name": definition.display_name,
             "version": version.version, "content_hash": version.content_hash[:12], "output_contract_key": version.output_contract_key}
            for version in prompts
            if (definition := db.get(PromptTemplateDefinition, version.prompt_template_id)) is not None
        ],
    }


@router.post("/preflight", response_model=ModelLabPreflightResponse)
def preflight(payload: ModelLabExperimentCreateRequest, db: Session = Depends(get_db)) -> ModelLabPreflightResponse:
    """只解析冻结输入/参数/Prompt，不创建审计记录且不访问模型。"""

    return ModelLabPreflightResponse(**preflight_experiment(db, payload.model_dump()))


@router.post("/experiments/{experiment_id}/preflight", response_model=ModelLabPreflightResponse)
def preflight_existing(experiment_id: str, db: Session = Depends(get_db)) -> ModelLabPreflightResponse:
    """Refresh an already frozen experiment's server-side start authorization."""

    return ModelLabPreflightResponse(**preflight_existing_experiment(db, experiment_id))


@router.post("/experiments", response_model=ModelLabExperimentResponse, status_code=status.HTTP_201_CREATED)
def create(payload: ModelLabExperimentCreateRequest, db: Session = Depends(get_db)) -> ModelLabExperimentResponse:
    return _response(db, create_experiment(db, payload.model_dump()))


@router.get("/experiments", response_model=list[ModelLabExperimentResponse])
def list_items(project_id: Optional[str] = Query(default=None, max_length=64), db: Session = Depends(get_db)) -> list[ModelLabExperimentResponse]:
    return [_response(db, item) for item in list_experiments(db, project_id)]


@router.get("/experiments/{experiment_id}", response_model=ModelLabExperimentResponse)
def get_item(experiment_id: str, db: Session = Depends(get_db)) -> ModelLabExperimentResponse:
    return _response(db, get_experiment(db, experiment_id))


@router.post("/experiments/{experiment_id}/start", response_model=ModelLabExperimentResponse)
def start(
    experiment_id: str,
    payload: ModelLabStartRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> ModelLabExperimentResponse:
    item = start_experiment(
        db,
        experiment_id=experiment_id,
        confirmed_create_calls=payload.confirmed_create_calls,
        preflight_hash=payload.preflight_hash,
    )
    if item.workflow_run_id:
        dispatch_workflow(background_tasks, MODEL_LAB_WORKFLOW_KEY, item.workflow_run_id)
    return _response(db, item)


@router.post("/experiments/{experiment_id}/pause", response_model=ModelLabExperimentResponse)
def pause(experiment_id: str, db: Session = Depends(get_db)) -> ModelLabExperimentResponse:
    return _response(db, pause_experiment(db, experiment_id))


@router.post("/experiments/{experiment_id}/resume", response_model=ModelLabExperimentResponse)
def resume(experiment_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> ModelLabExperimentResponse:
    item = resume_experiment(db, experiment_id)
    if item.workflow_run_id:
        dispatch_workflow(background_tasks, MODEL_LAB_WORKFLOW_KEY, item.workflow_run_id)
    return _response(db, item)


@router.post("/experiments/{experiment_id}/variants/{variant_id}/resume-provider-task", response_model=ModelLabExperimentResponse)
def resume_provider_task(
    experiment_id: str, variant_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
) -> ModelLabExperimentResponse:
    """恢复已持久化供应商任务；客户端不能传入或改写任务号。"""

    resume_provider_task_variant(db, experiment_id=experiment_id, variant_id=variant_id)
    item = get_experiment(db, experiment_id)
    if item.workflow_run_id:
        dispatch_workflow(background_tasks, MODEL_LAB_WORKFLOW_KEY, item.workflow_run_id)
    return _response(db, item)


@router.put("/experiments/{experiment_id}/variants/{variant_id}/evaluation", response_model=ModelLabExperimentResponse)
def evaluate(
    experiment_id: str, variant_id: str, payload: ModelLabEvaluationRequest, db: Session = Depends(get_db)
) -> ModelLabExperimentResponse:
    upsert_evaluation(
        db, experiment_id=experiment_id, variant_id=variant_id, scores=payload.scores, notes=payload.notes,
        is_winner=payload.is_winner,
    )
    return _response(db, get_experiment(db, experiment_id))


@router.post("/experiments/{experiment_id}/variants/{variant_id}/promote", response_model=ModelLabExperimentResponse)
def promote(
    experiment_id: str, variant_id: str, payload: ModelLabPromotionRequest, db: Session = Depends(get_db)
) -> ModelLabExperimentResponse:
    return _response(
        db,
        promote_winner_to_production(
            db, experiment_id=experiment_id, variant_id=variant_id, confirmed=payload.confirmed,
            replace_profile_id=payload.replace_profile_id,
        ),
    )
