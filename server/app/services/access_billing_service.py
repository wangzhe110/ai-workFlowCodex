"""Phase 3 的权限与用量领域边界。

本模块故意不接入认证、支付或第三方计费 SDK：认证层将来只需把经过验证的
``principal_id`` 注入 ``ProjectPrincipal``，再调用 ``require_permission``。这样 API
在没有登录模块的 V1 阶段也不会把客户端传来的身份字段误当作可信身份。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Project,
    ProjectMember,
    ProjectMemberRole,
    ProjectSubscription,
    SaaSPlan,
    SubscriptionStatus,
    UsageEvent,
    UsageEventKind,
)
from app.models.entities import new_id, utcnow


Permission = str


ROLE_PERMISSIONS: dict[ProjectMemberRole, frozenset[Permission]] = {
    ProjectMemberRole.OWNER: frozenset({
        "knowledge.read", "knowledge.write", "generation.read", "generation.submit",
        "billing.read", "billing.manage",
    }),
    ProjectMemberRole.ADMIN: frozenset({
        "knowledge.read", "knowledge.write", "generation.read", "generation.submit", "billing.read",
    }),
    ProjectMemberRole.EDITOR: frozenset({
        "knowledge.read", "knowledge.write", "generation.read", "generation.submit",
    }),
    ProjectMemberRole.VIEWER: frozenset({"knowledge.read", "generation.read"}),
}


@dataclass(frozen=True)
class ProjectPrincipal:
    """认证成功后由未来身份层构建的最小可信主体。"""

    principal_id: str


class QuotaPolicy(Protocol):
    """未来套餐/配额策略的扩展点；当前只保存与校验，不执行自动扣费。"""

    def allows(self, *, subscription: ProjectSubscription, capability: str, quantity: Decimal) -> bool:
        ...


class UsageMeter(Protocol):
    """未来 Provider 用量回写的扩展点，事件仍须通过不可变流水落库。"""

    def record(self, *, project_id: str, capability: str, unit: str, quantity: Decimal) -> UsageEvent:
        ...


def _error(detail: str, code: int) -> None:
    raise HTTPException(status_code=code, detail=detail)


def _project_or_404(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        _error("项目不存在", status.HTTP_404_NOT_FOUND)
    return project


def require_permission(
    db: Session,
    *,
    project_id: str,
    principal: ProjectPrincipal,
    permission: Permission,
) -> ProjectMember:
    """按项目成员事实和固定角色矩阵授权，拒绝跨项目或未知身份。"""

    _project_or_404(db, project_id)
    member = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.principal_id == principal.principal_id,
        )
    )
    allowed_permissions = ROLE_PERMISSIONS.get(member.role, frozenset()) if member is not None else frozenset()
    if member is None or permission not in allowed_permissions:
        _error("没有该项目操作权限", status.HTTP_403_FORBIDDEN)
    return member


def add_project_member(
    db: Session,
    *,
    project_id: str,
    principal_id: str,
    role: ProjectMemberRole,
) -> ProjectMember:
    """测试和未来身份同步入口；同一项目身份仅有一个角色记录。"""

    _project_or_404(db, project_id)
    principal_id = principal_id.strip()
    if not principal_id:
        _error("principal_id 不能为空", status.HTTP_422_UNPROCESSABLE_CONTENT)
    item = ProjectMember(project_id=project_id, principal_id=principal_id, role=role)
    try:
        with db.begin_nested():
            db.add(item)
            db.flush()
    except IntegrityError as exc:
        _error("该身份已是项目成员", status.HTTP_409_CONFLICT)
        raise AssertionError("unreachable") from exc
    db.commit()
    db.refresh(item)
    return item


def create_saas_plan(
    db: Session,
    *,
    code: str,
    name: str,
    quota_policy: dict[str, Any] | None = None,
) -> SaaSPlan:
    code, name = code.strip(), name.strip()
    if not code or not name:
        _error("套餐 code 和 name 不能为空", status.HTTP_422_UNPROCESSABLE_CONTENT)
    item = SaaSPlan(code=code[:80], name=name[:160], quota_policy=dict(quota_policy or {}))
    try:
        with db.begin_nested():
            db.add(item)
            db.flush()
    except IntegrityError as exc:
        _error("套餐 code 已存在", status.HTTP_409_CONFLICT)
        raise AssertionError("unreachable") from exc
    db.commit()
    db.refresh(item)
    return item


def subscribe_project(
    db: Session,
    *,
    project_id: str,
    plan_id: str,
    quota_snapshot: dict[str, Any] | None = None,
) -> ProjectSubscription:
    """创建项目套餐事实；订阅切换以取消旧订阅并新增新记录表达。"""

    _project_or_404(db, project_id)
    plan = db.get(SaaSPlan, plan_id)
    if plan is None or not plan.is_active:
        _error("套餐不存在或未启用", status.HTTP_422_UNPROCESSABLE_CONTENT)
    existing = db.scalar(
        select(ProjectSubscription).where(
            ProjectSubscription.project_id == project_id,
            ProjectSubscription.status == SubscriptionStatus.ACTIVE,
        )
    )
    if existing is not None:
        _error("项目已有启用套餐；请先取消后再创建新订阅", status.HTTP_409_CONFLICT)
    item = ProjectSubscription(
        project_id=project_id,
        plan_id=plan.id,
        status=SubscriptionStatus.ACTIVE,
        quota_snapshot=dict(quota_snapshot if quota_snapshot is not None else plan.quota_policy or {}),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def active_subscription_or_409(db: Session, *, project_id: str) -> ProjectSubscription:
    _project_or_404(db, project_id)
    item = db.scalar(
        select(ProjectSubscription).where(
            ProjectSubscription.project_id == project_id,
            ProjectSubscription.status == SubscriptionStatus.ACTIVE,
        )
    )
    if item is None:
        _error("项目没有可用套餐，不能记录生产用量", status.HTTP_409_CONFLICT)
    return item


def record_usage_event(
    db: Session,
    *,
    project_id: str,
    capability: str,
    unit: str,
    quantity: Decimal | float | int,
    idempotency_key: str,
    metadata: dict[str, Any] | None = None,
) -> tuple[UsageEvent, bool]:
    """保存不可变、幂等的实际用量；不能在无订阅项目上静默记账。"""

    subscription = active_subscription_or_409(db, project_id=project_id)
    key, capability, unit = idempotency_key.strip(), capability.strip(), unit.strip()
    amount = Decimal(str(quantity))
    if not key or not capability or not unit or amount <= 0:
        _error("用量 key、能力、单位不能为空且 quantity 必须大于 0", status.HTTP_422_UNPROCESSABLE_CONTENT)
    existing = db.scalar(
        select(UsageEvent).where(UsageEvent.project_id == project_id, UsageEvent.idempotency_key == key)
    )
    if existing is not None:
        return existing, False
    item = UsageEvent(
        project_id=project_id,
        subscription_id=subscription.id,
        idempotency_key=key[:160],
        event_kind=UsageEventKind.NORMAL,
        capability=capability[:80],
        unit=unit[:80],
        quantity=amount,
        metadata_json=dict(metadata or {}),
    )
    try:
        with db.begin_nested():
            db.add(item)
            db.flush()
    except IntegrityError as exc:
        existing = db.scalar(
            select(UsageEvent).where(UsageEvent.project_id == project_id, UsageEvent.idempotency_key == key)
        )
        if existing is not None:
            return existing, False
        raise exc
    db.commit()
    db.refresh(item)
    return item, True


def reverse_usage_event(
    db: Session,
    *,
    project_id: str,
    event_id: str,
    idempotency_key: str,
    metadata: dict[str, Any] | None = None,
) -> UsageEvent:
    """冲正追加一条负数事件，原事件永久不修改。"""

    original = db.scalar(
        select(UsageEvent).where(UsageEvent.id == event_id, UsageEvent.project_id == project_id)
    )
    if original is None:
        _error("用量事件不存在", status.HTTP_404_NOT_FOUND)
    existing = db.scalar(
        select(UsageEvent).where(UsageEvent.project_id == project_id, UsageEvent.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing
    item = UsageEvent(
        project_id=project_id,
        subscription_id=original.subscription_id,
        correction_of_event_id=original.id,
        idempotency_key=idempotency_key.strip()[:160],
        event_kind=UsageEventKind.REVERSAL,
        capability=original.capability,
        unit=original.unit,
        quantity=-Decimal(str(original.quantity)),
        metadata_json=dict(metadata or {}),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def modify_usage_event(*_: Any, **__: Any) -> None:
    """明确拒绝更新入口，强制任何修正走 ``reverse_usage_event``。"""

    _error("用量流水不可修改；请创建冲正事件", status.HTTP_409_CONFLICT)
