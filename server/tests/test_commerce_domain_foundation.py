"""带货短剧工作流第一阶段：领域模型、约束和定义初始化测试。"""

from pathlib import Path
from typing import Optional
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError

from app.core.database import Base, SessionLocal, engine
from app.models import (
    AssetKind,
    ChapterPlan,
    DialogueLine,
    MediaAsset,
    OutlineVersionStatus,
    ProductAnalysisStatus,
    ProductAnalysisVersion,
    ProductAsset,
    ProductAssetVersion,
    ProductAssetVersionStatus,
    ProductPlacementMethod,
    ProductPlacementPlan,
    ProductPlacementStrength,
    Project,
    ProjectProductSelection,
    RenderBatch,
    RenderBatchStatus,
    RunStatus,
    ScriptAnalysisStatus,
    ScriptAnalysisVersion,
    ScriptAsset,
    SegmentPlanStatus,
    StoryOutlineVersion,
    StoryRun,
    StoryRunMode,
    StoryRunStage,
    StoryRunState,
    StoryRunStatus,
    SubShotPlan,
    TopicCandidate,
    VideoSegmentPlan,
    WorkflowDefinition,
    WorkflowRun,
)
from app.services.commerce_configuration_service import (
    COMMERCE_WORKFLOW_CODE,
    COMMERCE_WORKFLOW_VERSION,
    ensure_commerce_foundation,
)
from app.services.commerce_domain_service import (
    CommerceDomainValidationError,
    add_sub_shot_plan,
    validate_sub_shot_within_segment,
)
from app.services.v1_configuration_service import V1_WORKFLOW_CODE, ensure_v1_foundation


def _flush(db) -> None:
    """统一处理测试中的一次 flush，令约束错误精确暴露在断言处。"""

    db.flush()


def _commerce_context(db, *, suffix: Optional[str] = None):
    """创建不依赖接口/模型调用的最小 Commerce 项目、选题和冻结产品版本。"""

    suffix = suffix or uuid4().hex[:8]
    project = Project(title=f"带货领域测试 {suffix}")
    db.add(project)
    _flush(db)
    workflow_run = WorkflowRun(project_id=project.id, workflow_key=f"legacy_topic_seed_{suffix}", status=RunStatus.SUCCEEDED)
    db.add(workflow_run)
    _flush(db)
    topic = TopicCandidate(
        project_id=project.id,
        generation_run_id=workflow_run.id,
        position=1,
        title="家庭冲突中的产品体验",
        opening_hook="开头三秒制造反差",
        synopsis="用于 Commerce 领域建模验证的原创选题。",
    )
    db.add(topic)

    product = ProductAsset(name=f"测试产品 {suffix}", description="共享产品主体")
    db.add(product)
    _flush(db)
    analysis = ProductAnalysisVersion(
        product_asset_id=product.id,
        version=1,
        raw_analysis={"source": "fixture"},
        analysis_status=ProductAnalysisStatus.SUCCEEDED,
    )
    db.add(analysis)
    _flush(db)
    product_version = ProductAssetVersion(
        product_asset_id=product.id,
        source_analysis_version_id=analysis.id,
        version=1,
        product_name="测试产品生产版本",
        appearance_description="白色瓶身，蓝色标签",
        selling_points=[{"name": "易用"}],
        user_pain_points=[{"name": "时间不足"}],
        usage_scenarios=[{"name": "家庭厨房"}],
        package_ocr={"brand": "测试品牌"},
        reference_images=[{"view": "front", "url": "https://example.invalid/product.png"}],
        status=ProductAssetVersionStatus.CONFIRMED,
    )
    db.add(product_version)
    _flush(db)
    selection = ProjectProductSelection(
        project_id=project.id,
        product_asset_id=product.id,
        product_asset_version_id=product_version.id,
    )
    db.add(selection)
    _flush(db)
    return project, workflow_run, topic, product, analysis, product_version, selection


def _story_run(db, *, project, topic, product_version, selection, run_number: int = 1, stage=StoryRunStage.TOPIC):
    """创建一个具备独立状态机的 StoryRun。"""

    story_run = StoryRun(
        project_id=project.id,
        topic_candidate_id=topic.id,
        project_product_selection_id=selection.id,
        product_asset_version_id=product_version.id,
        run_number=run_number,
        mode=StoryRunMode.STEPWISE,
    )
    db.add(story_run)
    _flush(db)
    state = StoryRunState(
        story_run_id=story_run.id,
        current_stage=stage,
        status=StoryRunStatus.RUNNING,
        stage_data={"created_for": "test"},
    )
    db.add(state)
    _flush(db)
    return story_run


