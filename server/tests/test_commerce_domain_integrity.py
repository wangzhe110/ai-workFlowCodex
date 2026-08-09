"""Commerce Phase 1 审核修复：删除策略、跨对象归属、时间与版本规则。"""

from copy import deepcopy
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.database import Base, SessionLocal, engine, init_database
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
    SceneMappingVersion,
    ScriptAnalysisStatus,
    ScriptAnalysisVersion,
    ScriptAsset,
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
)
from app.services.commerce_domain_service import (
    CommerceDomainValidationError,
    add_sub_shot_plan,
    create_chapter_plan,
    create_dialogue_line,
    create_next_product_asset_version,
    create_next_script_analysis_version,
    create_next_story_outline_version,
    create_project_product_selection,
    create_render_batch,
    create_scene_mapping_version,
    create_story_run,
    create_video_segment_plan,
    freeze_product_asset_version,
    transition_product_asset_version_status,
    transition_story_outline_version_status,
    update_product_asset_version,
    update_script_analysis_version,
    update_story_outline_version,
    validate_product_placement_bindings,
)
from app.services.v1_configuration_service import V1_WORKFLOW_CODE
from app.services import commerce_domain_service


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture()
def commerce_db():
    """每个测试使用同一隔离 SQLite 文件中的独立会话。"""

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _flush(db) -> None:
    db.flush()


def _project_with_topic(db, label: str):
    """创建一个项目及其一个可供 StoryRun 使用的旧选题候选。"""

    suffix = f"{label}-{uuid4().hex[:8]}"
    project = Project(title=f"Commerce 审核 {suffix}")
    db.add(project)
    _flush(db)
    workflow_run = WorkflowRun(
        project_id=project.id,
        workflow_key=f"commerce-topic-seed-{suffix}",
        status=RunStatus.SUCCEEDED,
    )
    db.add(workflow_run)
    _flush(db)
    topic = TopicCandidate(
        project_id=project.id,
        generation_run_id=workflow_run.id,
        position=1,
        title="带货剧情选题",
        opening_hook="强冲突开场",
        synopsis="用于 Commerce 归属校验的原创选题。",
    )
    db.add(topic)
    _flush(db)
    return project, workflow_run, topic


def _frozen_product_version(db, *, label: str, source_media_asset_id: Optional[str] = None):
    """创建共享产品、分析版本和允许生产引用的冻结产品版本。"""

    product = ProductAsset(name=f"产品 {label}-{uuid4().hex[:8]}")
    db.add(product)
    _flush(db)
    analysis = ProductAnalysisVersion(
        product_asset_id=product.id,
        source_media_asset_id=source_media_asset_id,
        version=1,
        product_identification={"name": "测试产品", "confidence": 0.95},
        package_ocr={"brand": "Lemon", "sku": "LF-1"},
        candidate_reference_images=[{"view": "front", "url": "https://example.invalid/front.png"}],
        appearance_description_candidates=[{"text": "蓝色瓶身"}],
        selling_point_candidates=[{"text": "快速清洁"}],
        user_pain_point_candidates=[{"text": "家务时间不足"}],
        usage_scenario_candidates=[{"text": "家庭厨房"}],
        raw_analysis={"provider": "mock"},
        analysis_status=ProductAnalysisStatus.SUCCEEDED,
    )
    db.add(analysis)
    _flush(db)
    version = ProductAssetVersion(
        product_asset_id=product.id,
        source_analysis_version_id=analysis.id,
        version=1,
        product_name="测试产品生产版本",
        appearance_description="蓝色瓶身",
        selling_points=[{"text": "快速清洁"}],
        user_pain_points=[{"text": "家务时间不足"}],
        usage_scenarios=[{"text": "家庭厨房"}],
        package_ocr={"brand": "Lemon"},
        reference_images=[{"view": "front", "url": "https://example.invalid/front.png"}],
        status=ProductAssetVersionStatus.CONFIRMED,
    )
    db.add(version)
    _flush(db)
    freeze_product_asset_version(db, product_asset_version_id=version.id)
    return product, analysis, version


