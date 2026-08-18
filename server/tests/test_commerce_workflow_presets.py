"""Commerce 业务预设与 StoryRun 配置冻结回归。

这些测试只解析已注册的 Mock Profile、Prompt 与数据库快照；不投递 Worker，
不调用任何供应商 Adapter，也不创建付费任务。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select

from app.core.database import SessionLocal
from app.main import app
from app.models import (
    CommerceStoryRunWorkflowConfig,
    CommerceWorkflowPresetVersion,
    ProductAsset,
    ProductAssetVersion,
    ProductAssetVersionStatus,
    Project,
    ProjectProductSelection,
    RunStatus,
    StoryRunStage,
    TopicCandidate,
    TopicStatus,
    WorkflowRun,
)
from app.services import commerce_workflow_service
from app.services.commerce_domain_service import create_project_product_selection
from app.services.commerce_workflow_preset_service import (
    MAX_ESTIMATED_SHOTS,
    _validate_aspect_ratio,
    copy_preset_draft,
    ensure_commerce_workflow_preset_foundation,
    get_preset_definition,
    resolve_story_run_workflow_config,
    validate_business_config,
)
from app.services.commerce_workflow_service import create_next_story_run


def _seed_story_run_inputs() -> tuple[str, str, str]:
    """创建最小 Commerce 归属图，不经过模型或 Worker。"""

    db = SessionLocal()
    try:
        project = Project(title=f"Workflow preset fixture {uuid4().hex[:8]}")
        db.add(project)
        db.flush()
        topic_run = WorkflowRun(
            project_id=project.id,
            workflow_key="fixture_topic",
            input_snapshot={},
            status=RunStatus.SUCCEEDED,
        )
        db.add(topic_run)
        db.flush()
        topic = TopicCandidate(
            project_id=project.id,
            generation_run_id=topic_run.id,
            position=1,
            title="预设冻结选题",
            opening_hook="冻结钩子",
            synopsis="冻结简介",
            status=TopicStatus.SELECTED,
        )
        product = ProductAsset(name="预设测试商品")
        db.add_all([topic, product])
        db.flush()
        product_version = ProductAssetVersion(
            product_asset_id=product.id,
            version=1,
            product_name="预设测试商品",
            appearance_description="测试包装",
            status=ProductAssetVersionStatus.CONFIRMED,
            frozen_at=datetime.now(timezone.utc),
        )
        db.add(product_version)
        db.flush()
        selection = create_project_product_selection(
            db,
            project_id=project.id,
            product_asset_id=product.id,
            product_asset_version_id=product_version.id,
        )
        db.commit()
        return project.id, topic.id, selection.id
    finally:
        db.close()


def test_preset_foundation_is_idempotent_and_standard_preserves_defaults() -> None:
    """三个内置预设只补缺失记录；standard 保持原默认的分步生产语义。"""

    with TestClient(app):
        db = SessionLocal()
        try:
            ensure_commerce_workflow_preset_foundation(db)
            first = list(db.scalars(select(CommerceWorkflowPresetVersion)).all())
            ensure_commerce_workflow_preset_foundation(db)
            second = list(db.scalars(select(CommerceWorkflowPresetVersion)).all())
            assert {item.id for item in first} == {item.id for item in second}
            standard = get_preset_definition(db, "standard")
            active = db.get(CommerceWorkflowPresetVersion, standard.active_version_id)
            assert active is not None
            assert active.version == 1
            assert active.status.value == "PUBLISHED"
            assert active.config["execution_mode"] == "STEPWISE"
            assert active.config["idea_candidate_count"] == 10
            assert active.config["target_duration_seconds"] == 30
            assert active.config["target_shot_duration_seconds"] == 5
        finally:
            db.close()


def test_preset_api_draft_publish_activate_and_rollback_without_json_editor() -> None:
    """Draft 可编辑；Published 不可覆盖；活动指针可回滚到旧 Published 版本。"""

    with TestClient(app) as client:
        presets = client.get("/api/v1/commerce/workflow-presets")
        assert presets.status_code == 200, presets.text
        standard = next(item for item in presets.json() if item["preset_key"] == "standard")
        original_id = standard["active_version_id"]
        draft = client.post("/api/v1/commerce/workflow-presets/standard/drafts", json={})
        assert draft.status_code == 201, draft.text
        draft_payload = draft.json()
        updated_config = deepcopy(standard["active_version"]["config"])
        updated_config["target_duration_seconds"] = 35
        updated = client.patch(
            f"/api/v1/commerce/workflow-preset-versions/{draft_payload['id']}",
            json={"config": updated_config, "change_summary": "测试 Draft 业务时长"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["status"] == "DRAFT"
        published = client.post(f"/api/v1/commerce/workflow-preset-versions/{draft_payload['id']}/publish")
        assert published.status_code == 200, published.text
        assert published.json()["status"] == "PUBLISHED"
        assert client.patch(
            f"/api/v1/commerce/workflow-preset-versions/{draft_payload['id']}",
            json={"config": updated_config, "change_summary": "不得覆盖"},
        ).status_code == 409
        activated = client.post(
            f"/api/v1/commerce/workflow-presets/standard/versions/{draft_payload['id']}/activate"
        )
        assert activated.status_code == 200, activated.text
        assert activated.json()["active_version_id"] == draft_payload["id"]
        rolled_back = client.post(
            f"/api/v1/commerce/workflow-presets/standard/versions/{original_id}/activate"
        )
        assert rolled_back.status_code == 200, rolled_back.text
        assert rolled_back.json()["active_version_id"] == original_id


def test_concurrent_preset_copy_allocates_distinct_versions() -> None:
    """并发复制不会覆盖版本号，SQLite 的短暂锁由服务内重试处理。"""

    with TestClient(app):
        barrier = Barrier(2)

        def copy_one() -> tuple[str, int]:
            db = SessionLocal()
            try:
                barrier.wait(timeout=5)
                item = copy_preset_draft(db, preset_key="preview")
                return item.id, item.version
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            copied = list(pool.map(lambda _item: copy_one(), range(2)))
    assert len({item[0] for item in copied}) == 2
    assert len({item[1] for item in copied}) == 2


@pytest.mark.parametrize(
    "override",
    [
        {"idea_candidate_count": 11},
        {"run_variant_count": 11},
        {"chapter_mode": "MANUAL", "chapter_count": None},
        {"chapter_mode": "AUTO", "chapter_count": 2},
        {"target_duration_seconds": 120, "target_shot_duration_seconds": 1},
        {"credential": "must-not-pass"},
        {"nested": {"headers": {"authorization": "must-not-pass"}}},
        {"visual_style": "https://example.invalid/not-allowed"},
    ],
)
def test_business_config_rejects_unsafe_or_invalid_overrides(override: dict) -> None:
    """候选/章节/数量上限和递归敏感字段都在模型调用前失败。"""

    with pytest.raises(HTTPException) as failure:
        validate_business_config({"schema_version": 1, **override})
    assert failure.value.status_code == 422


def test_aspect_ratio_requires_declared_real_video_capability() -> None:
    """非 Mock Profile 未声明画幅时在创建 Run 前失败，绝不静默改为默认画幅。"""

    with pytest.raises(HTTPException) as unsupported:
        _validate_aspect_ratio(
            {"aspect_ratio": "9:16"},
            {"VIDEO_GENERATE": [{"profile_snapshot": {"adapter_key": "volcengine_ark_video", "parameter_config": {}}}]},
        )
    assert unsupported.value.status_code == 422
    assert "画幅" in str(unsupported.value.detail)


def test_story_run_freezes_preset_model_prompt_and_effective_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run 创建后阶段快照只读取冻结行，不再查询活动槽位或活动 Prompt。"""

    with TestClient(app):
        project_id, topic_id, selection_id = _seed_story_run_inputs()
        db = SessionLocal()
        try:
            story_run = create_next_story_run(
                db,
                project_id=project_id,
                topic_candidate_id=topic_id,
                project_product_selection_id=selection_id,
                preset_key="standard",
                run_overrides={"target_duration_seconds": 20, "target_shot_duration_seconds": 5},
            )
            frozen = db.get(CommerceStoryRunWorkflowConfig, story_run.id)
            assert frozen is not None
            assert frozen.effective_workflow_config["target_duration_seconds"] == 20
            assert frozen.config_sources["target_duration_seconds"] == "run_override"
            assert frozen.estimates["estimated_shots"] == 4
            assert set(frozen.model_bindings) >= {"STORY_GENERATE", "VIDEO_GENERATE", "FINAL_COMPOSE"}
            assert set(frozen.prompt_templates) >= {"commerce.story_outline", "commerce.video_prompt_generate"}
            video = frozen.model_bindings["VIDEO_GENERATE"][0]["profile_snapshot"]["parameter_resolution"]
            assert "aspect_ratio" not in video["effective_parameters"]
            # 测试用 mock Profile 不声明视频 ratio；真实 Seedance Profile 的首帧
            # 省略审计由 test_model_parameter_capabilities 覆盖。无论哪一种，冻结
            # 后都不能把项目画幅作为供应商首帧参数发送。
            assert video["execution_context"]["input_mode"] == "first_frame"

            monkeypatch.setattr(
                commerce_workflow_service,
                "enabled_profiles_for_slot",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不得读取当前活动槽位")),
            )
            monkeypatch.setattr(
                commerce_workflow_service,
                "freeze_active_prompt",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不得读取当前活动 Prompt")),
            )
            snapshot = commerce_workflow_service._freeze_execution_snapshot(db, story_run, StoryRunStage.OUTLINE)
            assert snapshot["workflow_config"]["preset_version_id"] == frozen.preset_version_id
            assert snapshot["model_bindings"]["STORY_GENERATE"] == frozen.model_bindings["STORY_GENERATE"]
            prompt = snapshot["prompt_templates"]["STORY_GENERATE"]
            assert prompt["prompt_version_id"] == frozen.prompt_templates["commerce.story_outline"]["prompt_version_id"]
        finally:
            db.close()


