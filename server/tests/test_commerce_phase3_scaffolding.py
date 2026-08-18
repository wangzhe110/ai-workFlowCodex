"""Commerce Phase 3：知识库、Provider 边界、权限计量和迁移回归。"""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    GenerationInvocation,
    GenerationModality,
    GenerationTask,
    KnowledgeChunk,
    KnowledgeResourceType,
    CommerceWorkflowLink,
    CommerceWorkflowStep,
    ProductAnalysisStatus,
    ProductAnalysisVersion,
    ProductAsset,
    ProductAssetVersion,
    ProductAssetVersionStatus,
    Project,
    ProjectProductSelection,
    ProjectMemberRole,
    RunStatus,
    RetrievalCall,
    StoryRun,
    StoryRunMode,
    StoryRunStage,
    StoryRunState,
    StoryRunStatus,
    TopicCandidate,
    UsageEvent,
    UsageEventKind,
    ViralKnowledgeStatus,
    WorkflowRun,
    WorkflowStep,
)
from app.services.access_billing_service import (
    ProjectPrincipal,
    ROLE_PERMISSIONS,
    add_project_member,
    create_saas_plan,
    modify_usage_event,
    record_usage_event,
    require_permission,
    reverse_usage_event,
    subscribe_project,
)
from app.services.generation_service import (
    FakeGenerationProvider,
    GenerationRequest,
    GenerationResult,
    apply_generation_callback,
    cancel_generation_task,
    generation_provider_registry,
    submit_generation,
)
from app.services.knowledge_service import (
    archive_viral_case,
    create_knowledge_chunk,
    create_viral_case,
    create_viral_pattern,
    get_viral_case,
    publish_viral_pattern_version,
    update_viral_case,
)
from app.services.retrieval_service import (
    FakeInMemoryRetriever,
    RetrievalFilter,
    RetrievalHit,
    RetrievalQuery,
    retrieve,
    retriever_registry,
)
from app.services.sensitive_data import REDACTED_VALUE, redact_sensitive_data


@pytest.fixture()
def phase3_db():
    """Phase 3 模型使用测试 SQLite，不依赖真实 Provider 或外部服务。"""

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture(autouse=True)
def isolated_provider_registries():
    """每个测试自行注册 Fake，避免任何真实适配器或网络入口遗留。"""

    generation_provider_registry._providers.clear()
    retriever_registry._providers.clear()
    yield
    generation_provider_registry._providers.clear()
    retriever_registry._providers.clear()


def _project(db, label: str = "知识") -> Project:
    item = Project(title=f"Phase3 {label} {uuid4().hex[:8]}")
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _case_payload(source: str = "source-1", *, title: str = "爆款家庭短剧") -> dict:
    return {
        "source_type": "douyin",
        "source_identifier": source,
        "source_url": f"https://example.invalid/{source}",
        "title": title,
        "summary": "三秒冲突、情绪反转和解决方案。",
        "raw_text": "人物在开头产生误会，产品帮助解决。",
        "raw_analysis": {"hook": "conflict"},
        "structured_analysis": {"beats": ["hook", "turn", "resolve"]},
        "tags": ["家庭", "反转"],
        "category": "生活",
    }


def _request(project: Project, *, modality: GenerationModality = GenerationModality.IMAGE, key: str = "idem") -> GenerationRequest:
    return GenerationRequest(
        project_id=project.id,
        modality=modality,
        capability="image_generate" if modality == GenerationModality.IMAGE else "video_generate",
        model_key="fake-model", parameters={"prompt": "原创建议"}, idempotency_key=key,
        preferred_provider="preferred", fallback_providers=("fallback",),
    )


def _raises(code: int):
    return pytest.raises(HTTPException, match="")


def test_viral_case_lifecycle_pagination_filters_archive_and_api(phase3_db) -> None:
    """场景 #1：案例创建、筛选分页、更新、归档及管理 API 的稳定契约。"""

    project = _project(phase3_db)
    first = create_viral_case(phase3_db, project_id=project.id, payload=_case_payload("a"))
    second = create_viral_case(phase3_db, project_id=project.id, payload={**_case_payload("b", title="城市反转"), "tags": ["城市"]})
    update_viral_case(phase3_db, project_id=project.id, case_id=first.id, changes={"title": "更新后的爆款"})
    archive_viral_case(phase3_db, project_id=project.id, case_id=second.id)

    from app.services.knowledge_service import list_viral_cases
    rows, total = list_viral_cases(phase3_db, project_id=project.id, page=1, page_size=1, tag="家庭")
    assert total == 1 and rows[0].title == "更新后的爆款"
    assert second.status == ViralKnowledgeStatus.ARCHIVED

    client = TestClient(app)
    response = client.get(f"/api/v1/commerce/projects/{project.id}/knowledge/cases", params={"tag": "家庭"})
    assert response.status_code == 200
    assert response.json()["total"] == 1


def test_viral_case_duplicate_and_project_isolation(phase3_db) -> None:
    """场景 #2/#3/#22：来源仅在项目内去重，跨项目和错误引用均不留下半条记录。"""

    one, two = _project(phase3_db, "one"), _project(phase3_db, "two")
    item = create_viral_case(phase3_db, project_id=one.id, payload=_case_payload("shared"))
    with pytest.raises(HTTPException) as duplicate:
        create_viral_case(phase3_db, project_id=one.id, payload=_case_payload("shared"))
    assert duplicate.value.status_code == 409
    assert create_viral_case(phase3_db, project_id=two.id, payload=_case_payload("shared")).id
    with pytest.raises(HTTPException) as foreign:
        get_viral_case(phase3_db, project_id=two.id, case_id=item.id)
    assert foreign.value.status_code == 404
    before = phase3_db.scalar(select(func.count(KnowledgeChunk.id)))
    with pytest.raises(HTTPException) as invalid:
        create_knowledge_chunk(phase3_db, project_id=one.id, payload={"viral_case_id": item.id, "viral_pattern_id": "missing", "chunk_index": 0, "content": "x"})
    assert invalid.value.status_code == 422
    assert phase3_db.scalar(select(func.count(KnowledgeChunk.id))) == before