def _outline_and_chapter(db, story_run):
    """为片段、子镜头和植入测试创建最小大纲与章节。"""

    outline = StoryOutlineVersion(
        story_run_id=story_run.id,
        version=1,
        title="带货剧情大纲",
        premise="产品解决人物冲突中的真实痛点。",
        story_beats=[{"beat": "冲突"}],
        product_placement_strategy={"principle": "先剧情后产品"},
        status=OutlineVersionStatus.LOCKED,
    )
    db.add(outline)
    _flush(db)
    chapter = ChapterPlan(
        story_run_id=story_run.id,
        outline_version_id=outline.id,
        chapter_number=1,
        title="第一章",
        narrative_purpose="建立人物痛点",
        content_summary="角色因为问题陷入困境。",
        product_plan={"placement": "soft_prop"},
    )
    db.add(chapter)
    _flush(db)
    return outline, chapter


@pytest.fixture()
def commerce_db():
    """复用测试 SQLite，显式建表以便不依赖 HTTP 生命周期。"""

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def test_commerce_workflow_definition_is_idempotent_and_coexists_with_v1(commerce_db) -> None:
    """新定义只按 code + version 新增，不覆盖 LemonFlow V1。"""

    v1 = ensure_v1_foundation(commerce_db)
    first = ensure_commerce_foundation(commerce_db)
    second = ensure_commerce_foundation(commerce_db)

    assert first.id == second.id
    assert v1.workflow_code == V1_WORKFLOW_CODE
    assert first.workflow_code == COMMERCE_WORKFLOW_CODE
    assert first.version == COMMERCE_WORKFLOW_VERSION
    assert [node["key"] for node in first.definition_json["nodes"]] == [
        "TOPIC",
        "OUTLINE",
        "CHAPTERS",
        "STORYBOARD",
        "VISUAL_ASSETS",
        "VIDEO_PROMPTS",
        "SEGMENT_RENDER",
    ]
    assert (
        commerce_db.scalars(
            select(WorkflowDefinition).where(WorkflowDefinition.workflow_code == V1_WORKFLOW_CODE)
        ).first()
        is not None
    )


def test_multiple_topics_and_reruns_have_independent_story_runs_and_product_versions(commerce_db) -> None:
    """同一项目可并行运行多个选题，同一选题也能以新的 run_number 重做。"""

    project, workflow_run, topic_one, product, analysis, product_v1, selection_v1 = _commerce_context(commerce_db)
    topic_two = TopicCandidate(
        project_id=project.id,
        generation_run_id=workflow_run.id,
        position=2,
        title="第二个带货选题",
        opening_hook="第二种开头",
        synopsis="与第一个选题无关。",
    )
    product_v2 = ProductAssetVersion(
        product_asset_id=product.id,
        source_analysis_version_id=analysis.id,
        version=2,
        product_name="测试产品生产版本 v2",
        appearance_description="新版包装",
        status=ProductAssetVersionStatus.CONFIRMED,
    )
    db = commerce_db
    db.add_all([topic_two, product_v2])
    _flush(db)
    selection_v2 = ProjectProductSelection(
        project_id=project.id,
        product_asset_id=product.id,
        product_asset_version_id=product_v2.id,
    )
    db.add(selection_v2)
    _flush(db)

    first = _story_run(
        db,
        project=project,
        topic=topic_one,
        product_version=product_v1,
        selection=selection_v1,
        stage=StoryRunStage.OUTLINE,
    )
    second = _story_run(
        db,
        project=project,
        topic=topic_two,
        product_version=product_v2,
        selection=selection_v2,
        stage=StoryRunStage.VIDEO_PROMPTS,
    )
    rerun = _story_run(
        db,
        project=project,
        topic=topic_one,
        product_version=product_v2,
        selection=selection_v2,
        run_number=2,
        stage=StoryRunStage.CHAPTERS,
    )
    db.commit()

    assert first.product_asset_version_id == product_v1.id
    assert second.product_asset_version_id == product_v2.id
    assert first.state.current_stage == StoryRunStage.OUTLINE
    assert second.state.current_stage == StoryRunStage.VIDEO_PROMPTS
    assert rerun.run_number == 2

    duplicate = StoryRun(
        project_id=project.id,
        topic_candidate_id=topic_one.id,
        project_product_selection_id=selection_v1.id,
        product_asset_version_id=product_v1.id,
        run_number=1,
    )
    db.add(duplicate)
    with pytest.raises(IntegrityError):
        db.commit()