def test_old_story_run_has_no_freeze_and_keeps_legacy_compatibility() -> None:
    """0024 不回填历史 Run；缺少冻结行仍明确走已有兼容路径。"""

    with TestClient(app):
        project_id, topic_id, selection_id = _seed_story_run_inputs()
        db = SessionLocal()
        try:
            # 该调用是本测试用于构造 0024 前历史记录的领域服务，不是新的客户端入口。
            from app.services.commerce_domain_service import create_story_run

            legacy = create_story_run(
                db,
                project_id=project_id,
                topic_candidate_id=topic_id,
                project_product_selection_id=selection_id,
                product_asset_version_id=db.get(ProjectProductSelection, selection_id).product_asset_version_id,
                run_number=1,
            )
            db.commit()
            assert legacy.workflow_config_freeze is None
        finally:
            db.close()


def test_0024_sqlite_upgrade_downgrade_and_reupgrade(tmp_path: Path) -> None:
    """空 SQLite 可 0023→0024→0023→0024 往返，不残留预设表。"""

    server_root = Path(__file__).resolve().parents[1]
    database_url = f"sqlite:///{tmp_path / 'commerce-workflow-presets.db'}"
    config = Config(str(server_root / "alembic.ini"))
    config.set_main_option("script_location", str(server_root / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == ["0025_model_lab"]

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

    migrate("upgrade", "0023_prompt_template_version_management")
    migrate("upgrade", "head")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert {
            "commerce_workflow_preset_definitions",
            "commerce_workflow_preset_versions",
            "commerce_story_run_workflow_configs",
        }.issubset(inspector.get_table_names())
    finally:
        engine.dispose()

    migrate("downgrade", "0023_prompt_template_version_management")
    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert "commerce_workflow_preset_definitions" not in tables
        assert "commerce_workflow_preset_versions" not in tables
        assert "commerce_story_run_workflow_configs" not in tables
    finally:
        engine.dispose()
    migrate("upgrade", "head")


def test_estimated_shots_are_capped_before_task_creation() -> None:
    """数量估算只能在配置层拒绝危险输入，绝不提前创建模型任务。"""

    with pytest.raises(HTTPException) as failure:
        validate_business_config(
            {
                "schema_version": 1,
                "target_duration_seconds": 120,
                "target_shot_duration_seconds": 1,
            }
        )
    assert failure.value.status_code == 422
    assert MAX_ESTIMATED_SHOTS == 24