def test_pattern_versioning_and_state_conflicts(phase3_db) -> None:
    """场景 #4/#5：已发布模式只能通过追加版本演进，草稿规则不允许绕过。"""

    project = _project(phase3_db)
    draft = create_viral_pattern(phase3_db, project_id=project.id, payload={
        "pattern_type": "OPENING_HOOK", "name": "先反差再追问", "structured_rules": {"seconds": 3}, "tags": ["开头"],
    })
    first = publish_viral_pattern_version(phase3_db, project_id=project.id, pattern_id=draft.id)
    second = publish_viral_pattern_version(
        phase3_db, project_id=project.id, pattern_id=first.id, payload={"name": "新版反差钩子"},
    )
    assert first.id != second.id and first.version == 1 and second.version == 2
    assert first.status == ViralKnowledgeStatus.ARCHIVED and not first.is_current
    assert second.status == ViralKnowledgeStatus.ACTIVE and second.is_current
    with pytest.raises(HTTPException) as conflict:
        publish_viral_pattern_version(phase3_db, project_id=project.id, pattern_id=first.id)
    assert conflict.value.status_code == 409


def test_knowledge_chunk_source_constraints_hash_and_order(phase3_db) -> None:
    """场景 #6：来源互斥、切片位置唯一、哈希稳定都由服务和数据库共同保证。"""

    project = _project(phase3_db)
    case = create_viral_case(phase3_db, project_id=project.id, payload=_case_payload("chunks"))
    chunk = create_knowledge_chunk(phase3_db, project_id=project.id, payload={"viral_case_id": case.id, "chunk_index": 0, "content": "固定内容"})
    assert chunk.content_hash == "aaded270994eb14168722a7a3bdf55d230f6f8b8282af03cef2ed496382ebfc3"
    with pytest.raises(HTTPException) as duplicate:
        create_knowledge_chunk(phase3_db, project_id=project.id, payload={"viral_case_id": case.id, "chunk_index": 0, "content": "另一个"})
    assert duplicate.value.status_code == 409
    both = KnowledgeChunk(
        viral_case_id=case.id, viral_pattern_id=case.id, resource_type=KnowledgeResourceType.VIRAL_CASE,
        resource_id=case.id, chunk_index=8, content="x", content_hash="x", metadata_json={},
    )
    phase3_db.add(both)
    with pytest.raises(IntegrityError):
        phase3_db.flush()
    phase3_db.rollback()


def test_retrieval_routes_filters_deduplicates_and_tracks_calls(phase3_db) -> None:
    """场景 #7/#9：检索返回需经项目、状态、标签二次验证，并稳定去重排序。"""

    project, foreign_project = _project(phase3_db, "rag"), _project(phase3_db, "foreign")
    active = create_viral_case(phase3_db, project_id=project.id, payload=_case_payload("active"))
    archived = create_viral_case(phase3_db, project_id=project.id, payload=_case_payload("archived"))
    foreign = create_viral_case(phase3_db, project_id=foreign_project.id, payload=_case_payload("foreign"))
    archive_viral_case(phase3_db, project_id=project.id, case_id=archived.id)
    active_chunk = create_knowledge_chunk(phase3_db, project_id=project.id, payload={"viral_case_id": active.id, "chunk_index": 0, "content": "命中"})
    archived_chunk = create_knowledge_chunk(phase3_db, project_id=project.id, payload={"viral_case_id": archived.id, "chunk_index": 0, "content": "不应命中"})
    foreign_chunk = create_knowledge_chunk(phase3_db, project_id=foreign_project.id, payload={"viral_case_id": foreign.id, "chunk_index": 0, "content": "跨项目"})
    retriever_registry.register(FakeInMemoryRetriever([
        RetrievalHit(active_chunk.id, "VIRAL_CASE", active.id, 0.5),
        RetrievalHit(active_chunk.id, "VIRAL_CASE", active.id, 0.9),
        RetrievalHit(archived_chunk.id, "VIRAL_CASE", archived.id, 0.99),
        RetrievalHit(foreign_chunk.id, "VIRAL_CASE", foreign.id, 0.98),
    ]))
    hits, call = retrieve(
        phase3_db, provider_key="fake_in_memory",
        query=RetrievalQuery(project_id=project.id, query_text="钩子", filters=RetrievalFilter(tags=("家庭",))),
    )
    assert [item.chunk_id for item in hits] == [active_chunk.id]
    assert call.status == RunStatus.SUCCEEDED and call.result_references[0]["score"] == 0.5


