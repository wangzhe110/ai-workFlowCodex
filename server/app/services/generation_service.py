"""统一图片/视频 Provider 骨架；仅含 Fake 实现，不调用真实供应商。"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Protocol
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    GenerationInvocation,
    GenerationModality,
    GenerationTask,
    Project,
    RunStatus,
)
from app.models.entities import utcnow


@dataclass(frozen=True)
class ProviderCapability:
    modality: GenerationModality
    capability: str
    supports_async: bool
    supports_callback: bool


@dataclass(frozen=True)
class GenerationRequest:
    project_id: str
    modality: GenerationModality
    capability: str
    model_key: str
    parameters: dict[str, Any]
    idempotency_key: str | None = None
    preferred_provider: str | None = None
    fallback_providers: tuple[str, ...] = ()


@dataclass(frozen=True)
class GenerationResult:
    status: RunStatus
    provider_task_id: str | None = None
    output_reference: dict[str, Any] | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    sanitized_response: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


class GenerationProvider(Protocol):
    key: str
    capabilities: tuple[ProviderCapability, ...]

    def submit(self, request: GenerationRequest) -> GenerationResult:
        ...


class GenerationProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, GenerationProvider] = {}

    def register(self, provider: GenerationProvider) -> None:
        self._providers[provider.key] = provider

    def unregister(self, key: str) -> None:
        self._providers.pop(key, None)

    def resolve(self, key: str) -> GenerationProvider | None:
        return self._providers.get(key)


class GenerationProviderRouter:
    """按首选、后备顺序路由，业务服务不依赖任何供应商类或 SDK。"""

    def candidates_for(self, request: GenerationRequest) -> tuple[str, ...]:
        return tuple(key for key in (request.preferred_provider, *request.fallback_providers) if key)


class FakeGenerationProvider:
    """可配置成功、失败、超时和异步结果的确定性测试 Provider。"""

    def __init__(self, key: str, *, behavior: str = "success") -> None:
        self.key = key
        self.behavior = behavior
        self.capabilities = (
            ProviderCapability(GenerationModality.IMAGE, "image_generate", True, True),
            ProviderCapability(GenerationModality.VIDEO, "video_generate", True, True),
        )
        self.requests: list[GenerationRequest] = []

    def submit(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        if self.behavior == "raise":
            raise TimeoutError("fake timeout")
        if self.behavior == "fail":
            return GenerationResult(
                status=RunStatus.FAILED,
                error_code="FAKE_PROVIDER_FAILED",
                error_message="Fake provider configured to fail",
                sanitized_response={"outcome": "failed"},
            )
        task_id = f"fake-{self.key}-{uuid4().hex[:12]}"
        if self.behavior == "async":
            return GenerationResult(
                status=RunStatus.RUNNING,
                provider_task_id=task_id,
                sanitized_response={"outcome": "accepted"},
            )
        unit_usage = {"image_count": 1} if request.modality == GenerationModality.IMAGE else {"video_seconds": 4}
        return GenerationResult(
            status=RunStatus.SUCCEEDED,
            provider_task_id=task_id,
            output_reference={"type": request.modality.value, "uri": f"fake://{task_id}"},
            usage=unit_usage,
            sanitized_response={"outcome": "succeeded", "task_id": task_id},
        )


generation_provider_registry = GenerationProviderRegistry()
generation_provider_router = GenerationProviderRouter()


def _error(detail: str, code: int) -> None:
    raise HTTPException(status_code=code, detail=detail)


def _project_or_404(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        _error("项目不存在", status.HTTP_404_NOT_FOUND)
    return project


def _supports(provider: GenerationProvider, request: GenerationRequest) -> bool:
    return any(item.modality == request.modality and item.capability == request.capability for item in provider.capabilities)


def _safe_snapshot(request: GenerationRequest) -> dict[str, Any]:
    """拒绝把明显认证信息保存到任务或 invocation 快照。"""

    forbidden = ("api_key", "apikey", "authorization", "password", "secret", "token")
    for key in request.parameters:
        if any(word in key.lower() for word in forbidden):
            _error("生成参数不能包含密钥或认证信息", status.HTTP_422_UNPROCESSABLE_CONTENT)
    return {
        "capability": request.capability,
        "model_key": request.model_key,
        "parameters": dict(request.parameters),
        "preferred_provider": request.preferred_provider,
        "fallback_providers": list(request.fallback_providers),
    }


def _record_invocation(
    db: Session,
    *,
    task: GenerationTask,
    attempt_number: int,
    provider_key: str,
    request: GenerationRequest,
    result: GenerationResult,
    started_at,
    latency_ms: int,
) -> GenerationInvocation:
    invocation = GenerationInvocation(
        generation_task_id=task.id,
        project_id=task.project_id,
        attempt_number=attempt_number,
        provider_key=provider_key,
        model_key=request.model_key,
        request_snapshot=_safe_snapshot(request),
        sanitized_response=dict(result.sanitized_response),
        provider_task_id=result.provider_task_id,
        usage=dict(result.usage),
        status=result.status,
        error_code=result.error_code,
        error_message=(result.error_message or None)[:500] if result.error_message else None,
        latency_ms=latency_ms,
        created_at=started_at,
        finished_at=utcnow() if result.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED} else None,
    )
    db.add(invocation)
    return invocation


def _apply_result(
    task: GenerationTask,
    *,
    provider_key: str,
    request: GenerationRequest,
    result: GenerationResult,
    latency_ms: int | None = None,
) -> None:
    """单向状态机：终态永不被回调或 fallback 失败覆盖。"""

    if task.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
        return
    task.provider_key = provider_key
    task.model_key = request.model_key
    task.provider_task_id = result.provider_task_id or task.provider_task_id
    task.usage = dict(result.usage)
    task.status = result.status
    task.error_code = result.error_code
    task.error_message = (result.error_message or None)[:500] if result.error_message else None
    task.latency_ms = latency_ms
    if result.status == RunStatus.SUCCEEDED:
        task.output_reference = dict(result.output_reference or {})
        task.finished_at = utcnow()
    elif result.status == RunStatus.FAILED:
        task.finished_at = utcnow()


def submit_generation(db: Session, *, request: GenerationRequest) -> tuple[GenerationTask, bool]:
    """创建或复用项目内幂等任务，并依次执行首选/Fallback Fake Provider。"""

    _project_or_404(db, request.project_id)
    snapshot = _safe_snapshot(request)
    if request.idempotency_key:
        existing = db.scalar(
            select(GenerationTask).where(
                GenerationTask.project_id == request.project_id,
                GenerationTask.idempotency_key == request.idempotency_key,
            )
        )
        if existing is not None:
            return existing, False
    candidates = generation_provider_router.candidates_for(request)
    if not candidates:
        _error("没有配置可用的生成 Provider", status.HTTP_503_SERVICE_UNAVAILABLE)
    if not any(
        (provider := generation_provider_registry.resolve(provider_key)) is not None and _supports(provider, request)
        for provider_key in candidates
    ):
        # 未配置能力不能创建看似已提交的任务，更不能退化为伪成功；后续接入真实
        # Provider 时也必须显式注册到本 Registry 才能走到提交代码。
        _error("生成 Provider 未配置或不支持该能力", status.HTTP_503_SERVICE_UNAVAILABLE)
    task = GenerationTask(
        project_id=request.project_id,
        modality=request.modality,
        capability=request.capability,
        idempotency_key=request.idempotency_key,
        request_snapshot=snapshot,
        status=RunStatus.PENDING,
    )
    try:
        with db.begin_nested():
            db.add(task)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(GenerationTask).where(
                GenerationTask.project_id == request.project_id,
                GenerationTask.idempotency_key == request.idempotency_key,
            )
        )
        if existing is not None:
            return existing, False
        raise
    task.status = RunStatus.RUNNING
    task.started_at = utcnow()
    errors: list[str] = []
    attempted = 0
    for provider_key in candidates:
        provider = generation_provider_registry.resolve(provider_key)
        if provider is None or not _supports(provider, request):
            errors.append(f"{provider_key}:unavailable")
            continue
        attempted += 1
        started = utcnow()
        started_monotonic = perf_counter()
        try:
            result = provider.submit(request)
        except Exception as exc:
            result = GenerationResult(
                status=RunStatus.FAILED,
                error_code="GENERATION_PROVIDER_EXCEPTION",
                error_message=type(exc).__name__,
                sanitized_response={"outcome": "exception"},
            )
        _record_invocation(
            db, task=task, attempt_number=attempted, provider_key=provider_key,
            request=request, result=result, started_at=started,
            latency_ms=int((perf_counter() - started_monotonic) * 1000),
        )
        if result.status in {RunStatus.SUCCEEDED, RunStatus.RUNNING}:
            task.fallback_used = attempted > 1
            _apply_result(
                task, provider_key=provider_key, request=request, result=result,
                latency_ms=int((perf_counter() - started_monotonic) * 1000),
            )
            db.commit()
            db.refresh(task)
            return task, True
        errors.append(f"{provider_key}:{result.error_code or 'failed'}")
    task.status = RunStatus.FAILED
    task.error_code = "GENERATION_ALL_PROVIDERS_FAILED" if attempted else "GENERATION_PROVIDER_UNCONFIGURED"
    task.error_message = ";".join(errors)[:500] or "没有可用的生成 Provider"
    task.finished_at = utcnow()
    task.fallback_used = attempted > 1
    db.commit()
    db.refresh(task)
    return task, True


def get_generation_task(db: Session, *, project_id: str, task_id: str) -> GenerationTask:
    task = db.scalar(select(GenerationTask).where(GenerationTask.id == task_id, GenerationTask.project_id == project_id))
    if task is None:
        _error("生成任务不存在", status.HTTP_404_NOT_FOUND)
    return task


def apply_generation_callback(
    db: Session,
    *,
    project_id: str,
    task_id: str,
    provider_key: str,
    provider_task_id: str,
    result: GenerationResult,
) -> GenerationTask:
    """应用异步回调；错误项目/Provider/task ID 或终态倒灌均不改变任务。"""

    task = get_generation_task(db, project_id=project_id, task_id=task_id)
    if task.provider_key != provider_key or task.provider_task_id != provider_task_id:
        _error("Provider 回调不属于当前生成任务", status.HTTP_409_CONFLICT)
    if task.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
        return task
    if result.status not in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.RUNNING}:
        _error("Provider 回调状态不合法", status.HTTP_422_UNPROCESSABLE_CONTENT)
    request = GenerationRequest(
        project_id=task.project_id,
        modality=task.modality,
        capability=task.capability,
        model_key=task.model_key or "unknown",
        parameters=(task.request_snapshot or {}).get("parameters") or {},
    )
    next_attempt = int(db.scalar(select(func.max(GenerationInvocation.attempt_number)).where(
        GenerationInvocation.generation_task_id == task.id
    )) or 0) + 1
    _record_invocation(
        db, task=task, attempt_number=next_attempt, provider_key=provider_key,
        request=request, result=result, started_at=utcnow(), latency_ms=0,
    )
    _apply_result(task, provider_key=provider_key, request=request, result=result, latency_ms=0)
    db.commit()
    db.refresh(task)
    return task
