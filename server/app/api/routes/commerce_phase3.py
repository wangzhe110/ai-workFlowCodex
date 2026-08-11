"""Commerce Phase 3 的知识库与生成能力 HTTP 契约。

路由不直接连接向量库或真实模型。未来登录中间件应覆盖
``current_phase3_principal``，供应商 webhook 校验应覆盖
``verify_generation_callback``；当前两个依赖均不会信任客户端传来的身份或密钥。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import GenerationModality, RunStatus
from app.schemas import (
    GenerationCallbackRequest,
    GenerationTaskCreateRequest,
    GenerationTaskResponse,
    KnowledgeChunkCreateRequest,
    KnowledgeChunkResponse,
    RetrievalHitResponse,
    RetrievalPreviewRequest,
    RetrievalPreviewResponse,
    ViralCaseCreateRequest,
    ViralCasePageResponse,
    ViralCasePatchRequest,
    ViralCaseResponse,
    ViralPatternCreateRequest,
    ViralPatternPageResponse,
    ViralPatternPatchRequest,
    ViralPatternPublishRequest,
    ViralPatternResponse,
)
from app.services.access_billing_service import ProjectPrincipal, require_permission
from app.services.generation_service import (
    GenerationRequest,
    GenerationResult,
    apply_generation_callback,
    get_generation_task,
    submit_generation,
)
from app.services.knowledge_service import (
    archive_viral_case,
    archive_viral_pattern,
    create_knowledge_chunk,
    create_viral_case,
    create_viral_pattern,
    get_viral_case,
    get_viral_pattern,
    list_viral_cases,
    list_viral_patterns,
    publish_viral_pattern_version,
    update_viral_case,
    update_viral_pattern_draft,
)
from app.services.retrieval_service import RetrievalFilter, RetrievalQuery, retrieve


router = APIRouter(prefix="/api/v1/commerce/projects/{project_id}", tags=["Commerce Phase 3"])


def current_phase3_principal() -> Optional[ProjectPrincipal]:
    """认证接入点：当前返回空，绝不从 body/header 伪造身份。

    部署真实认证后替换此依赖即可把经过验证的主体传给相同的服务级 RBAC。
    """

    return None


def verify_generation_callback() -> None:
    """供应商回调校验接入点；V1 骨架不支持真实供应商 webhook。"""

    return None


def _authorize_if_configured(
    db: Session,
    *,
    project_id: str,
    principal: Optional[ProjectPrincipal],
    permission: str,
) -> None:
    if principal is not None:
        require_permission(db, project_id=project_id, principal=principal, permission=permission)


def _case_response(item) -> ViralCaseResponse:
    return ViralCaseResponse(
        id=item.id, project_id=item.project_id, source_type=item.source_type,
        source_identifier=item.source_identifier, source_url=item.source_url, title=item.title,
        summary=item.summary, raw_text=item.raw_text, transcript_reference=item.transcript_reference,
        raw_analysis=item.raw_analysis, structured_analysis=item.structured_analysis, tags=item.tags,
        category=item.category, status=item.status.value, created_by=item.created_by,
        created_at=item.created_at, updated_at=item.updated_at,
    )


def _pattern_response(item) -> ViralPatternResponse:
    return ViralPatternResponse(
        id=item.id, project_id=item.project_id, pattern_key=item.pattern_key,
        source_case_id=item.source_case_id, pattern_type=item.pattern_type.value, name=item.name,
        summary=item.summary, structured_rules=item.structured_rules,
        applicable_scenarios=item.applicable_scenarios, tags=item.tags, version=item.version,
        is_current=item.is_current, status=item.status.value, created_by=item.created_by,
        created_at=item.created_at, updated_at=item.updated_at,
    )


def _chunk_response(item) -> KnowledgeChunkResponse:
    return KnowledgeChunkResponse(
        id=item.id, viral_case_id=item.viral_case_id, viral_pattern_id=item.viral_pattern_id,
        resource_type=item.resource_type.value, resource_id=item.resource_id, chunk_index=item.chunk_index,
        content=item.content, content_hash=item.content_hash, metadata=item.metadata_json,
        embedding_provider=item.embedding_provider, embedding_model=item.embedding_model,
        embedding_dimension=item.embedding_dimension, external_vector_id=item.external_vector_id,
        created_at=item.created_at, updated_at=item.updated_at,
    )


def _generation_response(item) -> GenerationTaskResponse:
    return GenerationTaskResponse(
        id=item.id, project_id=item.project_id, modality=item.modality.value, capability=item.capability,
        idempotency_key=item.idempotency_key, request_snapshot=item.request_snapshot,
        provider_key=item.provider_key, model_key=item.model_key, provider_task_id=item.provider_task_id,
        output_reference=item.output_reference, usage=item.usage, fallback_used=item.fallback_used,
        status=item.status.value, error_code=item.error_code, error_message=item.error_message,
        created_at=item.created_at, started_at=item.started_at, finished_at=item.finished_at,
    )


@router.post("/knowledge/cases", response_model=ViralCaseResponse, status_code=201)
def create_case_endpoint(
    project_id: str,
    payload: ViralCaseCreateRequest,
    principal: Optional[ProjectPrincipal] = Depends(current_phase3_principal),
    db: Session = Depends(get_db),
) -> ViralCaseResponse:
    _authorize_if_configured(db, project_id=project_id, principal=principal, permission="knowledge.write")
    return _case_response(create_viral_case(db, project_id=project_id, payload=payload.model_dump()))


@router.get("/knowledge/cases", response_model=ViralCasePageResponse)
def list_cases_endpoint(
    project_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status_value: Optional[str] = Query(default=None, alias="status"),
    category: Optional[str] = Query(default=None, max_length=100),
    tag: Optional[str] = Query(default=None, max_length=80),
    keyword: Optional[str] = Query(default=None, max_length=240),
    principal: Optional[ProjectPrincipal] = Depends(current_phase3_principal),
    db: Session = Depends(get_db),
) -> ViralCasePageResponse:
    _authorize_if_configured(db, project_id=project_id, principal=principal, permission="knowledge.read")
    items, total = list_viral_cases(
        db, project_id=project_id, page=page, page_size=page_size, status_value=status_value,
        category=category, tag=tag, keyword=keyword,
    )
    return ViralCasePageResponse(items=[_case_response(item) for item in items], page=page, page_size=page_size, total=total)


@router.get("/knowledge/cases/{case_id}", response_model=ViralCaseResponse)
def get_case_endpoint(
    project_id: str,
    case_id: str,
    principal: Optional[ProjectPrincipal] = Depends(current_phase3_principal),
    db: Session = Depends(get_db),
) -> ViralCaseResponse:
    _authorize_if_configured(db, project_id=project_id, principal=principal, permission="knowledge.read")
    return _case_response(get_viral_case(db, project_id=project_id, case_id=case_id))


@router.patch("/knowledge/cases/{case_id}", response_model=ViralCaseResponse)
def update_case_endpoint(
    project_id: str,
    case_id: str,
    payload: ViralCasePatchRequest,
    principal: Optional[ProjectPrincipal] = Depends(current_phase3_principal),
    db: Session = Depends(get_db),
) -> ViralCaseResponse:
    _authorize_if_configured(db, project_id=project_id, principal=principal, permission="knowledge.write")
    return _case_response(update_viral_case(db, project_id=project_id, case_id=case_id, changes=payload.model_dump(exclude_unset=True)))


@router.post("/knowledge/cases/{case_id}/archive", response_model=ViralCaseResponse)
def archive_case_endpoint(
    project_id: str,
    case_id: str,
    principal: Optional[ProjectPrincipal] = Depends(current_phase3_principal),
    db: Session = Depends(get_db),
) -> ViralCaseResponse:
    _authorize_if_configured(db, project_id=project_id, principal=principal, permission="knowledge.write")
    return _case_response(archive_viral_case(db, project_id=project_id, case_id=case_id))


@router.post("/knowledge/patterns", response_model=ViralPatternResponse, status_code=201)
def create_pattern_endpoint(
    project_id: str,
    payload: ViralPatternCreateRequest,
    principal: Optional[ProjectPrincipal] = Depends(current_phase3_principal),
    db: Session = Depends(get_db),
) -> ViralPatternResponse:
    _authorize_if_configured(db, project_id=project_id, principal=principal, permission="knowledge.write")
    return _pattern_response(create_viral_pattern(db, project_id=project_id, payload=payload.model_dump()))


@router.get("/knowledge/patterns", response_model=ViralPatternPageResponse)
def list_patterns_endpoint(
    project_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status_value: Optional[str] = Query(default=None, alias="status"),
    pattern_type: Optional[str] = Query(default=None, max_length=80),
    tag: Optional[str] = Query(default=None, max_length=80),
    keyword: Optional[str] = Query(default=None, max_length=240),
    principal: Optional[ProjectPrincipal] = Depends(current_phase3_principal),
    db: Session = Depends(get_db),
) -> ViralPatternPageResponse:
    _authorize_if_configured(db, project_id=project_id, principal=principal, permission="knowledge.read")
    items, total = list_viral_patterns(
        db, project_id=project_id, page=page, page_size=page_size, status_value=status_value,
        pattern_type=pattern_type, tag=tag, keyword=keyword,
    )
    return ViralPatternPageResponse(items=[_pattern_response(item) for item in items], page=page, page_size=page_size, total=total)


@router.get("/knowledge/patterns/{pattern_id}", response_model=ViralPatternResponse)
def get_pattern_endpoint(
    project_id: str,
    pattern_id: str,
    principal: Optional[ProjectPrincipal] = Depends(current_phase3_principal),
    db: Session = Depends(get_db),
) -> ViralPatternResponse:
    _authorize_if_configured(db, project_id=project_id, principal=principal, permission="knowledge.read")
    return _pattern_response(get_viral_pattern(db, project_id=project_id, pattern_id=pattern_id))


@router.patch("/knowledge/patterns/{pattern_id}", response_model=ViralPatternResponse)
def update_pattern_endpoint(
    project_id: str,
    pattern_id: str,
    payload: ViralPatternPatchRequest,
    principal: Optional[ProjectPrincipal] = Depends(current_phase3_principal),
    db: Session = Depends(get_db),
) -> ViralPatternResponse:
    _authorize_if_configured(db, project_id=project_id, principal=principal, permission="knowledge.write")
    return _pattern_response(update_viral_pattern_draft(db, project_id=project_id, pattern_id=pattern_id, changes=payload.model_dump(exclude_unset=True)))


@router.post("/knowledge/patterns/{pattern_id}/publish", response_model=ViralPatternResponse)
def publish_pattern_endpoint(
    project_id: str,
    pattern_id: str,
    payload: ViralPatternPublishRequest,
    principal: Optional[ProjectPrincipal] = Depends(current_phase3_principal),
    db: Session = Depends(get_db),
) -> ViralPatternResponse:
    _authorize_if_configured(db, project_id=project_id, principal=principal, permission="knowledge.write")
    updates = payload.updates.model_dump(exclude_unset=True) if payload.updates else None
    return _pattern_response(publish_viral_pattern_version(db, project_id=project_id, pattern_id=pattern_id, payload=updates))


@router.post("/knowledge/patterns/{pattern_id}/archive", response_model=ViralPatternResponse)
def archive_pattern_endpoint(
    project_id: str,
    pattern_id: str,
    principal: Optional[ProjectPrincipal] = Depends(current_phase3_principal),
    db: Session = Depends(get_db),
) -> ViralPatternResponse:
    _authorize_if_configured(db, project_id=project_id, principal=principal, permission="knowledge.write")
    return _pattern_response(archive_viral_pattern(db, project_id=project_id, pattern_id=pattern_id))


@router.post("/knowledge/chunks", response_model=KnowledgeChunkResponse, status_code=201)
def create_chunk_endpoint(
    project_id: str,
    payload: KnowledgeChunkCreateRequest,
    principal: Optional[ProjectPrincipal] = Depends(current_phase3_principal),
    db: Session = Depends(get_db),
) -> KnowledgeChunkResponse:
    _authorize_if_configured(db, project_id=project_id, principal=principal, permission="knowledge.write")
    return _chunk_response(create_knowledge_chunk(db, project_id=project_id, payload=payload.model_dump()))


@router.post("/knowledge/retrieval-preview", response_model=RetrievalPreviewResponse)
def retrieval_preview_endpoint(
    project_id: str,
    payload: RetrievalPreviewRequest,
    principal: Optional[ProjectPrincipal] = Depends(current_phase3_principal),
    db: Session = Depends(get_db),
) -> RetrievalPreviewResponse:
    _authorize_if_configured(db, project_id=project_id, principal=principal, permission="knowledge.read")
    hits, call = retrieve(
        db,
        provider_key=payload.provider_key,
        query=RetrievalQuery(
            project_id=project_id, query_text=payload.query_text, top_k=payload.top_k,
            filters=RetrievalFilter(
                resource_types=tuple(payload.resource_types), tags=tuple(payload.tags), statuses=tuple(payload.statuses),
            ),
            request_id=payload.request_id or "",
        ),
    )
    return RetrievalPreviewResponse(
        retrieval_call_id=call.id, provider_key=call.provider_key, status=call.status.value,
        hits=[RetrievalHitResponse(rank=rank, chunk_id=item.chunk_id, resource_type=item.resource_type, resource_id=item.resource_id, score=item.score, metadata=item.metadata) for rank, item in enumerate(hits, start=1)],
    )


@router.post("/generation-tasks", response_model=GenerationTaskResponse, status_code=202)
def submit_generation_endpoint(
    project_id: str,
    payload: GenerationTaskCreateRequest,
    principal: Optional[ProjectPrincipal] = Depends(current_phase3_principal),
    db: Session = Depends(get_db),
) -> GenerationTaskResponse:
    _authorize_if_configured(db, project_id=project_id, principal=principal, permission="generation.submit")
    task, _ = submit_generation(
        db,
        request=GenerationRequest(
            project_id=project_id, modality=GenerationModality(payload.modality), capability=payload.capability,
            model_key=payload.model_key, parameters=payload.parameters, idempotency_key=payload.idempotency_key,
            preferred_provider=payload.preferred_provider, fallback_providers=tuple(payload.fallback_providers),
        ),
    )
    return _generation_response(task)


@router.get("/generation-tasks/{task_id}", response_model=GenerationTaskResponse)
def get_generation_endpoint(
    project_id: str,
    task_id: str,
    principal: Optional[ProjectPrincipal] = Depends(current_phase3_principal),
    db: Session = Depends(get_db),
) -> GenerationTaskResponse:
    _authorize_if_configured(db, project_id=project_id, principal=principal, permission="generation.read")
    return _generation_response(get_generation_task(db, project_id=project_id, task_id=task_id))


@router.post("/generation-tasks/{task_id}/callbacks", response_model=GenerationTaskResponse)
def generation_callback_endpoint(
    project_id: str,
    task_id: str,
    payload: GenerationCallbackRequest,
    _: None = Depends(verify_generation_callback),
    db: Session = Depends(get_db),
) -> GenerationTaskResponse:
    """仅用于 Fake/未来验签后的 Provider 回调；无真实 Provider 时不会自动触发。"""

    task = apply_generation_callback(
        db, project_id=project_id, task_id=task_id, provider_key=payload.provider_key,
        provider_task_id=payload.provider_task_id,
        result=GenerationResult(
            status=RunStatus(payload.status), output_reference=payload.output_reference,
            usage=payload.usage, sanitized_response=payload.sanitized_response,
            error_code=payload.error_code, error_message=payload.error_message,
        ),
    )
    return _generation_response(task)