def _run_with_graph(db, *, label: str):
    """创建一个合法 StoryRun、大纲、章节、片段和子镜头。"""

    project, workflow_run, topic = _project_with_topic(db, label)
    product, _, product_version = _frozen_product_version(db, label=label)
    selection = create_project_product_selection(
        db,
        project_id=project.id,
        product_asset_id=product.id,
        product_asset_version_id=product_version.id,
    )
    story_run = create_story_run(
        db,
        project_id=project.id,
        topic_candidate_id=topic.id,
        project_product_selection_id=selection.id,
        product_asset_version_id=product_version.id,
        run_number=1,
    )
    outline = create_next_story_outline_version(
        db,
        story_run_id=story_run.id,
        title="带货大纲",
        premise="产品在冲突中自然解决问题。",
    )
    chapter = create_chapter_plan(
        db,
        story_run_id=story_run.id,
        outline_version_id=outline.id,
        chapter_number=1,
        title="第一章",
        narrative_purpose="建立痛点",
        content_summary="角色面对问题。",
    )
    segment = create_video_segment_plan(
        db,
        story_run_id=story_run.id,
        chapter_id=chapter.id,
        segment_number=1,
        target_duration_ms=6000,
        narrative_target="体验产品解决问题",
    )
    sub_shot = add_sub_shot_plan(
        db,
        segment,
        shot_number=1,
        start_ms=1000,
        end_ms=4000,
        action="角色使用产品",
        emotion="释然",
        shot_scale="中景",
        camera_move="推镜",
        lighting="自然光",
        visual_description="家庭厨房体验画面",
    )
    return {
        "project": project,
        "workflow_run": workflow_run,
        "topic": topic,
        "product": product,
        "product_version": product_version,
        "selection": selection,
        "story_run": story_run,
        "outline": outline,
        "chapter": chapter,
        "segment": segment,
        "sub_shot": sub_shot,
    }


def _placement_fields(graph, **overrides):
    fields = {
        "story_run_id": graph["story_run"].id,
        "product_asset_version_id": graph["product_version"].id,
        "chapter_id": graph["chapter"].id,
        "video_segment_id": None,
        "sub_shot_id": None,
        "placement_method": ProductPlacementMethod.SOFT_PROP,
        "placement_strength": ProductPlacementStrength.LIGHT,
        "pain_point_trigger": "时间不足",
        "product_action": "拿起产品",
        "ad_entry_point": "冲突出现后",
        "story_recovery_point": "体验完成后",
        "planned_duration_ms": 0,
    }
    fields.update(overrides)
    return fields


def _validate_placement(db, fields) -> None:
    """测试中只传入领域校验所需的定位、版本和时长字段。"""

    validate_product_placement_bindings(
        db,
        story_run_id=fields["story_run_id"],
        product_asset_version_id=fields["product_asset_version_id"],
        chapter_id=fields.get("chapter_id"),
        video_segment_id=fields.get("video_segment_id"),
        sub_shot_id=fields.get("sub_shot_id"),
        planned_duration_ms=fields["planned_duration_ms"],
    )


def test_product_analysis_structured_fields_are_persisted_and_readable(commerce_db) -> None:
    """核心产品识别结果不再只藏在 raw_analysis 中。"""

    _, analysis, _ = _frozen_product_version(commerce_db, label="structured")
    commerce_db.commit()
    loaded = commerce_db.get(ProductAnalysisVersion, analysis.id)
    assert loaded is not None
    assert loaded.product_identification["name"] == "测试产品"
    assert loaded.package_ocr["brand"] == "Lemon"
    assert loaded.candidate_reference_images[0]["view"] == "front"
    assert loaded.appearance_description_candidates[0]["text"] == "蓝色瓶身"
    assert loaded.selling_point_candidates[0]["text"] == "快速清洁"
    assert loaded.user_pain_point_candidates[0]["text"] == "家务时间不足"
    assert loaded.usage_scenario_candidates[0]["text"] == "家庭厨房"


def test_source_project_delete_preserves_shared_product_and_nulls_media_reference(commerce_db) -> None:
    """来源项目删除不会被产品分析反向阻止，分析记录保留且来源置空。"""

    project = Project(title=f"来源项目 {uuid4().hex[:8]}")
    commerce_db.add(project)
    _flush(commerce_db)
    media = MediaAsset(
        project_id=project.id,
        kind=AssetKind.SOURCE_VIDEO,
        original_filename="product.mp4",
        content_type="video/mp4",
        byte_size=1024,
        storage_key=f"commerce/source/{uuid4().hex}.mp4",
    )
    commerce_db.add(media)
    _flush(commerce_db)
    product, analysis, product_version = _frozen_product_version(
        commerce_db,
        label="source-delete",
        source_media_asset_id=media.id,
    )
    product_id, analysis_id, version_id, media_id = product.id, analysis.id, product_version.id, media.id
    commerce_db.commit()

    commerce_db.delete(project)
    commerce_db.commit()
    # expire_on_commit=False 是服务默认配置；显式过期后读取真实数据库 ``SET NULL`` 结果。
    commerce_db.expire_all()
    assert commerce_db.get(Project, project.id) is None
    assert commerce_db.get(MediaAsset, media_id) is None
    assert commerce_db.get(ProductAsset, product_id) is not None
    retained_analysis = commerce_db.get(ProductAnalysisVersion, analysis_id)
    assert retained_analysis is not None and retained_analysis.source_media_asset_id is None
    assert commerce_db.get(ProductAssetVersion, version_id) is not None


