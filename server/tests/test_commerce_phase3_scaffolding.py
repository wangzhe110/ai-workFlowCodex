"""Commerce Phase 3：知识库、Provider 边界、权限计量和迁移回归。"""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine, func, inspect, select
from sqlalchemy.exc import IntegrityError

from app.core.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    GenerationInvocation,
    GenerationModality,
    GenerationTask,
    KnowledgeChunk,
    KnowledgeResourceType,
    Project,
    ProjectMemberRole,
    RunStatus,
    UsageEvent,
    UsageEventKind,
    ViralKnowledgeStatus,
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
    generation_provider_registry,
    get_generation_task,
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
    retriever_registry.register(FakeInMemoryRetriever())
    yield
    generation_provider_registry._providers.clear()
    retriever_registry._providers.clear()
    retriever_registry.register(FakeInMemoryRetriever())


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
    """场景 #8/#21：未配置/异常 Provider 返回 503，不降级为假成功或真实网络请求。"""

    project = _project(phase3_db)
    with pytest.raises(HTTPException) as missing:
        retrieve(phase3_db, provider_key="not-configured", query=RetrievalQuery(project_id=project.id, query_text="x"))
    assert missing.value.status_code == 503
    retriever_registry.register(FakeInMemoryRetriever(error=RuntimeError("boom")))
    with pytest.raises(HTTPException) as error:
        retrieve(phase3_db, provider_key="fake_in_memory", query=RetrievalQuery(project_id=project.id, query_text="x"))
    assert error.value.status_code == 503
    assert phase3_db.scalar(select(func.count()).select_from(__import__("app.models", fromlist=["RetrievalCall"]).RetrievalCall).where(__import__("app.models", fromlist=["RetrievalCall"]).RetrievalCall.status == RunStatus.FAILED)) >= 1


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
    protected = apply_generation_callback(
        phase3_db, project_id=project.id, task_id=video.id, provider_key="async", provider_task_id=video.provider_task_id,
        result=GenerationResult(status=RunStatus.FAILED, error_code="late"),
    )
    assert protected.status == RunStatus.SUCCEEDED
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


def test_generation_rejects_secrets_and_missing_provider_without_external_call(phase3_db) -> None:
    """场景 #21：未接真实 Adapter 时严格 503；参数中疑似密钥也不能入库。"""

    project = _project(phase3_db)
    before = phase3_db.scalar(select(func.count()).select_from(GenerationTask))
    with pytest.raises(HTTPException) as secret:
        submit_generation(phase3_db, request=GenerationRequest(project_id=project.id, modality=GenerationModality.IMAGE, capability="image_generate", model_key="x", parameters={"api_key": "bait"}, preferred_provider="real"))
    assert secret.value.status_code == 422
    with pytest.raises(HTTPException) as missing:
        submit_generation(phase3_db, request=GenerationRequest(project_id=project.id, modality=GenerationModality.IMAGE, capability="image_generate", model_key="x", parameters={}, preferred_provider="real"))
    assert missing.value.status_code == 503
    # 未注册 Provider 不会调用网络或真实适配器，也不会伪造已提交任务。
    assert phase3_db.scalar(select(func.count()).select_from(GenerationTask)) == before


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


def test_0015_empty_sqlite_upgrade_downgrade_and_reupgrade(tmp_path) -> None:
    """场景 #23：空库往返仅创建/删除 0015 自己的表，迁移链仍是唯一 head。"""

    database_url = f"sqlite:///{tmp_path / 'empty-0015.db'}"
    server_root = Path(__file__).resolve().parents[1]
    config = Config(str(server_root / "alembic.ini"))
    config.set_main_option("script_location", str(server_root / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == ["0015_commerce_phase3_knowledge_generation_scaffolding"]
    _migration_runner(database_url, "head")
    migration_engine = create_engine(database_url)
    try:
        tables = set(inspect(migration_engine).get_table_names())
        assert {"viral_cases", "viral_patterns", "knowledge_chunks", "retrieval_calls", "generation_tasks", "generation_invocations", "project_members", "saas_plans", "project_subscriptions", "usage_events"}.issubset(tables)
    finally:
        migration_engine.dispose()
    _migration_runner(database_url, "0014_commerce_phase2_legacy_compatibility", downgrade=True)
    _migration_runner(database_url, "head")


def test_0015_nonempty_0014_round_trip_preserves_existing_phase_data_and_foreign_keys(tmp_path) -> None:
    """场景 #24：非空 Phase 1/2 数据经过 0014→0015→0014→0015 不丢失。"""

    database_url = f"sqlite:///{tmp_path / 'nonempty-0015.db'}"
    _migration_runner(database_url, "0014_commerce_phase2_legacy_compatibility")
    migration_engine = create_engine(database_url)
    project_id = str(uuid4())
    try:
        with migration_engine.begin() as connection:
            projects = MetaData()
            projects.reflect(bind=connection, only=["projects"])
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            connection.execute(projects.tables["projects"].insert().values(id=project_id, title="非空 Phase2", description=None, created_at=now, updated_at=now))
    finally:
        migration_engine.dispose()
    _migration_runner(database_url, "0015_commerce_phase3_knowledge_generation_scaffolding")
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.begin() as connection:
            meta = MetaData()
            meta.reflect(bind=connection, only=["viral_cases"])
            connection.execute(meta.tables["viral_cases"].insert().values(
                id=str(uuid4()), project_id=project_id, source_type="fixture", source_identifier="nonempty", title="迁移案例",
                raw_analysis={}, structured_analysis={}, tags=[], status="ACTIVE", created_at=now, updated_at=now,
            ))
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    finally:
        migration_engine.dispose()
    _migration_runner(database_url, "0014_commerce_phase2_legacy_compatibility", downgrade=True)
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT title FROM projects WHERE id = ?", (project_id,)).scalar_one() == "非空 Phase2"
            assert "viral_cases" not in inspect(connection).get_table_names()
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    finally:
        migration_engine.dispose()
    _migration_runner(database_url, "head")