def test_retrieval_provider_errors_and_unconfigured_are_stable(phase3_db) -> None:
    """场景 #8/#21：未配置 Provider 503 会落库，显式 Fake 才能执行。"""

    project = _project(phase3_db)
    assert retriever_registry.resolve("fake_in_memory") is None
    with pytest.raises(HTTPException) as missing:
        retrieve(phase3_db, provider_key="not-configured", query=RetrievalQuery(project_id=project.id, query_text="x"))
    assert missing.value.status_code == 503
    missing_call = phase3_db.scalar(select(RetrievalCall).where(RetrievalCall.provider_key == "not-configured"))
    assert missing_call is not None
    assert missing_call.project_id == project.id
    assert missing_call.status == RunStatus.FAILED
    assert missing_call.error_code == "RETRIEVER_PROVIDER_UNCONFIGURED"
    assert missing_call.filter_snapshot == {"top_k": 5, "resource_types": [], "tags": [], "statuses": ["ACTIVE"]}
    assert missing_call.created_at is not None and missing_call.finished_at is not None and missing_call.latency_ms is not None

    client = TestClient(app)
    api_response = client.post(
        f"/api/v1/commerce/projects/{project.id}/knowledge/retrieval-preview",
        json={"provider_key": "api-not-configured", "query_text": "x"},
    )
    assert api_response.status_code == 503
    assert phase3_db.scalar(select(RetrievalCall).where(RetrievalCall.provider_key == "api-not-configured")) is not None

    retriever_registry.register(FakeInMemoryRetriever(error=RuntimeError("boom")))
    with pytest.raises(HTTPException) as error:
        retrieve(phase3_db, provider_key="fake_in_memory", query=RetrievalQuery(project_id=project.id, query_text="x"))
    assert error.value.status_code == 503
    assert phase3_db.scalar(select(func.count()).select_from(RetrievalCall).where(RetrievalCall.status == RunStatus.FAILED)) >= 3

    retriever_registry.unregister("fake_in_memory")
    retriever_registry.register(FakeInMemoryRetriever([]))
    hits, empty_call = retrieve(
        phase3_db, provider_key="fake_in_memory", query=RetrievalQuery(project_id=project.id, query_text="configured-empty")
    )
    assert hits == [] and empty_call.status == RunStatus.SUCCEEDED and empty_call.error_code is None


def test_image_video_fake_providers_async_callback_and_terminal_protection(phase3_db) -> None:
    """场景 #10/#11/#15/#16：图片视频、异步回调、终态保护和 usage 追踪。"""

    project = _project(phase3_db)
    image_provider = FakeGenerationProvider("preferred", behavior="success")
    generation_provider_registry.register(image_provider)
    image, created = submit_generation(phase3_db, request=_request(project, key="image"))
    assert created and image.status == RunStatus.SUCCEEDED and image.usage == {"image_count": 1}
    assert image.invocations[0].latency_ms is not None

    async_provider = FakeGenerationProvider("async", behavior="async")
    generation_provider_registry.unregister("preferred")
    generation_provider_registry.register(async_provider)
    video, _ = submit_generation(
        phase3_db,
        request=GenerationRequest(project_id=project.id, modality=GenerationModality.VIDEO, capability="video_generate", model_key="fake", parameters={}, idempotency_key="video", preferred_provider="async"),
    )
    assert video.status == RunStatus.RUNNING and video.provider_task_id
    updated = apply_generation_callback(
        phase3_db, project_id=project.id, task_id=video.id, provider_key="async", provider_task_id=video.provider_task_id,
        result=GenerationResult(status=RunStatus.SUCCEEDED, output_reference={"uri": "fake://video"}, usage={"video_seconds": 4, "input_tokens": 8}),
    )
    assert updated.status == RunStatus.SUCCEEDED and updated.usage["video_seconds"] == 4
    with pytest.raises(HTTPException) as protected:
        apply_generation_callback(
            phase3_db, project_id=project.id, task_id=video.id, provider_key="async", provider_task_id=video.provider_task_id,
            result=GenerationResult(status=RunStatus.FAILED, error_code="late"),
        )
    assert protected.value.status_code == 409
    generation_provider_registry.register(FakeGenerationProvider("broken-video", behavior="fail"))
    failed_video, _ = submit_generation(
        phase3_db,
        request=GenerationRequest(project_id=project.id, modality=GenerationModality.VIDEO, capability="video_generate", model_key="fake", parameters={}, idempotency_key="video-fail", preferred_provider="broken-video"),
    )
    assert failed_video.status == RunStatus.FAILED and failed_video.invocations[-1].error_code == "FAKE_PROVIDER_FAILED"


def test_generation_fallback_idempotency_project_scope_and_callback_binding(phase3_db) -> None:
    """场景 #12/#13/#14：fallback 全链审计，幂等只在项目内，错误回调不能串任务。"""

    project, other = _project(phase3_db, "one"), _project(phase3_db, "two")
    generation_provider_registry.register(FakeGenerationProvider("preferred", behavior="fail"))
    fallback = FakeGenerationProvider("fallback", behavior="success")
    generation_provider_registry.register(fallback)
    first, created = submit_generation(phase3_db, request=_request(project, key="same"))
    duplicate, duplicate_created = submit_generation(phase3_db, request=_request(project, key="same"))
    other_task, _ = submit_generation(phase3_db, request=_request(other, key="same"))
    assert created and not duplicate_created and duplicate.id == first.id and other_task.id != first.id
    assert first.fallback_used and [row.provider_key for row in first.invocations] == ["preferred", "fallback"]
    assert first.invocations[0].status == RunStatus.FAILED and first.invocations[1].status == RunStatus.SUCCEEDED
    with pytest.raises(HTTPException) as wrong_callback:
        apply_generation_callback(phase3_db, project_id=other.id, task_id=first.id, provider_key="fallback", provider_task_id=first.provider_task_id or "x", result=GenerationResult(status=RunStatus.SUCCEEDED))
    assert wrong_callback.value.status_code == 404