def test_cross_project_script_selection_and_story_run_bindings_are_rejected(commerce_db) -> None:
    """脚本、产品选择和 StoryRun 不允许把其他项目数据串接进来。"""

    first_project, _, first_topic = _project_with_topic(commerce_db, "first")
    second_project, _, second_topic = _project_with_topic(commerce_db, "second")
    media = MediaAsset(
        project_id=second_project.id,
        kind=AssetKind.SOURCE_VIDEO,
        original_filename="other.mp4",
        content_type="video/mp4",
        byte_size=1,
        storage_key=f"commerce/other/{uuid4().hex}.mp4",
    )
    commerce_db.add(media)
    _flush(commerce_db)
    product_one, _, version_one = _frozen_product_version(commerce_db, label="one")
    product_two, _, version_two = _frozen_product_version(commerce_db, label="two")
    first_selection = create_project_product_selection(
        commerce_db,
        project_id=first_project.id,
        product_asset_id=product_one.id,
        product_asset_version_id=version_one.id,
    )
    second_selection = create_project_product_selection(
        commerce_db,
        project_id=second_project.id,
        product_asset_id=product_two.id,
        product_asset_version_id=version_two.id,
    )

    from app.services.commerce_domain_service import create_script_asset

    with pytest.raises(CommerceDomainValidationError) as script_error:
        create_script_asset(
            commerce_db,
            project_id=first_project.id,
            media_asset_id=media.id,
            name="错误脚本",
        )
    assert "本项目" in script_error.value.detail

    with pytest.raises(CommerceDomainValidationError):
        create_project_product_selection(
            commerce_db,
            project_id=first_project.id,
            product_asset_id=product_one.id,
            product_asset_version_id=version_two.id,
        )

    draft = ProductAssetVersion(
        product_asset_id=product_one.id,
        version=2,
        product_name="未确认版本",
        appearance_description="草稿",
        status=ProductAssetVersionStatus.DRAFT,
    )
    commerce_db.add(draft)
    _flush(commerce_db)
    with pytest.raises(CommerceDomainValidationError):
        create_project_product_selection(
            commerce_db,
            project_id=first_project.id,
            product_asset_id=product_one.id,
            product_asset_version_id=draft.id,
        )

    with pytest.raises(CommerceDomainValidationError):
        create_story_run(
            commerce_db,
            project_id=first_project.id,
            topic_candidate_id=second_topic.id,
            project_product_selection_id=first_selection.id,
            product_asset_version_id=version_one.id,
            run_number=1,
        )
    with pytest.raises(CommerceDomainValidationError):
        create_story_run(
            commerce_db,
            project_id=first_project.id,
            topic_candidate_id=first_topic.id,
            project_product_selection_id=second_selection.id,
            product_asset_version_id=version_two.id,
            run_number=1,
        )
    with pytest.raises(CommerceDomainValidationError):
        create_story_run(
            commerce_db,
            project_id=first_project.id,
            topic_candidate_id=first_topic.id,
            project_product_selection_id=first_selection.id,
            product_asset_version_id=version_two.id,
            run_number=1,
        )


def test_outline_chapter_scene_mapping_and_segment_cannot_cross_story_runs(commerce_db) -> None:
    """大纲、章节、映射和片段都必须绑定同一个 StoryRun。"""

    first = _run_with_graph(commerce_db, label="first-run")
    second = _run_with_graph(commerce_db, label="second-run")
    with pytest.raises(CommerceDomainValidationError):
        create_chapter_plan(
            commerce_db,
            story_run_id=first["story_run"].id,
            outline_version_id=second["outline"].id,
            chapter_number=2,
            title="错误章节",
            narrative_purpose="错误",
            content_summary="错误",
        )
    with pytest.raises(CommerceDomainValidationError):
        create_scene_mapping_version(
            commerce_db,
            story_run_id=first["story_run"].id,
            outline_version_id=second["outline"].id,
            version=1,
        )
    with pytest.raises(CommerceDomainValidationError):
        create_video_segment_plan(
            commerce_db,
            story_run_id=first["story_run"].id,
            chapter_id=second["chapter"].id,
            segment_number=2,
            target_duration_ms=4000,
            narrative_target="错误片段",
        )