def test_script_and_product_versions_are_append_only_and_unique(commerce_db) -> None:
    """同一脚本和产品可保存多版本，重复版本号由数据库拒绝。"""

    db = commerce_db
    project, _, _, product, _, _, _ = _commerce_context(db)
    media = MediaAsset(
        project_id=project.id,
        kind=AssetKind.SOURCE_VIDEO,
        original_filename="script.mp4",
        content_type="video/mp4",
        byte_size=1024,
        storage_key=f"commerce/{uuid4().hex}/script.mp4",
    )
    db.add(media)
    _flush(db)
    script = ScriptAsset(project_id=project.id, media_asset_id=media.id, name="脚本素材")
    db.add(script)
    _flush(db)
    db.add_all(
        [
            ScriptAnalysisVersion(
                script_asset_id=script.id,
                version=1,
                timeline_transcript=[{"start_ms": 0, "text": "开场"}],
                story_beats=[{"name": "hook"}],
                analysis_status=ScriptAnalysisStatus.SUCCEEDED,
            ),
            ScriptAnalysisVersion(
                script_asset_id=script.id,
                version=2,
                timeline_transcript=[{"start_ms": 0, "text": "第二版"}],
                analysis_status=ScriptAnalysisStatus.SUCCEEDED,
            ),
            ProductAssetVersion(
                product_asset_id=product.id,
                version=2,
                product_name="另一个冻结产品版本",
                appearance_description="透明包装",
                status=ProductAssetVersionStatus.CONFIRMED,
            ),
        ]
    )
    db.commit()
    assert [item.version for item in script.analyses] == [1, 2]
    assert [item.version for item in product.versions] == [1, 2]

    db.add(ScriptAnalysisVersion(script_asset_id=script.id, version=2))
    with pytest.raises(IntegrityError):
        db.commit()


def test_outline_chapter_segment_sub_shot_dialogue_and_placement_constraints(commerce_db) -> None:
    """片段、子镜头与植入使用可查询关系和数据库/领域双重时间约束。"""

    db = commerce_db
    project, _, topic, _, _, product_version, selection = _commerce_context(db)
    story_run = _story_run(
        db,
        project=project,
        topic=topic,
        product_version=product_version,
        selection=selection,
    )
    outline, chapter = _outline_and_chapter(db, story_run)
    four_seconds = VideoSegmentPlan(
        story_run_id=story_run.id,
        chapter_id=chapter.id,
        segment_number=1,
        target_duration_ms=4000,
        narrative_target="用反差建立痛点",
        status=SegmentPlanStatus.READY,
        video_prompt_version="v1",
    )
    fifteen_seconds = VideoSegmentPlan(
        story_run_id=story_run.id,
        chapter_id=chapter.id,
        segment_number=2,
        target_duration_ms=15000,
        narrative_target="完整体验演示",
        status=SegmentPlanStatus.READY,
    )
    db.add_all([four_seconds, fifteen_seconds])
    _flush(db)
    sub_shot = add_sub_shot_plan(
        db,
        four_seconds,
        shot_number=1,
        start_ms=0,
        end_ms=4000,
        action="人物拿起产品",
        emotion="释然",
        shot_scale="中景",
        camera_move="推镜",
        lighting="晨光",
        visual_description="厨房内的产品体验画面",
    )
    _flush(db)
    dialogue = DialogueLine(
        sub_shot_id=sub_shot.id,
        speaker="女主",
        dialogue="终于不用再手忙脚乱了。",
        start_ms=500,
        end_ms=2000,
    )
    placement = ProductPlacementPlan(
        story_run_id=story_run.id,
        product_asset_version_id=product_version.id,
        sub_shot_id=sub_shot.id,
        placement_method=ProductPlacementMethod.EXPERIENCE_DEMO,
        placement_strength=ProductPlacementStrength.MEDIUM,
        pain_point_trigger="准备晚餐时间不足",
        product_action="一键开启产品",
        ad_entry_point="冲突升级后",
        story_recovery_point="问题得到解决后回归家庭对话",
        planned_duration_ms=2500,
    )
    db.add_all([dialogue, placement])
    db.commit()
    assert sub_shot.end_ms == four_seconds.target_duration_ms
    assert dialogue.sub_shot_id == sub_shot.id
    assert placement.placement_method == ProductPlacementMethod.EXPERIENCE_DEMO

    with pytest.raises(CommerceDomainValidationError):
        validate_sub_shot_within_segment(four_seconds, start_ms=100, end_ms=4001)

    # 章节顺序、大纲版本、片段顺序和子镜头顺序均由数据库唯一约束保护。
    db.add(StoryOutlineVersion(story_run_id=story_run.id, version=1, title="重复", premise="重复"))
    with pytest.raises(IntegrityError):
        db.commit()


