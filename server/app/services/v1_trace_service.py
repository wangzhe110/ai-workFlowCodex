"""LemonFlow V1 项目级生产追溯读模型。

生产追溯面向制作负责人回答“这一版结果到底用了什么”。它仅暴露已冻结的版本
标识、状态与聚合用量；模型原始 Prompt、输入和输出仍保留在受限审计表中，不通过
普通前端接口返回，避免把参考素材内容或内部指令扩散出去。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ModelInvocation, ModelSlot, PromptTemplate, WorkflowRun


def _number(value: object) -> float | None:
    """将 Numeric 等数据库数值安全转换为 API 使用的 float。"""

    return float(value) if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) else None


def list_project_invocation_traces(db: Session, *, project_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """返回项目 V1 调用审计的安全视图，最新调用在前。"""

    invocations = list(
        db.scalars(
            select(ModelInvocation)
            .where(ModelInvocation.project_id == project_id)
            .order_by(ModelInvocation.created_at.desc())
            .limit(limit)
        ).all()
    )
    if not invocations:
        return []
    slots = {
        item.id: item
        for item in db.scalars(
            select(ModelSlot).where(ModelSlot.id.in_([row.model_slot_id for row in invocations]))
        ).all()
    }
    prompt_ids = [row.prompt_template_id for row in invocations if row.prompt_template_id]
    prompts = {
        item.id: item
        for item in db.scalars(select(PromptTemplate).where(PromptTemplate.id.in_(prompt_ids))).all()
    } if prompt_ids else {}
    run_ids = [row.workflow_run_id for row in invocations if row.workflow_run_id]
    runs = {
        item.id: item
        for item in db.scalars(select(WorkflowRun).where(WorkflowRun.id.in_(run_ids))).all()
    } if run_ids else {}

    traces: list[dict[str, Any]] = []
    for invocation in invocations:
        profile_snapshot = invocation.model_profile_snapshot if isinstance(invocation.model_profile_snapshot, dict) else {}
        prompt_snapshot = invocation.prompt_snapshot if isinstance(invocation.prompt_snapshot, dict) else {}
        slot = slots.get(invocation.model_slot_id)
        prompt = prompts.get(invocation.prompt_template_id)
        run = runs.get(invocation.workflow_run_id)
        traces.append(
            {
                "id": invocation.id,
                "workflow_run_id": invocation.workflow_run_id,
                "workflow_key": run.workflow_key if run else None,
                "workflow_version": run.workflow_version if run else None,
                "task_type": invocation.task_type,
                "slot_key": slot.slot_key if slot else "UNKNOWN_SLOT",
                "model_display_name": str(profile_snapshot.get("display_name") or profile_snapshot.get("model_key") or "未记录模型"),
                "model_key": str(profile_snapshot.get("model_key") or "未记录"),
                "model_version": str(profile_snapshot.get("model_version") or profile_snapshot.get("model_key") or "未记录"),
                "model_profile_version": profile_snapshot.get("version") if isinstance(profile_snapshot.get("version"), int) else None,
                "prompt_template_id": invocation.prompt_template_id,
                "prompt_template_version_id": invocation.prompt_template_version_id,
                "prompt_key": prompt_snapshot.get("prompt_key") if isinstance(prompt_snapshot.get("prompt_key"), str) else None,
                "prompt_content_hash": prompt_snapshot.get("content_hash") if isinstance(prompt_snapshot.get("content_hash"), str) else None,
                "prompt_name": prompt_snapshot.get("display_name") if isinstance(prompt_snapshot.get("display_name"), str) else (prompt_snapshot.get("name") if isinstance(prompt_snapshot.get("name"), str) else (prompt.name if prompt else None)),
                "prompt_version": prompt_snapshot.get("prompt_version") if isinstance(prompt_snapshot.get("prompt_version"), int) else (prompt_snapshot.get("version") if isinstance(prompt_snapshot.get("version"), int) else (prompt.version if prompt else None)),
                "status": invocation.status.value,
                "provider_task_id": invocation.provider_task_id,
                "input_tokens": invocation.input_tokens,
                "output_tokens": invocation.output_tokens,
                "media_units": invocation.media_units if isinstance(invocation.media_units, dict) else {},
                "cost_amount": _number(invocation.cost_amount),
                "currency": invocation.currency,
                "latency_ms": invocation.latency_ms,
                "error_code": invocation.error_code,
                "created_at": invocation.created_at,
                "finished_at": invocation.finished_at,
            }
        )
    return traces