@pytest.mark.parametrize(
    "location_overrides",
    [
        {"chapter_id": None, "video_segment_id": None, "sub_shot_id": None},
        {"video_segment_id": "also-present"},
        {"video_segment_id": "also-present", "sub_shot_id": "also-present"},
    ],
)
def test_product_placement_database_requires_exactly_one_location(commerce_db, location_overrides) -> None:
    """三选一定位由数据库 CHECK 兜底，不能为空或多选。"""

    graph = _run_with_graph(commerce_db, label="placement-check")
    fields = _placement_fields(graph, **location_overrides)
    commerce_db.add(ProductPlacementPlan(**fields))
    with pytest.raises(IntegrityError):
        commerce_db.commit()


def test_product_placement_validates_story_run_version_and_target_duration(commerce_db) -> None:
    """服务层补齐植入目标归属、冻结版本和相对时长规则。"""

    first = _run_with_graph(commerce_db, label="placement-first")
    second = _run_with_graph(commerce_db, label="placement-second")
    with pytest.raises(CommerceDomainValidationError):
        _validate_placement(commerce_db, _placement_fields(first, chapter_id=second["chapter"].id))
    with pytest.raises(CommerceDomainValidationError):
        _validate_placement(
            commerce_db,
            _placement_fields(first, product_asset_version_id=second["product_version"].id),
        )
    with pytest.raises(CommerceDomainValidationError):
        _validate_placement(
            commerce_db,
            _placement_fields(
                first,
                chapter_id=None,
                video_segment_id=first["segment"].id,
                planned_duration_ms=6001,
            ),
        )
    with pytest.raises(CommerceDomainValidationError):
        _validate_placement(
            commerce_db,
            _placement_fields(
                first,
                chapter_id=None,
                sub_shot_id=first["sub_shot"].id,
                planned_duration_ms=3001,
            ),
        )


def test_dialogue_time_boundaries_are_checked_against_segment_and_sub_shot(commerce_db) -> None:
    """对白既受 15 秒绝对上限约束，也受实际片段/子镜头时间线约束。"""

    graph = _run_with_graph(commerce_db, label="dialogue")
    with pytest.raises(CommerceDomainValidationError):
        create_dialogue_line(
            commerce_db,
            video_segment_id=graph["segment"].id,
            sub_shot_id=None,
            speaker="角色",
            dialogue="越过片段",
            start_ms=5000,
            end_ms=6001,
        )
    with pytest.raises(CommerceDomainValidationError):
        create_dialogue_line(
            commerce_db,
            video_segment_id=None,
            sub_shot_id=graph["sub_shot"].id,
            speaker="角色",
            dialogue="越过子镜头",
            start_ms=500,
            end_ms=2000,
        )
    with pytest.raises(IntegrityError):
        commerce_db.add(
            DialogueLine(
                video_segment_id=graph["segment"].id,
                speaker="角色",
                dialogue="绝对上限",
                start_ms=14999,
                end_ms=15001,
            )
        )
        commerce_db.commit()


def test_positive_number_and_render_batch_count_constraints(commerce_db) -> None:
    """版本/序号必须从 1 开始，批次成本与任务计数不得越界。"""

    graph = _run_with_graph(commerce_db, label="numbers")
    commerce_db.add(
        RenderBatch(
            story_run_id=graph["story_run"].id,
            batch_number=0,
            status=RenderBatchStatus.PENDING,
            total_tasks=1,
            completed_tasks=1,
            failed_tasks=1,
            running_tasks=0,
            estimated_cost=-0.1,
        )
    )
    with pytest.raises(IntegrityError):
        commerce_db.commit()