def test_nested_sensitive_data_is_redacted_before_generation_and_retrieval_persistence(phase3_db) -> None:
    """场景 #21：嵌套请求、响应与异常的敏感键只以稳定占位符落库。"""

    class SecretProvider(FakeGenerationProvider):
        def submit(self, request: GenerationRequest) -> GenerationResult:
            self.requests.append(request)
            return GenerationResult(
                status=RunStatus.FAILED,
                sanitized_response={
                    "headers": {"Authorization": "Bearer response-secret"},
                    "items": [{"X-API-Key": "response-api-key"}],
                    "normal": "保留响应字段",
                },
                error_code="SECRET_PROVIDER_FAILED",
                error_message="provider authorization: Bearer error-secret, client_secret=error-client-secret",
                usage={"image_count": 1, "input_tokens": 12},
            )

    assert redact_sensitive_data(({"Refresh_Token": "tuple-secret"}, "普通 token 文本")) == (
        {"Refresh_Token": REDACTED_VALUE}, "普通 token 文本"
    )

    project = _project(phase3_db)
    request_secret = "request-api-secret"
    response_secret = "response-secret"
    error_secret = "error-secret"
    generation_provider_registry.register(SecretProvider("secret-provider"))
    task, created = submit_generation(
        phase3_db,
        request=GenerationRequest(
            project_id=project.id,
            modality=GenerationModality.IMAGE,
            capability="image_generate",
            model_key="secret-model",
            parameters={
                "prompt": "token 是正常提示词文本，不是字段名",
                "outer": {"api_key": request_secret, "nested": [{"Access-Token": "request-token-secret"}]},
                "array": [{"client_secret": "request-client-secret"}, {"safe": "保留字段"}],
            },
            idempotency_key="nested-secret",
            preferred_provider="secret-provider",
        ),
    )
    assert created and task.status == RunStatus.FAILED and task.usage == {"image_count": 1, "input_tokens": 12}
    phase3_db.expire_all()
    persisted_task = phase3_db.get(GenerationTask, task.id)
    invocation = phase3_db.scalar(select(GenerationInvocation).where(GenerationInvocation.generation_task_id == task.id))
    assert persisted_task is not None and invocation is not None
    persisted = json.dumps(
        {
            "task_request": persisted_task.request_snapshot,
            "task_error": persisted_task.error_message,
            "invocation_request": invocation.request_snapshot,
            "invocation_response": invocation.sanitized_response,
            "invocation_error": invocation.error_message,
        },
        ensure_ascii=False,
    )
    for secret in (request_secret, "request-token-secret", "request-client-secret", response_secret, "response-api-key", error_secret, "error-client-secret"):
        assert secret not in persisted
    assert persisted.count("[REDACTED]") >= 6
    assert persisted_task.request_snapshot["parameters"]["prompt"] == "token 是正常提示词文本，不是字段名"
    assert invocation.sanitized_response["normal"] == "保留响应字段"

    case = create_viral_case(phase3_db, project_id=project.id, payload=_case_payload("redaction"))
    chunk = create_knowledge_chunk(phase3_db, project_id=project.id, payload={"viral_case_id": case.id, "chunk_index": 0, "content": "检索结果"})
    retriever_registry.register(FakeInMemoryRetriever([
        RetrievalHit(
            chunk.id, "VIRAL_CASE", case.id, 0.9,
            metadata={"nested": [{"proxy-authorization": "rag-header-secret"}], "safe": "保留检索字段"},
        )
    ], error=RuntimeError("api-key=rag-error-secret")))
    with pytest.raises(HTTPException) as provider_error:
        retrieve(phase3_db, provider_key="fake_in_memory", query=RetrievalQuery(project_id=project.id, query_text="检索"))
    assert provider_error.value.status_code == 503
    failed_call = phase3_db.scalar(select(RetrievalCall).where(RetrievalCall.status == RunStatus.FAILED).order_by(RetrievalCall.created_at.desc()))
    assert failed_call is not None and "rag-error-secret" not in (failed_call.error_summary or "")

    retriever_registry.unregister("fake_in_memory")
    retriever_registry.register(FakeInMemoryRetriever([
        RetrievalHit(
            chunk.id, "VIRAL_CASE", case.id, 0.9,
            metadata={"nested": [{"proxy-authorization": "rag-header-secret"}], "safe": "保留检索字段"},
        )
    ]))
    hits, retrieval_call = retrieve(phase3_db, provider_key="fake_in_memory", query=RetrievalQuery(project_id=project.id, query_text="检索成功"))
    retrieved = json.dumps({"hits": [item.metadata for item in hits], "references": retrieval_call.result_references}, ensure_ascii=False)
    assert "rag-header-secret" not in retrieved and "[REDACTED]" in retrieved and "保留检索字段" in retrieved


