"""向量库无关的 RAG 检索边界；默认只注册可注入的内存 Fake Provider。"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Protocol
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    KnowledgeChunk,
    KnowledgeResourceType,
    Project,
    RetrievalCall,
    RunStatus,
    ViralCase,
    ViralKnowledgeStatus,
    ViralPattern,
)


@dataclass(frozen=True)
class RetrievalFilter:
    resource_types: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ("ACTIVE",)


@dataclass(frozen=True)
class RetrievalQuery:
    project_id: str
    query_text: str
    top_k: int = 5
    filters: RetrievalFilter = field(default_factory=RetrievalFilter)
    request_id: str = ""


@dataclass(frozen=True)
class RetrievalHit:
    chunk_id: str
    resource_type: str
    resource_id: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


class RetrieverProvider(Protocol):
    """任何向量库适配器都只需实现此纯领域协议。"""

    key: str

    def search(self, query: RetrievalQuery) -> list[RetrievalHit]:
        ...


class RetrieverRegistry:
    """Provider 注册表没有供应商 SDK；测试可显式注入 Fake 实现。"""

    def __init__(self) -> None:
        self._providers: dict[str, RetrieverProvider] = {}

    def register(self, provider: RetrieverProvider) -> None:
        self._providers[provider.key] = provider

    def unregister(self, key: str) -> None:
        self._providers.pop(key, None)

    def resolve(self, key: str) -> RetrieverProvider | None:
        return self._providers.get(key)


class RetrieverRouter:
    """检索 Provider 路由点；替换向量库只改注册，不改业务检索服务。"""

    def resolve(self, provider_key: str) -> RetrieverProvider | None:
        return retriever_registry.resolve(provider_key)


class FakeInMemoryRetriever:
    """确定性测试检索器，不生成 embedding，也不访问网络。"""

    key = "fake_in_memory"

    def __init__(self, hits: list[RetrievalHit] | None = None, *, error: Exception | None = None) -> None:
        self.hits = list(hits or [])
        self.error = error
        self.queries: list[RetrievalQuery] = []

    def search(self, query: RetrievalQuery) -> list[RetrievalHit]:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return list(self.hits)


retriever_registry = RetrieverRegistry()
retriever_registry.register(FakeInMemoryRetriever())
retriever_router = RetrieverRouter()


def _error(detail: str, code: int) -> None:
    raise HTTPException(status_code=code, detail=detail)


def _is_active_and_owned(
    db: Session,
    *,
    project_id: str,
    chunk: KnowledgeChunk,
    resource_types: tuple[str, ...],
    tags: tuple[str, ...],
    statuses: tuple[str, ...],
) -> bool:
    """二次校验数据库归属与生命周期，绝不盲目信任 Provider 返回。"""

    if resource_types and chunk.resource_type.value not in resource_types:
        return False
    if chunk.resource_type == KnowledgeResourceType.VIRAL_CASE:
        parent = db.get(ViralCase, chunk.viral_case_id)
    else:
        parent = db.get(ViralPattern, chunk.viral_pattern_id)
    if parent is None or parent.project_id != project_id:
        return False
    if parent.status.value not in statuses:
        return False
    if tags and not set(tags).intersection(parent.tags or []):
        return False
    return True


def retrieve(
    db: Session,
    *,
    provider_key: str,
    query: RetrievalQuery,
) -> tuple[list[RetrievalHit], RetrievalCall]:
    """路由、去重、过滤和追踪一次检索，Provider 失败统一转换为 503。"""

    valid_resource_types = {item.value for item in KnowledgeResourceType}
    valid_statuses = {item.value for item in ViralKnowledgeStatus}
    if db.get(Project, query.project_id) is None:
        _error("项目不存在", status.HTTP_404_NOT_FOUND)
    if not query.query_text.strip() or not 1 <= query.top_k <= 50:
        _error("query_text 不能为空且 top_k 必须为 1 至 50", status.HTTP_422_UNPROCESSABLE_CONTENT)
    if any(value not in valid_resource_types for value in query.filters.resource_types):
        _error("resource_types 包含不支持的资源类型", status.HTTP_422_UNPROCESSABLE_CONTENT)
    if any(value not in valid_statuses for value in query.filters.statuses):
        _error("statuses 包含不支持的知识状态", status.HTTP_422_UNPROCESSABLE_CONTENT)
    provider = retriever_router.resolve(provider_key)
    if provider is None:
        _error("检索 Provider 未配置", status.HTTP_503_SERVICE_UNAVAILABLE)
    call = RetrievalCall(
        project_id=query.project_id,
        provider_key=provider_key,
        request_id=query.request_id or str(uuid4()),
        query_text=query.query_text.strip(),
        filter_snapshot={
            "top_k": query.top_k,
            "resource_types": list(query.filters.resource_types),
            "tags": list(query.filters.tags),
            "statuses": list(query.filters.statuses),
        },
        result_references=[],
        status=RunStatus.RUNNING,
    )
    db.add(call)
    db.flush()
    started = perf_counter()
    try:
        raw_hits = provider.search(query)
    except Exception as exc:
        call.status = RunStatus.FAILED
        call.error_code = "RETRIEVER_PROVIDER_ERROR"
        call.error_summary = type(exc).__name__[:500]
        call.latency_ms = int((perf_counter() - started) * 1000)
        from app.models.entities import utcnow
        call.finished_at = utcnow()
        db.commit()
        _error("检索 Provider 暂不可用", status.HTTP_503_SERVICE_UNAVAILABLE)
        raise AssertionError("unreachable") from exc

    by_id: dict[str, RetrievalHit] = {}
    for hit in raw_hits:
        if not isinstance(hit, RetrievalHit) or hit.chunk_id in by_id:
            continue
        chunk = db.get(KnowledgeChunk, hit.chunk_id)
        if chunk is None or chunk.resource_type.value != hit.resource_type or chunk.resource_id != hit.resource_id:
            continue
        if not _is_active_and_owned(
            db,
            project_id=query.project_id,
            chunk=chunk,
            resource_types=query.filters.resource_types,
            tags=query.filters.tags,
            statuses=query.filters.statuses,
        ):
            continue
        by_id[hit.chunk_id] = hit
    hits = sorted(by_id.values(), key=lambda item: (-item.score, item.chunk_id))[: query.top_k]
    from app.models.entities import utcnow
    call.status = RunStatus.SUCCEEDED
    call.latency_ms = int((perf_counter() - started) * 1000)
    call.finished_at = utcnow()
    call.result_references = [
        {
            "rank": rank,
            "chunk_id": item.chunk_id,
            "resource_type": item.resource_type,
            "resource_id": item.resource_id,
            "score": item.score,
        }
        for rank, item in enumerate(hits, start=1)
    ]
    db.commit()
    db.refresh(call)
    return hits, call