@pytest.mark.parametrize("invalid_number", [0, -1])
@pytest.mark.parametrize(
    "model_type, fields",
    [
        ("script", {"version": "invalid_number"}),
        ("product_analysis", {"version": "invalid_number"}),
        ("product_version", {"version": "invalid_number"}),
        ("story_run", {"run_number": "invalid_number"}),
        ("outline", {"version": "invalid_number"}),
        ("chapter", {"chapter_number": "invalid_number"}),
        ("scene_mapping", {"version": "invalid_number"}),
        ("segment", {"segment_number": "invalid_number"}),
        ("sub_shot", {"shot_number": "invalid_number"}),
    ],
)
def test_version_and_plan_numbers_reject_zero_and_negative(commerce_db, model_type, fields, invalid_number) -> None:
    """所有版本及计划序号的数据库 CHECK 均拒绝零和负数。"""

    graph = _run_with_graph(commerce_db, label=f"zero-{model_type}")
    media = MediaAsset(
        project_id=graph["project"].id,
        kind=AssetKind.SOURCE_VIDEO,
        original_filename="zero.mp4",
        content_type="video/mp4",
        byte_size=1,
        storage_key=f"commerce/zero/{uuid4().hex}.mp4",
    )
    commerce_db.add(media)
    _flush(commerce_db)
    script = ScriptAsset(project_id=graph["project"].id, media_asset_id=media.id, name="脚本")
    commerce_db.add(script)
    _flush(commerce_db)
    resolved_fields = {
        key: invalid_number if value == "invalid_number" else value for key, value in fields.items()
    }
    if model_type == "script":
        invalid = ScriptAnalysisVersion(script_asset_id=script.id, **resolved_fields)
    elif model_type == "product_analysis":
        invalid = ProductAnalysisVersion(product_asset_id=graph["product"].id, **resolved_fields)
    elif model_type == "product_version":
        invalid = ProductAssetVersion(
            product_asset_id=graph["product"].id,
            product_name="零版本",
            appearance_description="",
            **resolved_fields,
        )
    elif model_type == "outline":
        invalid = StoryOutlineVersion(
            story_run_id=graph["story_run"].id,
            title="零大纲",
            premise="零",
            **resolved_fields,
        )
    elif model_type == "story_run":
        invalid = StoryRun(
            project_id=graph["project"].id,
            topic_candidate_id=graph["topic"].id,
            project_product_selection_id=graph["selection"].id,
            product_asset_version_id=graph["product_version"].id,
            mode=StoryRunMode.STEPWISE,
            **resolved_fields,
        )
    elif model_type == "chapter":
        invalid = ChapterPlan(
            story_run_id=graph["story_run"].id,
            outline_version_id=graph["outline"].id,
            title="零章节",
            narrative_purpose="零",
            content_summary="零",
            **resolved_fields,
        )
    elif model_type == "scene_mapping":
        invalid = SceneMappingVersion(story_run_id=graph["story_run"].id, **resolved_fields)
    elif model_type == "segment":
        invalid = VideoSegmentPlan(
            story_run_id=graph["story_run"].id,
            chapter_id=graph["chapter"].id,
            target_duration_ms=4000,
            narrative_target="零片段",
            **resolved_fields,
        )
    else:
        invalid = SubShotPlan(
            video_segment_id=graph["segment"].id,
            start_ms=0,
            end_ms=1000,
            action="零",
            emotion="零",
            shot_scale="近景",
            camera_move="固定",
            lighting="自然光",
            visual_description="零",
            **resolved_fields,
        )
    commerce_db.add(invalid)
    with pytest.raises(IntegrityError):
        commerce_db.commit()


@pytest.mark.parametrize(
    "counter_name, counter_value",
    [
        ("total_tasks", -1),
        ("completed_tasks", -1),
        ("failed_tasks", -1),
        ("running_tasks", -1),
    ],
)
def test_render_batch_rejects_negative_task_counters(commerce_db, counter_name, counter_value) -> None:
    """每一个任务计数都有独立数据库非负约束，不能只依赖界面计算。"""

    graph = _run_with_graph(commerce_db, label=f"negative-{counter_name}")
    fields = {
        "story_run_id": graph["story_run"].id,
        "batch_number": 1,
        "status": RenderBatchStatus.PENDING,
        "total_tasks": 1,
        "completed_tasks": 0,
        "failed_tasks": 0,
        "running_tasks": 0,
    }
    fields[counter_name] = counter_value
    commerce_db.add(RenderBatch(**fields))
    with pytest.raises(IntegrityError):
        commerce_db.commit()


