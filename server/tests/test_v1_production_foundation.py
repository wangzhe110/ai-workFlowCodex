"""LemonFlow V1 数据基础约束测试。"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.database import Base, SessionLocal, engine
from app.models import (
    ModelSelectionMode,
    ModelSlot,
    Project,
    ReferenceAnalysis,
    RunStatus,
    StoryGenerationBatch,
    StoryProposal,
    StoryProposalStatus,
    WorkflowRun,
)


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