def test_generation_missing_provider_and_cancelled_terminal_state_machine(phase3_db) -> None:
    """场景 #21/#22：无 Provider 不伪造任务；取消终态有结束时间且不可逆。"""

    project = _project(phase3_db)
    before = phase3_db.scalar(select(func.count()).select_from(GenerationTask))
    with pytest.raises(HTTPException) as missing:
        submit_generation(phase3_db, request=GenerationRequest(project_id=project.id, modality=GenerationModality.IMAGE, capability="image_generate", model_key="x", parameters={}, preferred_provider="real"))
    assert missing.value.status_code == 503
    assert phase3_db.scalar(select(func.count()).select_from(GenerationTask)) == before

    pending = GenerationTask(
        project_id=project.id,
        modality=GenerationModality.IMAGE,
        capability="image_generate",
        request_snapshot={"parameters": {}},
        status=RunStatus.PENDING,
    )
    phase3_db.add(pending)
    phase3_db.commit()
    cancelled_pending = cancel_generation_task(phase3_db, project_id=project.id, task_id=pending.id)
    phase3_db.expire_all()
    assert phase3_db.get(GenerationTask, pending.id).status == RunStatus.CANCELLED
    assert cancelled_pending.finished_at is not None

    async_provider = FakeGenerationProvider("async", behavior="async")
    generation_provider_registry.register(async_provider)
    running, _ = submit_generation(
        phase3_db,
        request=GenerationRequest(project_id=project.id, modality=GenerationModality.VIDEO, capability="video_generate", model_key="fake", parameters={}, idempotency_key="cancel-running", preferred_provider="async"),
    )
    cancelled_running = cancel_generation_task(phase3_db, project_id=project.id, task_id=running.id)
    assert cancelled_running.status == RunStatus.CANCELLED and cancelled_running.finished_at is not None
    invocation_count = phase3_db.scalar(select(func.count()).select_from(GenerationInvocation).where(GenerationInvocation.generation_task_id == running.id))
    for terminal_result in (RunStatus.RUNNING, RunStatus.SUCCEEDED, RunStatus.FAILED):
        with pytest.raises(HTTPException) as invalid_transition:
            apply_generation_callback(
                phase3_db, project_id=project.id, task_id=running.id, provider_key="async", provider_task_id=running.provider_task_id or "missing",
                result=GenerationResult(status=terminal_result),
            )
        assert invalid_transition.value.status_code == 409
    assert phase3_db.scalar(select(func.count()).select_from(GenerationInvocation).where(GenerationInvocation.generation_task_id == running.id)) == invocation_count

    generation_provider_registry.unregister("async")
    generation_provider_registry.register(FakeGenerationProvider("preferred", behavior="success"))
    succeeded, _ = submit_generation(phase3_db, request=_request(project, key="completed"))
    assert succeeded.status == RunStatus.SUCCEEDED and succeeded.finished_at is not None
    with pytest.raises(HTTPException) as already_finished:
        cancel_generation_task(phase3_db, project_id=project.id, task_id=succeeded.id)
    assert already_finished.value.status_code == 409


def test_phase3_management_api_contracts_delegate_to_services(phase3_db) -> None:
    """管理接口通过依赖注入委托服务层，返回稳定 201/202/404/409/422 语义。"""

    project = _project(phase3_db, "api")
    client = TestClient(app)
    root = f"/api/v1/commerce/projects/{project.id}"
    created_case = client.post(f"{root}/knowledge/cases", json=_case_payload("api-case"))
    assert created_case.status_code == 201
    case_id = created_case.json()["id"]
    assert client.patch(f"{root}/knowledge/cases/{case_id}", json={"title": "API 更新"}).status_code == 200
    assert client.post(f"{root}/knowledge/cases", json=_case_payload("api-case")).status_code == 409
    assert client.get(f"{root}/knowledge/cases/not-found").status_code == 404
    assert client.post(f"{root}/knowledge/chunks", json={"viral_case_id": case_id, "viral_pattern_id": case_id, "chunk_index": 0, "content": "x"}).status_code == 422

    pattern = client.post(f"{root}/knowledge/patterns", json={"pattern_type": "OPENING_HOOK", "name": "API 钩子"})
    assert pattern.status_code == 201
    assert client.post(f"{root}/knowledge/patterns/{pattern.json()['id']}/publish", json={}).status_code == 200

    generation_provider_registry.register(FakeGenerationProvider("preferred", behavior="success"))
    submitted = client.post(f"{root}/generation-tasks", json={
        "modality": "IMAGE", "capability": "image_generate", "model_key": "fake", "parameters": {},
        "preferred_provider": "preferred", "idempotency_key": "api-generation",
    })
    assert submitted.status_code == 202
    assert client.get(f"{root}/generation-tasks/{submitted.json()['id']}").status_code == 200


def test_role_matrix_is_centralized_default_deny_and_project_scoped(phase3_db) -> None:
    """场景 #19/#20：四种角色、未知权限与跨项目成员均遵循同一 RBAC 策略。"""

    project, other = _project(phase3_db, "roles"), _project(phase3_db, "other")
    for role in ProjectMemberRole:
        member = add_project_member(phase3_db, project_id=project.id, principal_id=role.value.lower(), role=role)
        for permission in ("knowledge.read", "knowledge.write", "generation.submit", "generation.read", "billing.read", "billing.manage"):
            principal = ProjectPrincipal(member.principal_id)
            if permission in ROLE_PERMISSIONS[role]:
                assert require_permission(phase3_db, project_id=project.id, principal=principal, permission=permission).id == member.id
            else:
                with pytest.raises(HTTPException) as denied:
                    require_permission(phase3_db, project_id=project.id, principal=principal, permission=permission)
                assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as unknown:
        require_permission(phase3_db, project_id=project.id, principal=ProjectPrincipal("owner"), permission="unknown.permission")
    assert unknown.value.status_code == 403
    with pytest.raises(HTTPException) as cross_project:
        require_permission(phase3_db, project_id=other.id, principal=ProjectPrincipal("owner"), permission="knowledge.read")
    assert cross_project.value.status_code == 403


def test_usage_is_idempotent_immutable_and_reversible(phase3_db) -> None:
    """场景 #17/#18：无套餐拒绝、重复不上账、冲正可审计且原记录不可修改。"""

    project = _project(phase3_db, "billing")
    with pytest.raises(HTTPException) as no_subscription:
        record_usage_event(phase3_db, project_id=project.id, capability="image_generate", unit="image", quantity=1, idempotency_key="first")
    assert no_subscription.value.status_code == 409
    plan = create_saas_plan(phase3_db, code=f"p-{uuid4().hex[:8]}", name="测试套餐", quota_policy={"image": 10})
    subscribe_project(phase3_db, project_id=project.id, plan_id=plan.id)
    event, created = record_usage_event(phase3_db, project_id=project.id, capability="image_generate", unit="image", quantity=1, idempotency_key="first", metadata={"image_count": 1, "input_tokens": 5})
    duplicate, duplicate_created = record_usage_event(phase3_db, project_id=project.id, capability="image_generate", unit="image", quantity=1, idempotency_key="first")
    reverse = reverse_usage_event(phase3_db, project_id=project.id, event_id=event.id, idempotency_key="reverse-first")
    assert created and not duplicate_created and duplicate.id == event.id
    assert reverse.event_kind == UsageEventKind.REVERSAL and Decimal(str(reverse.quantity)) == Decimal("-1")
    with pytest.raises(HTTPException) as immutable:
        modify_usage_event()
    assert immutable.value.status_code == 409