def test_render_batch_rejects_counts_that_exceed_total_and_negative_cost(commerce_db) -> None:
    """批次聚合不能出现完成/失败/运行之和超过总数或负成本。"""

    graph = _run_with_graph(commerce_db, label="invalid-batch-aggregate")
    commerce_db.add(
        RenderBatch(
            story_run_id=graph["story_run"].id,
            batch_number=1,
            status=RenderBatchStatus.PENDING,
            total_tasks=1,
            completed_tasks=1,
            failed_tasks=1,
            running_tasks=0,
        )
    )
    with pytest.raises(IntegrityError):
        commerce_db.commit()

    commerce_db.rollback()
    commerce_db.add(
        RenderBatch(
            story_run_id=graph["story_run"].id,
            batch_number=1,
            status=RenderBatchStatus.PENDING,
            total_tasks=1,
            completed_tasks=0,
            failed_tasks=0,
            running_tasks=0,
            estimated_cost=-0.1,
        )
    )
    with pytest.raises(IntegrityError):
        commerce_db.commit()


def test_version_services_append_and_protect_locked_content(commerce_db) -> None:
    """成功脚本、确认/冻结产品和锁定大纲只能通过下一版本修订。"""

    graph = _run_with_graph(commerce_db, label="versions")
    media = MediaAsset(
        project_id=graph["project"].id,
        kind=AssetKind.SOURCE_VIDEO,
        original_filename="script.mp4",
        content_type="video/mp4",
        byte_size=1,
        storage_key=f"commerce/version/{uuid4().hex}.mp4",
    )
    commerce_db.add(media)
    _flush(commerce_db)
    script = ScriptAsset(project_id=graph["project"].id, media_asset_id=media.id, name="可版本化脚本")
    commerce_db.add(script)
    _flush(commerce_db)
    script_v1 = create_next_script_analysis_version(
        commerce_db,
        script_asset_id=script.id,
        timeline_transcript=[{"text": "v1"}],
        analysis_status=ScriptAnalysisStatus.SUCCEEDED,
    )
    with pytest.raises(CommerceDomainValidationError):
        update_script_analysis_version(
            commerce_db,
            analysis_id=script_v1.id,
            timeline_transcript=[{"text": "覆盖 v1"}],
        )
    script_v2 = create_next_script_analysis_version(
        commerce_db,
        script_asset_id=script.id,
        timeline_transcript=[{"text": "v2"}],
    )
    assert (script_v1.version, script_v2.version) == (1, 2)

    product = ProductAsset(name=f"版本产品 {uuid4().hex[:8]}")
    commerce_db.add(product)
    _flush(commerce_db)
    product_v1 = create_next_product_asset_version(
        commerce_db,
        product_asset_id=product.id,
        product_name="草稿产品",
        appearance_description="初稿外观",
    )
    update_product_asset_version(
        commerce_db,
        product_asset_version_id=product_v1.id,
        appearance_description="草稿可修改",
    )
    transition_product_asset_version_status(
        commerce_db,
        product_asset_version_id=product_v1.id,
        next_status=ProductAssetVersionStatus.CONFIRMED,
    )
    freeze_product_asset_version(commerce_db, product_asset_version_id=product_v1.id)
    with pytest.raises(CommerceDomainValidationError):
        update_product_asset_version(
            commerce_db,
            product_asset_version_id=product_v1.id,
            product_name="覆盖确认版本",
        )
    product_v2 = create_next_product_asset_version(
        commerce_db,
        product_asset_id=product.id,
        product_name="新草稿产品",
        appearance_description="新外观",
    )
    assert (product_v1.version, product_v2.version) == (1, 2)

    outline_v1 = graph["outline"]
    transition_story_outline_version_status(
        commerce_db,
        story_outline_version_id=outline_v1.id,
        next_status=OutlineVersionStatus.LOCKED,
    )
    with pytest.raises(CommerceDomainValidationError):
        update_story_outline_version(
            commerce_db,
            story_outline_version_id=outline_v1.id,
            title="覆盖锁定大纲",
        )
    outline_v2 = create_next_story_outline_version(
        commerce_db,
        story_run_id=graph["story_run"].id,
        title="新大纲",
        premise="新前提",
    )
    assert (outline_v1.version, outline_v2.version) == (1, 2)


