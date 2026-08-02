"""工作流步骤模型配置的版本化管理接口。"""

from typing import Optional

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas import (
    ModelEvaluationCreateRequest,
    ModelEvaluationComparisonResponse,
    ModelEvaluationResponse,
    ModelProfileCreateRequest,
    ModelProfilePreflightResponse,
    ModelProfileResponse,
)
from app.services.model_profile_service import (
    activate_model_profile,
    create_model_evaluation,
    create_model_profile,
    is_adapter_available,
    list_model_evaluations,
    list_model_evaluation_comparisons,
    list_model_profiles,
    preflight_model_profile,
)


router = APIRouter(prefix="/api/v1/model-profiles", tags=["模型配置"])


def _response(profile) -> ModelProfileResponse:
    """编码安全的配置记录，并标注本代码包是否已有对应适配器。"""

    return ModelProfileResponse(
        id=profile.id,
        step_key=profile.step_key,
        provider_key=profile.provider_key,
        model_key=profile.model_key,
        version=profile.version,
        provider_config=profile.provider_config,
        is_active=profile.is_active,
        adapter_available=is_adapter_available(
            profile.step_key,
            profile.provider_key,
            profile.model_key,
        ),
        created_at=profile.created_at,
    )


def _evaluation_response(evaluation) -> ModelEvaluationResponse:
    """将数据库小数与统计字段规范化为前端可直接比较的数值。"""

    total_cost = float(evaluation.total_cost_yuan)
    success_rate = round(evaluation.success_count / evaluation.sample_count * 100, 2)
    average_cost = round(total_cost / evaluation.sample_count, 4)
    cost_per_success = (
        round(total_cost / evaluation.success_count, 4) if evaluation.success_count else None
    )
    return ModelEvaluationResponse(
        id=evaluation.id,
        model_profile_id=evaluation.model_profile_id,
        scenario=evaluation.scenario,
        sample_count=evaluation.sample_count,
        success_count=evaluation.success_count,
        total_cost_yuan=total_cost,
        average_latency_seconds=evaluation.average_latency_seconds,
        quality_score=evaluation.quality_score,
        notes=evaluation.notes,
        success_rate=success_rate,
        average_cost_yuan=average_cost,
        cost_per_success_yuan=cost_per_success,
        created_at=evaluation.created_at,
    )


def _comparison_response(evaluation, profile) -> ModelEvaluationComparisonResponse:
    """复用评测统计编码，再补充模型版本信息以支持横向比较。"""

    base = _evaluation_response(evaluation)
    return ModelEvaluationComparisonResponse(
        **base.model_dump(),
        step_key=profile.step_key,
        provider_key=profile.provider_key,
        model_key=profile.model_key,
        profile_version=profile.version,
        display_name=(profile.provider_config or {}).get("display_name"),
    )


@router.get("", response_model=list[ModelProfileResponse])
def list_model_profiles_endpoint(
    step_key: Optional[str] = None,
    db: Session = Depends(get_db),
) -> list[ModelProfileResponse]:
    """读取所有步骤或某一个步骤的配置版本历史。"""

    return [_response(profile) for profile in list_model_profiles(db, step_key)]


@router.post("", response_model=ModelProfileResponse, status_code=status.HTTP_201_CREATED)
def create_model_profile_endpoint(
    payload: ModelProfileCreateRequest,
    db: Session = Depends(get_db),
) -> ModelProfileResponse:
    """创建模型配置版本；未安装的供应商只能作为未启用候选保存。"""

    return _response(
        create_model_profile(
            db,
            step_key=payload.step_key,
            provider_key=payload.provider_key,
            model_key=payload.model_key,
            provider_config=payload.provider_config,
            activate=payload.activate,
        )
    )


@router.post("/{profile_id}/activate", response_model=ModelProfileResponse)
def activate_model_profile_endpoint(
    profile_id: str,
    db: Session = Depends(get_db),
) -> ModelProfileResponse:
    """启用同一步骤的某个已接通配置版本，并停用旧版本。"""

    return _response(activate_model_profile(db, profile_id))


@router.post("/{profile_id}/preflight", response_model=ModelProfilePreflightResponse)
def preflight_model_profile_endpoint(
    profile_id: str,
    db: Session = Depends(get_db),
) -> ModelProfilePreflightResponse:
    """执行不生成内容的候选配置预检，供启用前排查密钥与基础环境。"""

    from datetime import datetime, timezone

    checks = preflight_model_profile(db, profile_id)
    return ModelProfilePreflightResponse(
        profile_id=profile_id,
        ready=all(check["status"] != "failed" for check in checks),
        checked_at=datetime.now(timezone.utc),
        checks=checks,
    )


@router.get("/{profile_id}/evaluations", response_model=list[ModelEvaluationResponse])
def list_model_evaluations_endpoint(
    profile_id: str,
    db: Session = Depends(get_db),
) -> list[ModelEvaluationResponse]:
    """读取一版模型配置的人工小样本验收记录。"""

    return [_evaluation_response(item) for item in list_model_evaluations(db, profile_id)]


@router.post(
    "/{profile_id}/evaluations",
    response_model=ModelEvaluationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_model_evaluation_endpoint(
    profile_id: str,
    payload: ModelEvaluationCreateRequest,
    db: Session = Depends(get_db),
) -> ModelEvaluationResponse:
    """记录一次已由人工完成的小样本验收汇总。"""

    return _evaluation_response(
        create_model_evaluation(
            db,
            profile_id=profile_id,
            scenario=payload.scenario,
            sample_count=payload.sample_count,
            success_count=payload.success_count,
            total_cost_yuan=payload.total_cost_yuan,
            average_latency_seconds=payload.average_latency_seconds,
            quality_score=payload.quality_score,
            notes=payload.notes,
        )
    )


@router.get("/evaluation-comparisons", response_model=list[ModelEvaluationComparisonResponse])
def list_model_evaluation_comparisons_endpoint(
    step_key: Optional[str] = None,
    db: Session = Depends(get_db),
) -> list[ModelEvaluationComparisonResponse]:
    """返回同一工作流步骤下各候选版本的实测行，不替用户混合不同测试场景。"""

    return [
        _comparison_response(evaluation, profile)
        for evaluation, profile in list_model_evaluation_comparisons(db, step_key)
    ]