def test_0016_usage_event_trigger_blocks_orm_and_core_updates_but_allows_reversal(tmp_path) -> None:
    """场景 #17/#18：0016 的数据库 trigger 拒绝任意 UPDATE，冲正仍是新行。"""

    database_url = f"sqlite:///{tmp_path / 'usage-event-trigger.db'}"
    _migration_runner(database_url, "head")
    migration_engine = create_engine(database_url)
    try:
        with Session(migration_engine) as db:
            db.execute(text("PRAGMA foreign_keys=ON"))
            project = Project(title="UsageEvent 触发器项目")
            db.add(project)
            db.commit()
            db.refresh(project)
            plan = create_saas_plan(db, code="trigger-plan", name="Trigger Plan", quota_policy={"image": 10})
            subscribe_project(db, project_id=project.id, plan_id=plan.id)
            original, created = record_usage_event(
                db, project_id=project.id, capability="image_generate", unit="image", quantity=1,
                idempotency_key="trigger-original", metadata={"image_count": 1},
            )
            assert created
            original_id = original.id
            original_project_id = original.project_id
            original_key = original.idempotency_key
            db.expire_all()
            persisted = db.get(UsageEvent, original_id)
            assert persisted is not None
            persisted.quantity = Decimal("2")
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()
            persisted = db.get(UsageEvent, original_id)
            assert persisted is not None
            assert Decimal(str(persisted.quantity)) == Decimal("1")
            assert persisted.project_id == original_project_id and persisted.idempotency_key == original_key
            for column, value in (
                ("unit", "video"),
                ("project_id", "different-project-id"),
                ("idempotency_key", "mutated-key"),
            ):
                with pytest.raises(IntegrityError):
                    db.execute(text(f"UPDATE usage_events SET {column} = :value WHERE id = :id"), {"value": value, "id": original_id})
                    db.commit()
                db.rollback()
            reversal = reverse_usage_event(
                db, project_id=original_project_id, event_id=original_id, idempotency_key="trigger-reversal"
            )
            assert reversal.id != original_id
            assert reversal.correction_of_event_id == original_id
            assert reversal.idempotency_key == "trigger-reversal"
            assert Decimal(str(reversal.quantity)) == Decimal("-1")
    finally:
        migration_engine.dispose()

def _migration_runner(database_url: str, revision: str, *, downgrade: bool = False) -> None:
    server_root = Path(__file__).resolve().parents[1]
    config = Config(str(server_root / "alembic.ini"))
    config.set_main_option("script_location", str(server_root / "migrations"))
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
            config.attributes["connection"] = connection
            (command.downgrade if downgrade else command.upgrade)(config, revision)
            config.attributes.pop("connection", None)
    finally:
        migration_engine.dispose()