def test_succeeded_script_analysis_is_terminal_and_preserves_original_content(commerce_db) -> None:
    """成功脚本分析不能改内容、降级状态或通过两次调用解冻后覆盖。"""

    graph = _run_with_graph(commerce_db, label="script-terminal")
    media = MediaAsset(
        project_id=graph["project"].id,
        kind=AssetKind.SOURCE_VIDEO,
        original_filename="terminal.mp4",
        content_type="video/mp4",
        byte_size=1,
        storage_key=f"commerce/terminal/{uuid4().hex}.mp4",
    )
    commerce_db.add(media)
    _flush(commerce_db)
    script = ScriptAsset(project_id=graph["project"].id, media_asset_id=media.id, name="成功分析脚本")
    commerce_db.add(script)
    _flush(commerce_db)
    completed = create_next_script_analysis_version(
        commerce_db,
        script_asset_id=script.id,
        timeline_transcript=[{"text": "原始开场"}],
        story_beats=[{"beat": "原始冲突"}],
        raw_analysis={"provider": "mock", "version": 1},
        analysis_status=ScriptAnalysisStatus.SUCCEEDED,
    )
    commerce_db.commit()
    original_content = {
        "timeline_transcript": deepcopy(completed.timeline_transcript),
        "story_beats": deepcopy(completed.story_beats),
        "raw_analysis": deepcopy(completed.raw_analysis),
    }

    with pytest.raises(CommerceDomainValidationError):
        update_script_analysis_version(
            commerce_db,
            analysis_id=completed.id,
            timeline_transcript=[{"text": "尝试覆盖"}],
            story_beats=[{"beat": "尝试覆盖"}],
            raw_analysis={"provider": "unexpected"},
        )
    # 旧的两次调用绕过在第一步（SUCCEEDED -> PENDING）即失败。
    with pytest.raises(CommerceDomainValidationError) as status_error:
        update_script_analysis_version(
            commerce_db,
            analysis_id=completed.id,
            analysis_status=ScriptAnalysisStatus.PENDING,
        )
    assert "不可逆" in status_error.value.detail

    commerce_db.expire_all()
    persisted = commerce_db.get(ScriptAnalysisVersion, completed.id)
    assert persisted is not None
    assert persisted.analysis_status == ScriptAnalysisStatus.SUCCEEDED
    assert persisted.timeline_transcript == original_content["timeline_transcript"]
    assert persisted.story_beats == original_content["story_beats"]
    assert persisted.raw_analysis == original_content["raw_analysis"]

    reanalyzed = create_next_script_analysis_version(
        commerce_db,
        script_asset_id=script.id,
        timeline_transcript=[{"text": "重新分析的新开场"}],
        analysis_status=ScriptAnalysisStatus.PENDING,
    )
    assert (persisted.version, reanalyzed.version) == (1, 2)


def test_product_version_source_analysis_must_belong_to_same_product(commerce_db, monkeypatch) -> None:
    """创建/更新草稿都不能把另一产品的分析结果串入追溯链。"""

    first_product = ProductAsset(name=f"来源产品 {uuid4().hex[:8]}")
    second_product = ProductAsset(name=f"目标产品 {uuid4().hex[:8]}")
    commerce_db.add_all([first_product, second_product])
    _flush(commerce_db)
    first_analysis = ProductAnalysisVersion(
        product_asset_id=first_product.id,
        version=1,
        raw_analysis={"product": "first"},
        analysis_status=ProductAnalysisStatus.SUCCEEDED,
    )
    second_analysis = ProductAnalysisVersion(
        product_asset_id=second_product.id,
        version=1,
        raw_analysis={"product": "second"},
        analysis_status=ProductAnalysisStatus.SUCCEEDED,
    )
    commerce_db.add_all([first_analysis, second_analysis])
    _flush(commerce_db)
    draft = create_next_product_asset_version(
        commerce_db,
        product_asset_id=second_product.id,
        product_name="目标产品草稿",
        appearance_description="原始外观",
    )
    commerce_db.commit()

    with pytest.raises(CommerceDomainValidationError) as create_error:
        create_next_product_asset_version(
            commerce_db,
            product_asset_id=second_product.id,
            product_name="错误来源版本",
            appearance_description="不应创建",
            source_analysis_version_id=first_analysis.id,
        )
    assert "同一产品主体" in create_error.value.detail
    assert commerce_db.scalars(
        select(ProductAssetVersion).where(ProductAssetVersion.product_asset_id == second_product.id)
    ).all() == [draft]

    original_appearance = draft.appearance_description
    with pytest.raises(CommerceDomainValidationError) as update_error:
        update_product_asset_version(
            commerce_db,
            product_asset_version_id=draft.id,
            appearance_description="不应写入",
            source_analysis_version_id=first_analysis.id,
        )
    assert "同一产品主体" in update_error.value.detail
    commerce_db.expire(draft)
    assert draft.appearance_description == original_appearance
    assert draft.source_analysis_version_id is None

    valid_next = create_next_product_asset_version(
        commerce_db,
        product_asset_id=second_product.id,
        product_name="同产品来源版本",
        appearance_description="正确来源",
        source_analysis_version_id=second_analysis.id,
    )
    assert valid_next.source_analysis_version_id == second_analysis.id
    update_product_asset_version(
        commerce_db,
        product_asset_version_id=draft.id,
        source_analysis_version_id=second_analysis.id,
    )
    assert draft.source_analysis_version_id == second_analysis.id

    missing_id = str(uuid4())
    with pytest.raises(CommerceDomainValidationError) as missing_error:
        create_next_product_asset_version(
            commerce_db,
            product_asset_id=second_product.id,
            product_name="不存在分析",
            appearance_description="不应创建",
            source_analysis_version_id=missing_id,
        )
    assert missing_error.value.status_code == 404
    assert "来源产品分析版本不存在" in missing_error.value.detail
    assert "版本号冲突" not in missing_error.value.detail

    # 用稳定的旧版本号模拟并发读取到相同 max(version) 的写入竞争；只有真正的
    # owner + version 唯一冲突才应被包装为明确的 409。
    monkeypatch.setattr(commerce_domain_service, "_next_version", lambda *_: valid_next.version)
    with pytest.raises(CommerceDomainValidationError) as collision_error:
        create_next_product_asset_version(
            commerce_db,
            product_asset_id=second_product.id,
            product_name="并发冲突",
            appearance_description="重复版本号",
            source_analysis_version_id=second_analysis.id,
        )
    assert collision_error.value.status_code == 409
    assert "版本号冲突" in collision_error.value.detail


