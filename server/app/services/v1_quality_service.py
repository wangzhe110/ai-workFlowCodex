"""LemonFlow V1 模型质量与成本的可追溯聚合服务。

这个模块只读取 ``ModelInvocation`` 与人工 ``ReviewDecision``，生成不可变的
``ModelQualityEvaluation`` 快照。它不会触发任何模型调用、不会修改槽位绑定，也不
会根据分数或成本自动替换生产模型；是否切换始终由制作人到模型中心手工决定。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CharacterReferenceImage,
    ModelInvocation,
    ModelProfile,
    ModelQualityEvaluation,
    PromptTemplate,
    ReferenceAnalysis,
    ReviewDecision,
    RunStatus,
    SceneReferenceImage,
    ShotKeyframe,
    StoryProposal,
    VideoClip,
)


TERMINAL_STATUSES = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
POSITIVE_DECISIONS = {"LOCKED", "SELECTED", "APPROVED"}
REVIEW_TARGETS = {
    "STORY_PROPOSAL": StoryProposal,
    "CHARACTER_REFERENCE_IMAGE": CharacterReferenceImage,
    "SCENE_REFERENCE_IMAGE": SceneReferenceImage,
    "SHOT_KEYFRAME": ShotKeyframe,
    "VIDEO_CLIP": VideoClip,
}


def utcnow() -> datetime:
    """使用带时区 UTC 时间创建可比较的报表快照。"""

    return datetime.now(timezone.utc)


def _as_float(value: object) -> Optional[float]:
    """统一 SQLAlchemy Numeric、float 与空值，避免把无成本误报成零成本。"""

    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return float(value)
    return None


def _decision_invocation_id(db: Session, decision: ReviewDecision) -> Optional[str]:
    """把审核对象回溯到它的模型调用；不猜测跨版本或跨项目的关联。"""

    if decision.target_type == "REFERENCE_ANALYSIS":
        analysis = db.get(ReferenceAnalysis, decision.target_id)
        if analysis is None or analysis.project_id != decision.project_id:
            return None
        return db.scalar(
            select(ModelInvocation.id)
            .where(
                ModelInvocation.project_id == decision.project_id,
                ModelInvocation.workflow_run_id == analysis.workflow_run_id,
                ModelInvocation.task_type == "VIDEO_ANALYSIS",
            )
            .order_by(ModelInvocation.created_at.desc())
            .limit(1)
        )

    entity_type = REVIEW_TARGETS.get(decision.target_type)
    if entity_type is None:
        return None
    entity = db.get(entity_type, decision.target_id)
    if entity is None or entity.project_id != decision.project_id:
        return None
    invocation_id = getattr(entity, "model_invocation_id", None)
    return invocation_id if isinstance(invocation_id, str) and invocation_id else None


def _review_signals(db: Session) -> dict[str, list[ReviewDecision]]:
    """按调用 ID 收集所有正式审核决定，旧的无评分决定仍可贡献采用率。"""

    result: dict[str, list[ReviewDecision]] = defaultdict(list)
    for decision in db.scalars(select(ReviewDecision).order_by(ReviewDecision.created_at)).all():
        invocation_id = _decision_invocation_id(db, decision)
        if invocation_id:
            result[invocation_id].append(decision)
    return result


def refresh_quality_evaluations(
    db: Session,
    *,
    task_type: Optional[str] = None,
    scenario: str = "ALL_V1_PRODUCTION",
) -> list[ModelQualityEvaluation]:
    """从已终态调用生成新的质量快照，不覆盖旧快照也不改动生产配置。"""

    statement = select(ModelInvocation).where(ModelInvocation.status.in_(TERMINAL_STATUSES))
    clean_task_type = task_type.strip().upper() if task_type else None
    if clean_task_type:
        statement = statement.where(ModelInvocation.task_type == clean_task_type)
    invocations = list(db.scalars(statement.order_by(ModelInvocation.created_at)).all())
    signals = _review_signals(db)
    grouped: dict[tuple[str, Optional[str], str, str], list[ModelInvocation]] = defaultdict(list)
    for invocation in invocations:
        grouped[(invocation.model_profile_id, invocation.prompt_template_id, invocation.task_type, scenario)].append(invocation)

    created: list[ModelQualityEvaluation] = []
    for (profile_id, prompt_id, current_task_type, current_scenario), rows in grouped.items():
        success_count = sum(row.status == RunStatus.SUCCEEDED for row in rows)
        costs = [cost for row in rows if (cost := _as_float(row.cost_amount)) is not None]
        latencies = [row.latency_ms for row in rows if row.latency_ms is not None]
        decisions = [decision for row in rows for decision in signals.get(row.id, [])]
        scores = [decision.quality_score for decision in decisions if decision.quality_score is not None]
        adopted_count = sum(decision.decision in POSITIVE_DECISIONS for decision in decisions)
        first_created = min(row.created_at for row in rows)
        last_finished = max((row.finished_at or row.created_at) for row in rows)
        currencies = {row.currency for row in rows if row.currency}
        # 一个不可变模型配置通常只会有一种币种；若部署曾写入混合币种，不做错误平均。
        average_cost = sum(costs) / len(costs) if costs and len(currencies) <= 1 else None
        notes = None if len(currencies) <= 1 else "检测到多种成本币种，本快照未计算平均成本"
        evaluation = ModelQualityEvaluation(
            model_profile_id=profile_id,
            prompt_template_id=prompt_id,
            task_type=current_task_type,
            scenario=current_scenario,
            aggregation_start=first_created,
            aggregation_end=last_finished,
            sample_count=len(rows),
            success_count=success_count,
            success_rate=success_count / len(rows),
            average_cost_amount=average_cost,
            currency=next(iter(currencies)) if len(currencies) == 1 else "MIXED",
            average_latency_ms=round(sum(latencies) / len(latencies)) if latencies else None,
            average_human_score=round(sum(scores) / len(scores), 2) if scores else None,
            # 采用率只在确实出现审核决定时展示，避免把“尚未审核”误解为“未采用”。
            adoption_rate=adopted_count / len(decisions) if decisions else None,
            source="AUTO_AGGREGATED",
            notes=notes,
        )
        db.add(evaluation)
        created.append(evaluation)
    db.commit()
    for item in created:
        db.refresh(item)
    return created


def list_latest_quality_evaluations(
    db: Session, *, task_type: Optional[str] = None
) -> list[tuple[ModelQualityEvaluation, ModelProfile, Optional[PromptTemplate]]]:
    """每组只显示最新快照；全部历史仍留在数据库以供审计回溯。"""

    statement = select(ModelQualityEvaluation).order_by(ModelQualityEvaluation.created_at.desc())
    clean_task_type = task_type.strip().upper() if task_type else None
    if clean_task_type:
        statement = statement.where(ModelQualityEvaluation.task_type == clean_task_type)
    latest: list[ModelQualityEvaluation] = []
    seen: set[tuple[str, Optional[str], str, str]] = set()
    for item in db.scalars(statement).all():
        key = (item.model_profile_id, item.prompt_template_id, item.task_type, item.scenario)
        if key not in seen:
            seen.add(key)
            latest.append(item)
    profiles = {item.id: item for item in db.scalars(select(ModelProfile).where(ModelProfile.id.in_([row.model_profile_id for row in latest]))).all()} if latest else {}
    prompt_ids = [row.prompt_template_id for row in latest if row.prompt_template_id]
    prompts = {item.id: item for item in db.scalars(select(PromptTemplate).where(PromptTemplate.id.in_(prompt_ids))).all()} if prompt_ids else {}
    return [(item, profiles[item.model_profile_id], prompts.get(item.prompt_template_id)) for item in latest if item.model_profile_id in profiles]
