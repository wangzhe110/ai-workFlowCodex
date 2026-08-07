"""LemonFlow V1 数据基础约束测试。"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, SessionLocal, engine
from app.models import (
    ModelProfile,
    ModelSelectionMode,
    ModelSlot,
    ModelSlotProfileBinding,
    PromptTemplate,
    PromptTemplateStatus,
    Project,
    ReferenceAnalysis,
    RunStatus,
    StoryGenerationBatch,
    StoryProposal,
    StoryProposalStatus,
    WorkflowDefinition,
    WorkflowRun,
)
from app.services.v1_configuration_service import (
    V1_DEFAULT_PROMPT_VERSION,
    V1_MOCK_PROFILE_SPECS,
    V1_PROMPT_SEEDS,
    V1_SLOT_SPECS,
    ensure_v1_foundation,
)


def _foundation_counts(db) -> dict[str, int]:
    """返回初始化种子相关表的行数，供幂等性断言使用。"""

    return {
        "workflow_definitions": len(db.scalars(select(WorkflowDefinition)).all()),
        "prompt_templates": len(db.scalars(select(PromptTemplate)).all()),
        "model_slots": len(db.scalars(select(ModelSlot)).all()),
        "model_profiles": len(db.scalars(select(ModelProfile).where(ModelProfile.adapter_key == "mock_v1")).all()),
        "slot_profile_bindings": len(db.scalars(select(ModelSlotProfileBinding)).all()),
    }


def test_v1_production_tables_are_registered() -> None:
    """新生产链路表必须进入 ORM 元数据，供迁移和本地 auto 模式创建。"""

    expected_tables = {
        "workflow_definitions",
        "project_production_states",
        "reference_analyses",
        "story_generation_batches",
        "story_proposals",
        "character_definitions",
        "scene_definitions",
        "character_reference_images",
        "scene_reference_images",
        "shot_plans",
        "shot_keyframes",
        "model_slots",
        "prompt_templates",
        "model_invocations",
        "model_quality_evaluations",
        "video_clip_asset_bindings",
    }
    assert expected_tables.issubset(Base.metadata.tables)


def test_ensure_v1_foundation_is_idempotent_for_all_seed_data() -> None:
    """重复打开模型中心不会重复写 Workflow、Prompt、槽位或模拟绑定。"""

    isolated_engine = create_engine("sqlite+pysqlite:///:memory:")
    isolated_session_factory = sessionmaker(bind=isolated_engine)
    Base.metadata.create_all(bind=isolated_engine)
    db = isolated_session_factory()
    try:
        first_definition = ensure_v1_foundation(db)
        first_counts = _foundation_counts(db)

        assert first_counts == {
            "workflow_definitions": 1,
            "prompt_templates": len(V1_PROMPT_SEEDS),
            "model_slots": len(V1_SLOT_SPECS),
            "model_profiles": len(V1_MOCK_PROFILE_SPECS),
            "slot_profile_bindings": len(V1_MOCK_PROFILE_SPECS),
        }
        assert all(
            item.version == V1_DEFAULT_PROMPT_VERSION
            for item in db.scalars(select(PromptTemplate)).all()
        )

        second_definition = ensure_v1_foundation(db)

        assert second_definition.id == first_definition.id
        assert _foundation_counts(db) == first_counts
        for task_type, name, _content in V1_PROMPT_SEEDS:
            assert len(
                db.scalars(
                    select(PromptTemplate).where(
                        PromptTemplate.task_type == task_type,
                        PromptTemplate.name == name,
                        PromptTemplate.version == V1_DEFAULT_PROMPT_VERSION,
                    )
                ).all()
            ) == 1

        # 复现线上历史状态：默认模板已经存在，但没有任何 ACTIVE Prompt。初始化仍然
        # 必须按唯一业务键跳过创建，不能新增版本、更不能修改该历史模板状态。
        archived_default = db.scalars(
            select(PromptTemplate).where(
                PromptTemplate.task_type == "VIDEO_ANALYSIS",
                PromptTemplate.name == "V1 默认视频分析",
                PromptTemplate.version == V1_DEFAULT_PROMPT_VERSION,
            )
        ).one()
        archived_default.status = PromptTemplateStatus.ARCHIVED
        db.commit()

        ensure_v1_foundation(db)

        assert _foundation_counts(db) == first_counts
        assert db.get(PromptTemplate, archived_default.id).status == PromptTemplateStatus.ARCHIVED
    finally:
        db.close()
        isolated_engine.dispose()


def test_story_selection_is_unique_per_generation_batch() -> None:
    """同一并行批次只能选择一份故事，但历史批次允许保留已选版本。"""

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        project = Project(title="V1 多模型故事测试", description=None)
        run = WorkflowRun(project=project, workflow_key="v1_story_generation", status=RunStatus.SUCCEEDED)
        db.add_all([project, run])
        db.flush()
        analysis = ReferenceAnalysis(
            project_id=project.id,
            workflow_run_id=run.id,
            version=1,
            video_script_structure={"theme": "测试"},
            opening_analysis={"hook": "测试"},
            viral_elements=[],
            scene_analysis=[],
            creative_brief={"direction": "原创"},
            generation_status=RunStatus.SUCCEEDED,
        )
        db.add(analysis)
        db.flush()
        batch = StoryGenerationBatch(
            project_id=project.id,
            reference_analysis_id=analysis.id,
            workflow_run_id=run.id,
            request_snapshot={},
            status=RunStatus.SUCCEEDED,
        )
        db.add(batch)
        db.flush()
        first = StoryProposal(
            batch_id=batch.id,
            project_id=project.id,
            candidate_number=1,
            content={"title": "方案一"},
            status=StoryProposalStatus.SELECTED,
        )
        db.add(first)
        db.commit()

        second = StoryProposal(
            batch_id=batch.id,
            project_id=project.id,
            candidate_number=2,
            content={"title": "方案二"},
            status=StoryProposalStatus.SELECTED,
        )
        db.add(second)
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_story_slot_can_be_configured_for_parallel_models() -> None:
    """模型槽位策略而非具体模型名称决定是否允许并行故事生成。"""

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        slot = ModelSlot(
            slot_key=f"STORY_GENERATE_TEST_{datetime.now(timezone.utc).timestamp()}",
            capability="STORY_GENERATE",
            selection_mode=ModelSelectionMode.MULTI_PARALLEL,
            description="测试故事模型并行策略",
        )
        db.add(slot)
        db.commit()
        assert slot.selection_mode == ModelSelectionMode.MULTI_PARALLEL
    finally:
        db.close()