def test_story_run_rejects_product_version_archived_after_project_selection(commerce_db) -> None:
    """项目选择是历史记录；产品归档后不能据此启动新的 StoryRun。"""

    graph = _run_with_graph(commerce_db, label="archived-product")
    transition_product_asset_version_status(
        commerce_db,
        product_asset_version_id=graph["product_version"].id,
        next_status=ProductAssetVersionStatus.ARCHIVED,
    )
    with pytest.raises(CommerceDomainValidationError) as error:
        create_story_run(
            commerce_db,
            project_id=graph["project"].id,
            topic_candidate_id=graph["topic"].id,
            project_product_selection_id=graph["selection"].id,
            product_asset_version_id=graph["product_version"].id,
            run_number=2,
        )
    assert "已确认且已冻结" in error.value.detail


def test_render_batch_rejects_other_project_workflow_run(commerce_db) -> None:
    """RenderBatch 只能复用同项目的 WorkflowRun。"""

    graph = _run_with_graph(commerce_db, label="batch")
    other_project, other_workflow_run, _ = _project_with_topic(commerce_db, "other-batch")
    assert other_project.id != graph["project"].id
    with pytest.raises(CommerceDomainValidationError):
        create_render_batch(
            commerce_db,
            story_run_id=graph["story_run"].id,
            workflow_run_id=other_workflow_run.id,
            batch_number=1,
            total_tasks=1,
        )


def test_application_initialization_idempotently_seeds_v1_and_commerce_workflows(commerce_db) -> None:
    """真实应用初始化路径而非测试专用调用，会同时且幂等地补齐两个工作流定义。"""

    commerce_db.rollback()
    init_database()
    first = {
        definition.workflow_code: definition
        for definition in commerce_db.scalars(
            select(WorkflowDefinition).where(
                WorkflowDefinition.workflow_code.in_([V1_WORKFLOW_CODE, COMMERCE_WORKFLOW_CODE])
            )
        ).all()
    }
    init_database()
    second_count = commerce_db.scalars(
        select(WorkflowDefinition).where(
            WorkflowDefinition.workflow_code.in_([V1_WORKFLOW_CODE, COMMERCE_WORKFLOW_CODE])
        )
    ).all()
    assert set(first) == {V1_WORKFLOW_CODE, COMMERCE_WORKFLOW_CODE}
    assert len(second_count) == 2
    commerce_definition = first[COMMERCE_WORKFLOW_CODE]
    assert commerce_definition.version == COMMERCE_WORKFLOW_VERSION
    assert len(commerce_definition.definition_json["nodes"]) == 7