def test_0016_empty_sqlite_upgrade_downgrade_and_reupgrade(tmp_path) -> None:
    """场景 #23：空库升级当前 head，0016 trigger 与后续 Commerce 表在往返后仍准确恢复。"""

    database_url = f"sqlite:///{tmp_path / 'empty-0016.db'}"
    server_root = Path(__file__).resolve().parents[1]
    config = Config(str(server_root / "alembic.ini"))
    config.set_main_option("script_location", str(server_root / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == ["0022_model_parameter_capabilities"]
    _migration_runner(database_url, "head")
    migration_engine = create_engine(database_url)
    try:
        tables = set(inspect(migration_engine).get_table_names())
        assert {"viral_cases", "viral_patterns", "knowledge_chunks", "retrieval_calls", "generation_tasks", "generation_invocations", "project_members", "saas_plans", "project_subscriptions", "usage_events", "commerce_reference_intakes", "commerce_creative_batches", "commerce_creative_ideas", "commerce_story_run_inputs", "commerce_character_design_versions", "commerce_final_videos"}.issubset(tables)
        with migration_engine.connect() as connection:
            assert "trg_usage_events_immutable_update_0016" in {
                row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            }
    finally:
        migration_engine.dispose()
    _migration_runner(database_url, "0015_commerce_phase3_knowledge_generation_scaffolding", downgrade=True)
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.connect() as connection:
            assert "trg_usage_events_immutable_update_0016" not in {
                row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            }
    finally:
        migration_engine.dispose()
    _migration_runner(database_url, "head")
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.connect() as connection:
            assert "trg_usage_events_immutable_update_0016" in {
                row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            }
            assert connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '%alembic_tmp%'").fetchall() == []
    finally:
        migration_engine.dispose()


def test_0003_uses_frozen_v1_foundation_metadata_and_0009_installs_asset_foreign_keys(tmp_path) -> None:
    """历史 0003 不得随着当前 ORM 提前引用 0009 资产中心表。

    SQLite 可以接受指向尚未创建表的外键，因此这里同时直接检查 0003 的本地快照，
    并从空库跑至 0009，确认资产字段和约束只在正确 revision 出现。
    """

    database_url = f"sqlite:///{tmp_path / 'frozen-v1-foundation.db'}"
    _migration_runner(database_url, "0003_v1_production_foundation")
    migration_engine = create_engine(database_url)
    try:
        inspector = inspect(migration_engine)
        assert "character_assets" not in inspector.get_table_names()
        assert "asset_library_character_id" not in {
            column["name"] for column in inspector.get_columns("character_definitions")
        }
        assert "asset_library_scene_id" not in {
            column["name"] for column in inspector.get_columns("scene_definitions")
        }
        assert "asset_version_id" not in {
            column["name"] for column in inspector.get_columns("character_reference_images")
        }
        assert "story_proposal_id" not in {
            column["name"] for column in inspector.get_columns("character_definitions")
        }
        assert "quality_score" not in {
            column["name"] for column in inspector.get_columns("review_decisions")
        }
        assert "currency" not in {
            column["name"] for column in inspector.get_columns("model_quality_evaluations")
        }
        # 继续逐项防回归：0004 只做回填；0005--0009 对 *0003 新表* 的字段、
        # 索引和状态都不得因当前 ORM 定义而提前出现。workflow_runs、
        # workflow_steps 和 model_profiles 是 0001 初始表，不在本次 0003 修复范围。
        assert "selected_video_clip_id" not in {
            column["name"] for column in inspector.get_columns("shot_plans")
        }
        assert "emotion" not in {
            column["name"] for column in inspector.get_columns("shot_plans")
        }
        assert "ix_character_definitions_asset_library_character_id" not in {
            index["name"] for index in inspector.get_indexes("character_definitions")
        }
    finally:
        migration_engine.dispose()

    _migration_runner(database_url, "0009_phase4_asset_center_and_structured_shots")
    migration_engine = create_engine(database_url)
    try:
        inspector = inspect(migration_engine)
        assert {"asset_libraries", "character_assets", "character_asset_versions", "scene_assets", "scene_asset_versions"} <= set(
            inspector.get_table_names()
        )
        expected = (
            ("character_definitions", "asset_library_character_id", "character_assets"),
            ("scene_definitions", "asset_library_scene_id", "scene_assets"),
            ("character_reference_images", "asset_version_id", "character_asset_versions"),
            ("scene_reference_images", "asset_version_id", "scene_asset_versions"),
            ("shot_asset_bindings", "character_asset_version_id", "character_asset_versions"),
            ("shot_asset_bindings", "scene_asset_version_id", "scene_asset_versions"),
            ("video_clip_asset_bindings", "character_asset_version_id", "character_asset_versions"),
            ("video_clip_asset_bindings", "scene_asset_version_id", "scene_asset_versions"),
        )
        for table_name, column_name, referent_table in expected:
            matches = [
                foreign_key
                for foreign_key in inspector.get_foreign_keys(table_name)
                if foreign_key["constrained_columns"] == [column_name]
                and foreign_key["referred_table"] == referent_table
                and foreign_key["referred_columns"] == ["id"]
            ]
            assert len(matches) == 1
        with migration_engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    finally:
        migration_engine.dispose()

def _insert_nonempty_phase12_graph(database_url: str) -> dict[str, str]:
    """在真实 0014 schema 写入完整 Commerce Phase 1/2 有效关系图。"""

    migration_engine = create_engine(database_url)
    try:
        with Session(migration_engine) as db:
            db.execute(text("PRAGMA foreign_keys=ON"))
            project = Project(title="非空 Commerce Phase1/2", description="迁移完整性夹具")
            db.add(project)
            db.flush()
            seed_run = WorkflowRun(project_id=project.id, workflow_key="fixture_seed", status=RunStatus.SUCCEEDED)
            db.add(seed_run)
            db.flush()
            topic = TopicCandidate(
                project_id=project.id,
                generation_run_id=seed_run.id,
                position=1,
                title="完整关系选题",
                opening_hook="三秒痛点",
                synopsis="用于验证带货短剧迁移不丢失已有数据。",
            )
            product = ProductAsset(name="迁移产品", description="完整产品主体")
            db.add_all((topic, product))
            db.flush()
            analysis = ProductAnalysisVersion(
                product_asset_id=product.id,
                version=1,
                product_identification={"name": "迁移产品"},
                package_ocr={"text": "包装 OCR"},
                raw_analysis={"source": "fixture", "selling_points": ["省时"]},
                analysis_status=ProductAnalysisStatus.SUCCEEDED,
            )
            db.add(analysis)
            db.flush()
            product_version = ProductAssetVersion(
                product_asset_id=product.id,
                source_analysis_version_id=analysis.id,
                version=1,
                product_name="迁移产品生产版",
                appearance_description="白色瓶身",
                selling_points=[{"text": "省时"}],
                user_pain_points=[{"text": "忙碌"}],
                usage_scenarios=[{"text": "下班回家"}],
                package_ocr={"text": "包装 OCR"},
                reference_images=[{"url": "fixture://product"}],
                status=ProductAssetVersionStatus.CONFIRMED,
                frozen_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            db.add(product_version)
            db.flush()
            selection = ProjectProductSelection(
                project_id=project.id,
                product_asset_id=product.id,
                product_asset_version_id=product_version.id,
            )
            db.add(selection)
            db.flush()
            story_run = StoryRun(
                project_id=project.id,
                topic_candidate_id=topic.id,
                project_product_selection_id=selection.id,
                product_asset_version_id=product_version.id,
                run_number=1,
                mode=StoryRunMode.STEPWISE,
            )
            db.add(story_run)
            db.flush()
            story_state = StoryRunState(
                story_run_id=story_run.id,
                current_stage=StoryRunStage.OUTLINE,
                status=StoryRunStatus.PAUSED,
                stage_data={"fixture": "phase1-phase2"},
            )
            parent_run = WorkflowRun(
                project_id=project.id,
                workflow_key="commerce_story_run",
                status=RunStatus.RUNNING,
                input_snapshot={"fixture": "commerce-parent"},
            )
            db.add_all((story_state, parent_run))
            db.flush()
            link = CommerceWorkflowLink(workflow_run_id=parent_run.id, story_run_id=story_run.id)
            db.add(link)
            db.flush()
            step = WorkflowStep(
                workflow_run_id=parent_run.id,
                step_key=StoryRunStage.OUTLINE.value,
                position=2,
                attempt=1,
                status=RunStatus.PENDING,
                input_payload={"fixture": "outline-attempt"},
                idempotency_key="fixture-commerce-outline-1",
            )
            db.add(step)
            db.flush()
            sidecar = CommerceWorkflowStep(
                workflow_step_id=step.id,
                workflow_run_id=parent_run.id,
                story_run_id=story_run.id,
                stage=StoryRunStage.OUTLINE.value,
                attempt=1,
                status=RunStatus.PENDING.value,
            )
            db.add(sidecar)
            db.commit()
            return {
                "project_id": project.id,
                "seed_run_id": seed_run.id,
                "topic_id": topic.id,
                "product_id": product.id,
                "analysis_id": analysis.id,
                "product_version_id": product_version.id,
                "selection_id": selection.id,
                "story_run_id": story_run.id,
                "parent_run_id": parent_run.id,
                "step_id": step.id,
            }
    finally:
        migration_engine.dispose()


def _phase12_snapshot(database_url: str) -> dict[str, list[dict]]:
    """以核心字段和外键关系生成稳定快照，而非只比较行数。"""

    queries = {
        "projects": "SELECT id, title, description FROM projects ORDER BY id",
        "workflow_runs": "SELECT id, project_id, workflow_key, status, input_snapshot FROM workflow_runs ORDER BY id",
        "topic_candidates": "SELECT id, project_id, generation_run_id, position, title, opening_hook, synopsis, status FROM topic_candidates ORDER BY id",
        "product_assets": "SELECT id, name, description FROM product_assets ORDER BY id",
        "product_analysis_versions": "SELECT id, product_asset_id, version, product_identification, package_ocr, raw_analysis, analysis_status FROM product_analysis_versions ORDER BY id",
        "product_asset_versions": "SELECT id, product_asset_id, source_analysis_version_id, version, product_name, appearance_description, selling_points, user_pain_points, usage_scenarios, package_ocr, reference_images, status, frozen_at FROM product_asset_versions ORDER BY id",
        "project_product_selections": "SELECT id, project_id, product_asset_id, product_asset_version_id FROM project_product_selections ORDER BY id",
        "story_runs": "SELECT id, project_id, topic_candidate_id, project_product_selection_id, product_asset_version_id, run_number, mode FROM story_runs ORDER BY id",
        "story_run_states": "SELECT story_run_id, current_stage, status, stage_data FROM story_run_states ORDER BY story_run_id",
        "commerce_workflow_links": "SELECT workflow_run_id, story_run_id FROM commerce_workflow_links ORDER BY workflow_run_id",
        "workflow_steps": "SELECT id, workflow_run_id, step_key, position, status, attempt, input_payload, idempotency_key FROM workflow_steps ORDER BY id",
        "commerce_workflow_steps": "SELECT workflow_step_id, workflow_run_id, story_run_id, stage, attempt, status FROM commerce_workflow_steps ORDER BY workflow_step_id",
    }
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            snapshot = {
                name: [dict(row._mapping) for row in connection.exec_driver_sql(sql).fetchall()]
                for name, sql in queries.items()
            }
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
            assert connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '%alembic_tmp%'"
            ).fetchall() == []
            return snapshot
    finally:
        migration_engine.dispose()


def test_nonempty_0014_0015_0016_round_trips_preserve_real_phase12_graph_and_foreign_keys(tmp_path) -> None:
    """场景 #24：真实 Project/产品/StoryRun/Commerce workflow 图历经迁移往返不变。"""

    database_url = f"sqlite:///{tmp_path / 'nonempty-commerce-roundtrip.db'}"
    _migration_runner(database_url, "0014_commerce_phase2_legacy_compatibility")
    ids = _insert_nonempty_phase12_graph(database_url)
    baseline = _phase12_snapshot(database_url)
    assert baseline["projects"] == [{"id": ids["project_id"], "title": "非空 Commerce Phase1/2", "description": "迁移完整性夹具"}]
    assert baseline["commerce_workflow_links"] == [{"workflow_run_id": ids["parent_run_id"], "story_run_id": ids["story_run_id"]}]
    assert baseline["commerce_workflow_steps"] == [{
        "workflow_step_id": ids["step_id"], "workflow_run_id": ids["parent_run_id"],
        "story_run_id": ids["story_run_id"], "stage": "OUTLINE", "attempt": 1, "status": "PENDING",
    }]

    _migration_runner(database_url, "0015_commerce_phase3_knowledge_generation_scaffolding")
    assert _phase12_snapshot(database_url) == baseline
    _migration_runner(database_url, "0014_commerce_phase2_legacy_compatibility", downgrade=True)
    assert _phase12_snapshot(database_url) == baseline
    _migration_runner(database_url, "0015_commerce_phase3_knowledge_generation_scaffolding")
    assert _phase12_snapshot(database_url) == baseline

    _migration_runner(database_url, "0016_commerce_phase3_integrity_hardening")
    assert _phase12_snapshot(database_url) == baseline
    _migration_runner(database_url, "0015_commerce_phase3_knowledge_generation_scaffolding", downgrade=True)
    assert _phase12_snapshot(database_url) == baseline
    _migration_runner(database_url, "0016_commerce_phase3_integrity_hardening")
    assert _phase12_snapshot(database_url) == baseline
