"""系统 Prompt 中心：版本、严格变量渲染与运行冻结的回归测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select

from app.core.database import SessionLocal
from app.main import app
from app.models import (
    ModelInvocation,
    ModelProfile,
    ModelSlot,
    Project,
    PromptTemplateDefinition,
    PromptTemplateVersion,
    PromptTemplateVersionStatus,
)
from app.services.prompt_template_service import (
    SEEDS_BY_KEY,
    copy_prompt_draft,
    ensure_prompt_template_foundation,
    freeze_active_prompt,
    get_active_prompt_version,
    activate_prompt_version,
    publish_prompt_draft,
    validate_prompt_version_payload,
)


def _catalog(client: TestClient) -> dict:
    response = client.get("/api/v1/production/prompt-template-catalog")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert rows
    return next(item for item in rows if item["prompt_key"] == "v1.story_generate")


def test_prompt_foundation_is_idempotent_and_keeps_active_choice() -> None:
    with TestClient(app):
        db = SessionLocal()
        try:
            ensure_prompt_template_foundation(db)
            db.commit()
            before = db.scalar(select(PromptTemplateDefinition).where(PromptTemplateDefinition.prompt_key == "v1.story_generate"))
            assert before is not None and before.active_version_id
            # 在 Python 侧统计，保持 SQLite/PostgreSQL 测试路径一致。
            before_count = len(list(db.scalars(select(PromptTemplateVersion).where(PromptTemplateVersion.prompt_template_id == before.id))))
            ensure_prompt_template_foundation(db)
            db.commit()
            db.refresh(before)
            after_count = len(list(db.scalars(select(PromptTemplateVersion).where(PromptTemplateVersion.prompt_template_id == before.id))))
            assert before_count == after_count
        finally:
            db.close()


def test_prompt_catalog_draft_publish_activate_rollback_and_preview_are_safe() -> None:
    with TestClient(app) as client:
        original = _catalog(client)
        active = original["active_version"]
        draft_response = client.post(f"/api/v1/production/prompt-template-catalog/{original['prompt_key']}/drafts", json={})
        assert draft_response.status_code == 201, draft_response.text
        draft = draft_response.json()
        assert draft["status"] == "DRAFT"
        update = client.patch(
            f"/api/v1/production/prompt-template-versions/{draft['id']}",
            json={
                "system_template": "你是原创故事助手。",
                "user_template": "冻结分析：{locked_reference_analysis}",
                "change_summary": "测试：严格版本化。",
            },
        )
        assert update.status_code == 200, update.text
        published = client.post(f"/api/v1/production/prompt-template-versions/{draft['id']}/publish")
        assert published.status_code == 200, published.text
        assert published.json()["status"] == "PUBLISHED"
        activated = client.post(
            f"/api/v1/production/prompt-template-catalog/{original['prompt_key']}/versions/{draft['id']}/activate"
        )
        assert activated.status_code == 200, activated.text
        assert activated.json()["active_version_id"] == draft["id"]
        # 已发布版本不可覆盖，且显式回滚只改活动指针。
        assert client.patch(
            f"/api/v1/production/prompt-template-versions/{draft['id']}",
            json={"system_template": "不能覆盖", "user_template": "{locked_reference_analysis}", "change_summary": "x"},
        ).status_code == 409
        rollback = client.post(
            f"/api/v1/production/prompt-template-catalog/{original['prompt_key']}/versions/{active['id']}/activate"
        )
        assert rollback.status_code == 200
        assert rollback.json()["active_version_id"] == active["id"]
        preview = client.post(
            f"/api/v1/production/prompt-template-catalog/{original['prompt_key']}/render-preview",
            json={"version_id": draft["id"], "variables": {"locked_reference_analysis": {"brief": "仅用于原创"}}},
        )
        assert preview.status_code == 200, preview.text
        payload = preview.json()
        assert "<LEMONFLOW_INPUT name=\"locked_reference_analysis\">" in payload["rendered_user_template"]
        assert "api_key" not in str(payload["sanitized_variable_snapshot"]).casefold()


def test_prompt_rejects_unknown_complex_and_sensitive_variables_before_render() -> None:
    with TestClient(app) as client:
        row = _catalog(client)
        draft = client.post(f"/api/v1/production/prompt-template-catalog/{row['prompt_key']}/drafts", json={}).json()
        url = f"/api/v1/production/prompt-template-versions/{draft['id']}"
        base = {"system_template": "系统", "change_summary": "安全校验"}
        assert client.patch(url, json={**base, "user_template": "{unknown}"}).status_code == 422
        assert client.patch(url, json={**base, "user_template": "{locked_reference_analysis.title}"}).status_code == 422
        assert client.patch(url, json={**base, "user_template": "Authorization: Bearer value"}).status_code == 422
        assert client.post(
            f"/api/v1/production/prompt-template-catalog/{row['prompt_key']}/render-preview",
            json={"version_id": row["active_version_id"], "variables": {}},
        ).status_code == 422
        assert client.post(
            f"/api/v1/production/prompt-template-catalog/{row['prompt_key']}/render-preview",
            json={"version_id": row["active_version_id"], "variables": {"locked_reference_analysis": {"headers": {"authorization": "secret"}}}},
        ).status_code == 422


def test_prompt_output_contract_is_code_owned_and_preview_never_calls_model() -> None:
    """Prompt Center 不能以编辑模板的方式改写业务解析契约或触发模型。"""

    seed = SEEDS_BY_KEY["v1.story_generate"]
    with pytest.raises(HTTPException) as invalid_contract:
        validate_prompt_version_payload(
            definition=seed,
            system_template=seed.system_template,
            user_template=seed.user_template,
            allowed_variables=seed.allowed_variables,
            output_contract_key="ARBITRARY_BROWSER_SCHEMA",
        )
    assert invalid_contract.value.status_code == 422
    assert "输出契约" in str(invalid_contract.value.detail)

    # 预览 API 只调用本地渲染服务，不创建 WorkflowRun / ModelInvocation。
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            before_invocations = db.scalar(select(func.count(ModelInvocation.id)))
        finally:
            db.close()
        row = _catalog(client)
        preview = client.post(
            f"/api/v1/production/prompt-template-catalog/{row['prompt_key']}/render-preview",
            json={
                "version_id": row["active_version_id"],
                "variables": {"locked_reference_analysis": {"brief": "纯本地预览"}},
            },
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["prompt_key"] == "v1.story_generate"
        assert "纯本地预览" in preview.json()["rendered_user_template"]
        db = SessionLocal()
        try:
            assert db.scalar(select(func.count(ModelInvocation.id))) == before_invocations
        finally:
            db.close()


def test_freeze_keeps_old_published_version_when_active_pointer_changes() -> None:
    with TestClient(app):
        db = SessionLocal()
        try:
            _, active = get_active_prompt_version(db, "v1.story_generate")
            frozen = freeze_active_prompt(db, "v1.story_generate", {"locked_reference_analysis": {"id": "analysis-a"}})
            draft = copy_prompt_draft(db, prompt_key="v1.story_generate")
            draft.status = PromptTemplateVersionStatus.PUBLISHED
            db.commit()
            definition = db.get(PromptTemplateDefinition, draft.prompt_template_id)
            assert definition is not None
            definition.active_version_id = draft.id
            db.commit()
            assert frozen["prompt_version_id"] == active.id
            assert frozen["prompt_version"] == active.version
            assert frozen["rendered_prompt_hash"]
        finally:
            db.close()


def test_model_invocation_traces_immutable_prompt_version_pointer() -> None:
    """调用审计引用的是 PromptVersion，不再把新目录 ID 塞进 legacy 外键。"""

    with TestClient(app):
        db = SessionLocal()
        try:
            definition, active = get_active_prompt_version(db, "v1.story_generate")
            frozen = freeze_active_prompt(
                db,
                "v1.story_generate",
                {"locked_reference_analysis": {"id": "analysis-for-audit"}},
            )
            slot = db.scalar(select(ModelSlot).where(ModelSlot.slot_key == "STORY_GENERATE"))
            profile = db.scalar(select(ModelProfile).where(ModelProfile.step_key == "STORY_GENERATE"))
            assert slot is not None and profile is not None
            project = Project(title="Prompt 审计指针测试")
            db.add(project)
            db.flush()
            invocation = ModelInvocation(
                project_id=project.id,
                model_slot_id=slot.id,
                model_profile_id=profile.id,
                prompt_template_id=None,
                prompt_template_version_id=active.id,
                task_type="STORY_GENERATE",
                model_profile_snapshot={"id": profile.id},
                prompt_snapshot=frozen,
                input_snapshot={"test_only": True},
                idempotency_key=f"prompt-pointer:{project.id}",
            )
            db.add(invocation)
            db.commit()
            db.refresh(invocation)
            assert invocation.prompt_template_version_id == active.id
            assert invocation.prompt_template_id is None
            assert invocation.prompt_snapshot["prompt_template_id"] == definition.id
            assert invocation.prompt_snapshot["content_hash"] == active.content_hash
        finally:
            db.close()


def test_concurrent_drafts_have_unique_version_numbers() -> None:
    """两个独立会话并发复制时，唯一约束保证版本号不会重复。"""

    with TestClient(app):
        def create() -> int:
            db = SessionLocal()
            try:
                return copy_prompt_draft(db, prompt_key="v1.scene_design").version
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            versions = list(executor.map(lambda _: create(), range(2)))
        assert len(versions) == 2
        assert len(set(versions)) == 2


def test_concurrent_activation_leaves_one_complete_active_pointer() -> None:
    """并发激活只会切换目录中的单一活动指针，不会产生半完成状态。"""

    with TestClient(app):
        setup = SessionLocal()
        try:
            first = copy_prompt_draft(setup, prompt_key="v1.scene_design")
            second = copy_prompt_draft(setup, prompt_key="v1.scene_design")
            publish_prompt_draft(setup, version_id=first.id)
            publish_prompt_draft(setup, version_id=second.id)
            definition_id = first.prompt_template_id
        finally:
            setup.close()

        def activate(version_id: str) -> str:
            db = SessionLocal()
            try:
                return activate_prompt_version(
                    db,
                    prompt_key="v1.scene_design",
                    version_id=version_id,
                ).active_version_id
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(activate, [first.id, second.id]))

        db = SessionLocal()
        try:
            definition = db.get(PromptTemplateDefinition, definition_id)
            assert definition is not None
            assert definition.active_version_id in {first.id, second.id}
            current = db.get(PromptTemplateVersion, definition.active_version_id)
            assert current is not None
            assert current.status == PromptTemplateVersionStatus.PUBLISHED
            assert set(results).issubset({first.id, second.id})
        finally:
            db.close()


def test_0023_sqlite_upgrade_downgrade_and_reupgrade(tmp_path: Path) -> None:
    """0023 可在隔离 SQLite 从 0022 安全往返，且不会遗留临时表。"""

    server_root = Path(__file__).resolve().parents[1]
    database_url = f"sqlite:///{tmp_path / 'prompt-template-0023.db'}"
    config = Config(str(server_root / "alembic.ini"))
    config.set_main_option("script_location", str(server_root / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == ["0023_prompt_template_version_management"]

    def migrate(action: str, revision: str) -> None:
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()
                config.attributes["connection"] = connection
                getattr(command, action)(config, revision)
                config.attributes.pop("connection", None)
        finally:
            engine.dispose()

    migrate("upgrade", "0022_model_parameter_capabilities")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "prompt_template_definitions" not in inspector.get_table_names()
        assert "prompt_template_version_id" not in {
            column["name"] for column in inspector.get_columns("model_invocations")
        }
    finally:
        engine.dispose()

    migrate("upgrade", "head")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert {"prompt_template_definitions", "prompt_template_versions"}.issubset(inspector.get_table_names())
        assert "prompt_template_version_id" in {
            column["name"] for column in inspector.get_columns("model_invocations")
        }
        foreign_keys = inspector.get_foreign_keys("model_invocations")
        assert any(
            foreign_key["constrained_columns"] == ["prompt_template_version_id"]
            and foreign_key["referred_table"] == "prompt_template_versions"
            for foreign_key in foreign_keys
        )
    finally:
        engine.dispose()

    migrate("downgrade", "0022_model_parameter_capabilities")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "prompt_template_definitions" not in inspector.get_table_names()
        assert "prompt_template_versions" not in inspector.get_table_names()
        assert "prompt_template_version_id" not in {
            column["name"] for column in inspector.get_columns("model_invocations")
        }
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE name LIKE '%alembic_tmp%'"
            ).fetchall() == []
    finally:
        engine.dispose()

    migrate("upgrade", "head")
    engine = create_engine(database_url)
    try:
        assert "prompt_template_versions" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