@pytest.mark.parametrize("duration_ms", [3999, 15001])
def test_video_segment_duration_database_range(duration_ms: int, commerce_db) -> None:
    """4 秒和 15 秒是合法边界，越界目标时长由数据库 CHECK 拒绝。"""

    db = commerce_db
    project, _, topic, _, _, product_version, selection = _commerce_context(db)
    story_run = _story_run(db, project=project, topic=topic, product_version=product_version, selection=selection)
    _, chapter = _outline_and_chapter(db, story_run)
    db.add(
        VideoSegmentPlan(
            story_run_id=story_run.id,
            chapter_id=chapter.id,
            segment_number=1,
            target_duration_ms=duration_ms,
            narrative_target="越界片段",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


@pytest.mark.parametrize(
    ("start_ms", "end_ms"),
    [(-1, 100), (100, 100), (100, 15001)],
)
def test_sub_shot_database_time_constraints(start_ms: int, end_ms: int, commerce_db) -> None:
    """子镜头的基础时间范围属于单表规则，直接由数据库拒绝。"""

    db = commerce_db
    project, _, topic, _, _, product_version, selection = _commerce_context(db)
    story_run = _story_run(db, project=project, topic=topic, product_version=product_version, selection=selection)
    _, chapter = _outline_and_chapter(db, story_run)
    segment = VideoSegmentPlan(
        story_run_id=story_run.id,
        chapter_id=chapter.id,
        segment_number=1,
        target_duration_ms=15000,
        narrative_target="子镜头约束",
    )
    db.add(segment)
    _flush(db)
    db.add(
        SubShotPlan(
            video_segment_id=segment.id,
            shot_number=1,
            start_ms=start_ms,
            end_ms=end_ms,
            action="动作",
            emotion="情绪",
            shot_scale="近景",
            camera_move="固定",
            lighting="自然光",
            visual_description="画面",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_render_batch_reuses_existing_workflow_run(commerce_db) -> None:
    """批次只聚合现有工作流任务，不创建第二套调度实体。"""

    db = commerce_db
    project, workflow_run, topic, _, _, product_version, selection = _commerce_context(db)
    story_run = _story_run(db, project=project, topic=topic, product_version=product_version, selection=selection)
    batch = RenderBatch(
        story_run_id=story_run.id,
        workflow_run_id=workflow_run.id,
        batch_number=1,
        status=RenderBatchStatus.PENDING,
        total_tasks=3,
        model_config_snapshot={"slot": "VIDEO_GENERATE"},
        generation_parameters_snapshot={"resolution": "720p"},
        estimated_cost=3.5,
        currency="CNY",
    )
    db.add(batch)
    db.commit()
    assert batch.workflow_run_id == workflow_run.id
    assert batch.total_tasks == 3


def test_alembic_commerce_upgrade_downgrade_and_reupgrade(tmp_path) -> None:
    """验证 0010 → 0011 → 0010 → 0011，且最终迁移链只有一个 head。"""

    server_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "commerce-migration.db"
    migration_url = f"sqlite:///{database_path}"

    def run_revision(action: str, revision: str) -> None:
        config = Config(str(server_root / "alembic.ini"))
        config.set_main_option("script_location", str(server_root / "migrations"))
        migration_engine = create_engine(migration_url)
        try:
            with migration_engine.begin() as connection:
                config.attributes["connection"] = connection
                getattr(command, action)(config, revision)
        finally:
            migration_engine.dispose()

    config = Config(str(server_root / "alembic.ini"))
    config.set_main_option("script_location", str(server_root / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == ["0011_commerce_domain_integrity_fixes"]

    run_revision("upgrade", "0010_commerce_domain_foundation")
    run_revision("upgrade", "head")
    migration_engine = create_engine(migration_url)
    try:
        assert {"script_assets", "story_runs", "video_segment_plans", "render_batches"}.issubset(
            inspect(migration_engine).get_table_names()
        )
    finally:
        migration_engine.dispose()

    run_revision("downgrade", "0010_commerce_domain_foundation")
    migration_engine = create_engine(migration_url)
    try:
        assert "story_runs" in inspect(migration_engine).get_table_names()
        assert "product_identification" not in {
            column["name"] for column in inspect(migration_engine).get_columns("product_analysis_versions")
        }
    finally:
        migration_engine.dispose()

    run_revision("upgrade", "head")
    migration_engine = create_engine(migration_url)
    try:
        assert "render_batches" in inspect(migration_engine).get_table_names()
        assert "product_identification" in {
            column["name"] for column in inspect(migration_engine).get_columns("product_analysis_versions")
        }
    finally:
        migration_engine.dispose()
