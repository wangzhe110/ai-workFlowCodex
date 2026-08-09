"""带货短剧（Commerce）领域创建、归属校验和版本保护。

此模块是 Commerce 跨表规则的唯一入口。数据库负责单表数值、唯一性、外键和“三选一”
约束；任何需要读取两个以上对象的归属、时长或冻结版本规则均集中于此，未来 API 与
Worker 不得各自复制判断逻辑。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Type, TypeVar

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ChapterPlan,
    DialogueLine,
    MediaAsset,
    OutlineVersionStatus,
    ProductAnalysisVersion,
    ProductAsset,
    ProductAssetVersion,
    ProductAssetVersionStatus,
    ProductPlacementPlan,
    ProjectProductSelection,
    RenderBatch,
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
    WorkflowRun,
)


EntityT = TypeVar("EntityT")


class CommerceDomainValidationError(HTTPException):
    """Commerce 服务层的统一可预期业务错误。

    它继承项目现有的 ``HTTPException`` 体系，因此未来路由可直接返回明确的 4xx
    原因；当前阶段的服务层测试同样可以不依赖任何 HTTP 接口调用它。
    """

    def __init__(self, detail: str, *, status_code: int = status.HTTP_422_UNPROCESSABLE_CONTENT) -> None:
        super().__init__(status_code=status_code, detail=detail)


def utcnow() -> datetime:
    """统一生成 UTC 时间，确保冻结和状态转换可审计。"""

    return datetime.now(timezone.utc)


def _invalid(message: str) -> None:
    raise CommerceDomainValidationError(message)


def _conflict(message: str) -> None:
    raise CommerceDomainValidationError(message, status_code=status.HTTP_409_CONFLICT)


def _get_or_404(db: Session, entity_type: Type[EntityT], entity_id: str, label: str) -> EntityT:
    entity = db.get(entity_type, entity_id)
    if entity is None:
        raise CommerceDomainValidationError(f"{label}不存在", status_code=status.HTTP_404_NOT_FOUND)
    return entity


def _lock_parent_for_next_version(db: Session, entity_type: Type[EntityT], entity_id: str, label: str) -> EntityT:
    """尽可能串行化同一逻辑资产的追加版本；SQLite 会忽略锁，唯一约束仍兜底。"""

    parent = db.scalars(select(entity_type).where(entity_type.id == entity_id).with_for_update()).first()
    if parent is None:
        raise CommerceDomainValidationError(f"{label}不存在", status_code=status.HTTP_404_NOT_FOUND)
    return parent


def _next_version(db: Session, entity_type, owner_column, owner_id: str) -> int:
    """读取同一逻辑资产的下一个版本号；提交冲突仍由唯一约束转换为明确错误。"""

    current = db.scalar(select(func.max(entity_type.version)).where(owner_column == owner_id)) or 0
    return int(current) + 1


def _flush_append(db: Session, entity: EntityT, label: str) -> EntityT:
    """在保存点内写入新增版本，只把真正的版本号冲突转换为 409。"""

    try:
        with db.begin_nested():
            db.add(entity)
            db.flush()
    except IntegrityError as exc:
        if _is_version_unique_conflict(exc, entity):
            _conflict(f"{label}版本号冲突，请重新读取最新版本后再创建")
        raise
    return entity


def _is_version_unique_conflict(error: IntegrityError, entity: EntityT) -> bool:
    """识别三个追加版本表的真实 ``owner + version`` 唯一冲突。

    PostgreSQL 提供约束名，SQLite 通常只返回列名。其他外键、非空或检查约束错误
    必须原样上抛，不能被误报成“版本号冲突”。
    """

    table_name = getattr(entity, "__tablename__", "")
    constraints = {
        "script_analysis_versions": ("uq_script_analysis_version", "script_asset_id"),
        "product_asset_versions": ("uq_product_asset_version", "product_asset_id"),
        "story_outline_versions": ("uq_story_outline_version", "story_run_id"),
    }
    expected = constraints.get(table_name)
    if expected is None:
        return False
    constraint_name, owner_column = expected
    postgres_constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
    if postgres_constraint == constraint_name:
        return True
    message = str(error.orig).lower()
    return (
        constraint_name in message
        or f"{table_name}.{owner_column}, {table_name}.version" in message
    )


# ---------------------------------------------------------------------------
# 跨对象归属与冻结版本校验。
# ---------------------------------------------------------------------------


def validate_script_asset_ownership(db: Session, *, project_id: str, media_asset_id: str) -> MediaAsset:
    """确保脚本逻辑资产只能引用本项目上传的媒体。"""

    media_asset = _get_or_404(db, MediaAsset, media_asset_id, "来源媒体")
    if media_asset.project_id != project_id:
        _invalid("脚本资产只能绑定本项目上传的来源媒体")
    return media_asset


def validate_project_product_selection(
    db: Session,
    *,
    project_id: str,
    product_asset_id: str,
    product_asset_version_id: str,
) -> ProductAssetVersion:
    """校验项目采用的是正确产品主体下、可投入生产的冻结版本。"""

    _get_or_404(db, ProductAsset, product_asset_id, "产品主体")
    product_version = _get_or_404(db, ProductAssetVersion, product_asset_version_id, "产品生产版本")
    if product_version.product_asset_id != product_asset_id:
        _invalid("项目产品选择中的产品主体与产品生产版本不一致")
    if product_version.status != ProductAssetVersionStatus.CONFIRMED or product_version.frozen_at is None:
        _conflict("项目只能选择已确认且已冻结、允许投入生产的产品版本")
    return product_version


def validate_product_version_source_analysis(
    db: Session,
    *,
    product_asset_id: str,
    source_analysis_version_id: Optional[str],
) -> None:
    """确保产品生产版本只能追溯到同一产品主体的分析版本。"""

    if source_analysis_version_id is None:
        return
    source_analysis = _get_or_404(db, ProductAnalysisVersion, source_analysis_version_id, "来源产品分析版本")
    if source_analysis.product_asset_id != product_asset_id:
        _invalid("产品生产版本的来源分析必须属于同一产品主体")


def validate_story_run_bindings(
    db: Session,
    *,
    project_id: str,
    topic_candidate_id: str,
    project_product_selection_id: str,
    product_asset_version_id: str,
) -> tuple[TopicCandidate, ProjectProductSelection, ProductAssetVersion]:
    """验证 StoryRun 的选题、项目产品选择和冻结版本完全属于同一项目。"""

    topic = _get_or_404(db, TopicCandidate, topic_candidate_id, "选题")
    selection = _get_or_404(db, ProjectProductSelection, project_product_selection_id, "项目产品选择")
    product_version = _get_or_404(db, ProductAssetVersion, product_asset_version_id, "产品生产版本")
    if topic.project_id != project_id:
        _invalid("StoryRun 不能绑定其他项目的选题")
    if selection.project_id != project_id:
        _invalid("StoryRun 不能绑定其他项目的产品选择")
    if selection.product_asset_version_id != product_asset_version_id:
        _invalid("StoryRun 冻结的产品版本必须与项目产品选择绑定的版本一致")
    if product_version.id != selection.product_asset_version_id:
        _invalid("StoryRun 的产品版本引用无效")
    # 项目选择建立后，产品版本仍可能被归档。历史 StoryRun 保留其冻结快照，但
    # 新运行不能再把已归档或尚未冻结的版本投入生产。
    if product_version.status != ProductAssetVersionStatus.CONFIRMED or product_version.frozen_at is None:
        _conflict("StoryRun 只能使用项目已确认且已冻结、仍允许投入生产的产品版本")
    return topic, selection, product_version


def validate_chapter_plan_bindings(db: Session, *, story_run_id: str, outline_version_id: str) -> StoryOutlineVersion:
    """章节只能使用本次 StoryRun 自己的大纲版本。"""

    outline = _get_or_404(db, StoryOutlineVersion, outline_version_id, "故事大纲版本")
    if outline.story_run_id != story_run_id:
        _invalid("章节不能引用其他 StoryRun 的大纲版本")
    return outline


def validate_scene_mapping_bindings(
    db: Session, *, story_run_id: str, outline_version_id: Optional[str]
) -> Optional[StoryOutlineVersion]:
    """场景映射如引用大纲，必须和自身 StoryRun 一致。"""

    if outline_version_id is None:
        return None
    outline = _get_or_404(db, StoryOutlineVersion, outline_version_id, "故事大纲版本")
    if outline.story_run_id != story_run_id:
        _invalid("场景映射不能引用其他 StoryRun 的大纲版本")
    return outline


def validate_video_segment_bindings(db: Session, *, story_run_id: str, chapter_id: str) -> ChapterPlan:
    """视频片段只能引用本次 StoryRun 的章节。"""

    chapter = _get_or_404(db, ChapterPlan, chapter_id, "章节")
    if chapter.story_run_id != story_run_id:
        _invalid("视频片段不能引用其他 StoryRun 的章节")
    return chapter


def validate_sub_shot_within_segment(segment: VideoSegmentPlan, *, start_ms: int, end_ms: int) -> None:
    """校验子镜头相对时间不超过父片段目标时长。"""

    if start_ms < 0:
        _invalid("子镜头开始时间不能小于 0ms")
    if end_ms <= start_ms:
        _invalid("子镜头结束时间必须晚于开始时间")
    if end_ms > segment.target_duration_ms:
        _invalid(f"子镜头结束时间 {end_ms}ms 超出片段目标时长 {segment.target_duration_ms}ms")


def validate_dialogue_line_bindings(
    db: Session,
    *,
    video_segment_id: Optional[str],
    sub_shot_id: Optional[str],
    start_ms: int,
    end_ms: int,
) -> VideoSegmentPlan:
    """对白只能归属一个片段或子镜头，并且时间必须落在实际可渲染范围内。"""

    if (video_segment_id is None) == (sub_shot_id is None):
        _invalid("对白必须且只能关联一个视频片段或子镜头")
    if start_ms < 0 or end_ms <= start_ms or end_ms > 15000:
        _invalid("对白时间必须满足 0 <= start_ms < end_ms <= 15000")
    if video_segment_id is not None:
        segment = _get_or_404(db, VideoSegmentPlan, video_segment_id, "视频片段")
        if end_ms > segment.target_duration_ms:
            _invalid("对白结束时间不能超过所属视频片段目标时长")
        return segment
    sub_shot = _get_or_404(db, SubShotPlan, sub_shot_id or "", "子镜头")
    segment = _get_or_404(db, VideoSegmentPlan, sub_shot.video_segment_id, "子镜头所属视频片段")
    if start_ms < sub_shot.start_ms or end_ms > sub_shot.end_ms:
        _invalid("对白时间必须完全落在所属子镜头时间范围内")
    if end_ms > segment.target_duration_ms:
        _invalid("对白结束时间不能超过所属视频片段目标时长")
    return segment


def _placement_target(
    db: Session,
    *,
    story_run_id: str,
    chapter_id: Optional[str],
    video_segment_id: Optional[str],
    sub_shot_id: Optional[str],
) -> tuple[str, Optional[Any]]:
    """校验植入目标三选一，并返回目标类型和对象。"""

    target_count = sum(item is not None for item in (chapter_id, video_segment_id, sub_shot_id))
    if target_count != 1:
        _invalid("产品植入必须且只能直接定位到章节、视频片段或子镜头中的一个")
    if chapter_id is not None:
        chapter = _get_or_404(db, ChapterPlan, chapter_id, "章节")
        if chapter.story_run_id != story_run_id:
            _invalid("产品植入章节不属于当前 StoryRun")
        return "CHAPTER", chapter
    if video_segment_id is not None:
        segment = _get_or_404(db, VideoSegmentPlan, video_segment_id, "视频片段")
        if segment.story_run_id != story_run_id:
            _invalid("产品植入视频片段不属于当前 StoryRun")
        return "SEGMENT", segment
    sub_shot = _get_or_404(db, SubShotPlan, sub_shot_id or "", "子镜头")
    segment = _get_or_404(db, VideoSegmentPlan, sub_shot.video_segment_id, "子镜头所属视频片段")
    if segment.story_run_id != story_run_id:
        _invalid("产品植入子镜头不属于当前 StoryRun")
    return "SUB_SHOT", sub_shot


def validate_product_placement_bindings(
    db: Session,
    *,
    story_run_id: str,
    product_asset_version_id: str,
    chapter_id: Optional[str],
    video_segment_id: Optional[str],
    sub_shot_id: Optional[str],
    planned_duration_ms: int,
) -> None:
    """验证植入版本、唯一定位及其相对目标时长。"""

    story_run = _get_or_404(db, StoryRun, story_run_id, "StoryRun")
    if story_run.product_asset_version_id != product_asset_version_id:
        _invalid("产品植入必须使用当前 StoryRun 冻结的产品版本")
    if planned_duration_ms < 0:
        _invalid("产品植入计划时长不能为负数")
    target_type, target = _placement_target(
        db,
        story_run_id=story_run_id,
        chapter_id=chapter_id,
        video_segment_id=video_segment_id,
        sub_shot_id=sub_shot_id,
    )
    if target_type == "SEGMENT" and planned_duration_ms > target.target_duration_ms:
        _invalid("产品植入计划时长不能超过目标视频片段时长")
    if target_type == "SUB_SHOT" and planned_duration_ms > target.end_ms - target.start_ms:
        _invalid("产品植入计划时长不能超过目标子镜头时长")


def validate_render_batch_bindings(
    db: Session, *, story_run_id: str, workflow_run_id: Optional[str]
) -> Optional[WorkflowRun]:
    """批量渲染仍复用 WorkflowRun，且不能跨项目绑定运行记录。"""

    if workflow_run_id is None:
        return None
    story_run = _get_or_404(db, StoryRun, story_run_id, "StoryRun")
    workflow_run = _get_or_404(db, WorkflowRun, workflow_run_id, "工作流运行")
    if workflow_run.project_id != story_run.project_id:
        _invalid("RenderBatch 不能绑定其他项目的 WorkflowRun")
    return workflow_run


# ---------------------------------------------------------------------------
# 后续 API 可直接使用的创建边界。
# ---------------------------------------------------------------------------


def create_script_asset(db: Session, *, project_id: str, media_asset_id: str, name: str) -> ScriptAsset:
    """创建和项目媒体严格同归属的脚本资产。"""

    validate_script_asset_ownership(db, project_id=project_id, media_asset_id=media_asset_id)
    script_asset = ScriptAsset(project_id=project_id, media_asset_id=media_asset_id, name=name)
    db.add(script_asset)
    db.flush()
    return script_asset


def create_project_product_selection(
    db: Session,
    *,
    project_id: str,
    product_asset_id: str,
    product_asset_version_id: str,
) -> ProjectProductSelection:
    """为项目创建一条可生产的冻结产品版本选择。"""

    validate_project_product_selection(
        db,
        project_id=project_id,
        product_asset_id=product_asset_id,
        product_asset_version_id=product_asset_version_id,
    )
    selection = ProjectProductSelection(
        project_id=project_id,
        product_asset_id=product_asset_id,
        product_asset_version_id=product_asset_version_id,
    )
    db.add(selection)
    db.flush()
    return selection


def create_story_run(
    db: Session,
    *,
    project_id: str,
    topic_candidate_id: str,
    project_product_selection_id: str,
    product_asset_version_id: str,
    run_number: int,
    mode: StoryRunMode = StoryRunMode.STEPWISE,
) -> StoryRun:
    """创建带独立状态机的 StoryRun，并冻结选题与产品选择关系。"""

    if run_number < 1:
        _invalid("StoryRun 的 run_number 必须大于等于 1")
    validate_story_run_bindings(
        db,
        project_id=project_id,
        topic_candidate_id=topic_candidate_id,
        project_product_selection_id=project_product_selection_id,
        product_asset_version_id=product_asset_version_id,
    )
    story_run = StoryRun(
        project_id=project_id,
        topic_candidate_id=topic_candidate_id,
        project_product_selection_id=project_product_selection_id,
        product_asset_version_id=product_asset_version_id,
        run_number=run_number,
        mode=mode,
    )
    db.add(story_run)
    db.flush()
    db.add(
        StoryRunState(
            story_run_id=story_run.id,
            current_stage=StoryRunStage.TOPIC,
            status=StoryRunStatus.PENDING,
        )
    )
    db.flush()
    return story_run


def create_chapter_plan(
    db: Session,
    *,
    story_run_id: str,
    outline_version_id: str,
    chapter_number: int,
    title: str,
    narrative_purpose: str,
    content_summary: str,
    product_plan: Optional[dict[str, Any]] = None,
) -> ChapterPlan:
    """创建属于指定大纲版本和 StoryRun 的章节。"""

    if chapter_number < 1:
        _invalid("章节序号必须大于等于 1")
    validate_chapter_plan_bindings(db, story_run_id=story_run_id, outline_version_id=outline_version_id)
    chapter = ChapterPlan(
        story_run_id=story_run_id,
        outline_version_id=outline_version_id,
        chapter_number=chapter_number,
        title=title,
        narrative_purpose=narrative_purpose,
        content_summary=content_summary,
        product_plan=product_plan or {},
    )
    db.add(chapter)
    db.flush()
    return chapter


def create_scene_mapping_version(
    db: Session,
    *,
    story_run_id: str,
    outline_version_id: Optional[str],
    version: int,
    mapping_snapshot: Optional[list[dict[str, Any]]] = None,
    status_value: str = "DRAFT",
) -> SceneMappingVersion:
    """创建版本化场景映射，并防止引用其他 StoryRun 的大纲。"""

    if version < 1:
        _invalid("场景映射版本号必须大于等于 1")
    validate_scene_mapping_bindings(db, story_run_id=story_run_id, outline_version_id=outline_version_id)
    scene_mapping = SceneMappingVersion(
        story_run_id=story_run_id,
        outline_version_id=outline_version_id,
        version=version,
        mapping_snapshot=mapping_snapshot or [],
        status=status_value,
    )
    db.add(scene_mapping)
    db.flush()
    return scene_mapping


def create_video_segment_plan(
    db: Session,
    *,
    story_run_id: str,
    chapter_id: str,
    segment_number: int,
    target_duration_ms: int,
    narrative_target: str,
    **fields: Any,
) -> VideoSegmentPlan:
    """创建片段规划，并在写入前统一验证章节归属。"""

    if segment_number < 1:
        _invalid("视频片段序号必须大于等于 1")
    validate_video_segment_bindings(db, story_run_id=story_run_id, chapter_id=chapter_id)
    segment = VideoSegmentPlan(
        story_run_id=story_run_id,
        chapter_id=chapter_id,
        segment_number=segment_number,
        target_duration_ms=target_duration_ms,
        narrative_target=narrative_target,
        **fields,
    )
    db.add(segment)
    db.flush()
    return segment


def add_sub_shot_plan(
    db: Session,
    segment: VideoSegmentPlan,
    *,
    shot_number: int,
    start_ms: int,
    end_ms: int,
    character_refs: Optional[list[dict[str, Any]]] = None,
    action: str,
    emotion: str,
    shot_scale: str,
    camera_move: str,
    lighting: str,
    visual_description: str,
) -> SubShotPlan:
    """经过跨表校验后追加一个子镜头，绝不修改已有镜头时间线。"""

    if shot_number < 1:
        _invalid("子镜头序号必须大于等于 1")
    validate_sub_shot_within_segment(segment, start_ms=start_ms, end_ms=end_ms)
    sub_shot = SubShotPlan(
        video_segment_id=segment.id,
        shot_number=shot_number,
        start_ms=start_ms,
        end_ms=end_ms,
        character_refs=character_refs or [],
        action=action,
        emotion=emotion,
        shot_scale=shot_scale,
        camera_move=camera_move,
        lighting=lighting,
        visual_description=visual_description,
    )
    db.add(sub_shot)
    db.flush()
    return sub_shot


def create_dialogue_line(
    db: Session,
    *,
    video_segment_id: Optional[str],
    sub_shot_id: Optional[str],
    speaker: str,
    dialogue: str,
    start_ms: int,
    end_ms: int,
) -> DialogueLine:
    """创建时验证对白归属和相对时间，不允许跨片段或越界。"""

    validate_dialogue_line_bindings(
        db,
        video_segment_id=video_segment_id,
        sub_shot_id=sub_shot_id,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    line = DialogueLine(
        video_segment_id=video_segment_id,
        sub_shot_id=sub_shot_id,
        speaker=speaker,
        dialogue=dialogue,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    db.add(line)
    db.flush()
    return line


def create_product_placement_plan(db: Session, **fields: Any) -> ProductPlacementPlan:
    """创建产品植入前统一验证冻结版本、唯一定位和时长。"""

    validate_product_placement_bindings(
        db,
        story_run_id=fields["story_run_id"],
        product_asset_version_id=fields["product_asset_version_id"],
        chapter_id=fields.get("chapter_id"),
        video_segment_id=fields.get("video_segment_id"),
        sub_shot_id=fields.get("sub_shot_id"),
        planned_duration_ms=fields.get("planned_duration_ms", 0),
    )
    placement = ProductPlacementPlan(**fields)
    db.add(placement)
    db.flush()
    return placement


def create_render_batch(db: Session, **fields: Any) -> RenderBatch:
    """创建批量渲染聚合记录，并阻止跨项目 WorkflowRun 绑定。"""

    batch_number = fields["batch_number"]
    if batch_number < 1:
        _invalid("批次序号必须大于等于 1")
    validate_render_batch_bindings(
        db,
        story_run_id=fields["story_run_id"],
        workflow_run_id=fields.get("workflow_run_id"),
    )
    batch = RenderBatch(**fields)
    db.add(batch)
    db.flush()
    return batch


# ---------------------------------------------------------------------------
# 不可覆盖版本规则：修订必须追加新版本，状态迁移不能携带内容覆盖。
# ---------------------------------------------------------------------------


_SCRIPT_ANALYSIS_CONTENT_FIELDS = {
    "timeline_transcript",
    "story_beats",
    "role_archetypes",
    "conflicts",
    "turning_points",
    "emotional_curve",
    "chapter_candidates",
    "product_slot_candidates",
    "narrative_function_sequence",
    "raw_analysis",
}
_PRODUCT_VERSION_CONTENT_FIELDS = {
    "source_analysis_version_id",
    "product_name",
    "appearance_description",
    "selling_points",
    "user_pain_points",
    "usage_scenarios",
    "package_ocr",
    "reference_images",
}
_OUTLINE_CONTENT_FIELDS = {"title", "premise", "story_beats", "product_placement_strategy"}


def _apply_fields(entity: Any, changes: dict[str, Any], allowed: set[str], label: str) -> None:
    """防止服务层静默接受未知字段或用错实体字段。"""

    unexpected = set(changes) - allowed
    if unexpected:
        _invalid(f"{label}不支持修改字段：{'、'.join(sorted(unexpected))}")
    for field, value in changes.items():
        setattr(entity, field, value)


def create_next_script_analysis_version(db: Session, *, script_asset_id: str, **contents: Any) -> ScriptAnalysisVersion:
    """为同一脚本创建最大版本号加一的分析快照。"""

    unexpected = set(contents) - _SCRIPT_ANALYSIS_CONTENT_FIELDS - {"analysis_status"}
    if unexpected:
        _invalid(f"脚本分析版本不支持字段：{'、'.join(sorted(unexpected))}")
    _lock_parent_for_next_version(db, ScriptAsset, script_asset_id, "脚本资产")
    version = _next_version(db, ScriptAnalysisVersion, ScriptAnalysisVersion.script_asset_id, script_asset_id)
    analysis = ScriptAnalysisVersion(script_asset_id=script_asset_id, version=version, **contents)
    return _flush_append(db, analysis, "脚本分析")


def update_script_analysis_version(db: Session, *, analysis_id: str, **changes: Any) -> ScriptAnalysisVersion:
    """只允许未成功分析的内容修改；成功状态不可逆且只能追加新版本。"""

    analysis = _get_or_404(db, ScriptAnalysisVersion, analysis_id, "脚本分析版本")
    content_changes = set(changes) & _SCRIPT_ANALYSIS_CONTENT_FIELDS
    if analysis.analysis_status == ScriptAnalysisStatus.SUCCEEDED:
        if content_changes:
            _conflict("已成功完成的脚本分析内容不可覆盖，请创建下一版本")
        if "analysis_status" in changes and changes["analysis_status"] not in {
            ScriptAnalysisStatus.SUCCEEDED,
            ScriptAnalysisStatus.SUCCEEDED.value,
        }:
            _conflict("已成功完成的脚本分析状态不可逆，请创建下一版本重新分析")
    _apply_fields(
        analysis,
        changes,
        _SCRIPT_ANALYSIS_CONTENT_FIELDS | {"analysis_status"},
        "脚本分析版本",
    )
    db.flush()
    return analysis


def create_next_product_asset_version(
    db: Session, *, product_asset_id: str, **contents: Any
) -> ProductAssetVersion:
    """创建产品生产草稿 vN+1；已有确认/冻结版本永远不被覆盖。"""

    unexpected = set(contents) - _PRODUCT_VERSION_CONTENT_FIELDS
    if unexpected:
        _invalid(f"产品生产版本不支持字段：{'、'.join(sorted(unexpected))}")
    _lock_parent_for_next_version(db, ProductAsset, product_asset_id, "产品主体")
    validate_product_version_source_analysis(
        db,
        product_asset_id=product_asset_id,
        source_analysis_version_id=contents.get("source_analysis_version_id"),
    )
    version = _next_version(db, ProductAssetVersion, ProductAssetVersion.product_asset_id, product_asset_id)
    product_version = ProductAssetVersion(
        product_asset_id=product_asset_id,
        version=version,
        status=ProductAssetVersionStatus.DRAFT,
        **contents,
    )
    return _flush_append(db, product_version, "产品生产")


def transition_product_asset_version_status(
    db: Session, *, product_asset_version_id: str, next_status: ProductAssetVersionStatus
) -> ProductAssetVersion:
    """仅执行显式产品版本状态迁移，不接受任何内容字段。"""

    product_version = _get_or_404(db, ProductAssetVersion, product_asset_version_id, "产品生产版本")
    allowed = {
        ProductAssetVersionStatus.DRAFT: {
            ProductAssetVersionStatus.CONFIRMED,
            ProductAssetVersionStatus.ARCHIVED,
        },
        ProductAssetVersionStatus.CONFIRMED: {ProductAssetVersionStatus.ARCHIVED},
        ProductAssetVersionStatus.ARCHIVED: set(),
    }
    if next_status == product_version.status:
        return product_version
    if next_status not in allowed[product_version.status]:
        _conflict(f"产品生产版本不能从 {product_version.status.value} 转为 {next_status.value}")
    product_version.status = next_status
    db.flush()
    return product_version


def freeze_product_asset_version(db: Session, *, product_asset_version_id: str) -> ProductAssetVersion:
    """冻结已确认版本，供项目选择和 StoryRun 生产引用。"""

    product_version = _get_or_404(db, ProductAssetVersion, product_asset_version_id, "产品生产版本")
    if product_version.status != ProductAssetVersionStatus.CONFIRMED:
        _conflict("只有已确认的产品生产版本可以冻结")
    if product_version.frozen_at is None:
        product_version.frozen_at = utcnow()
        db.flush()
    return product_version


def update_product_asset_version(db: Session, *, product_asset_version_id: str, **changes: Any) -> ProductAssetVersion:
    """只允许草稿内容编辑；状态变更必须经专门的状态机函数。"""

    product_version = _get_or_404(db, ProductAssetVersion, product_asset_version_id, "产品生产版本")
    if "status" in changes or "frozen_at" in changes:
        _invalid("产品生产版本状态或冻结时间必须使用专门状态转换方法更新")
    content_changes = set(changes) & _PRODUCT_VERSION_CONTENT_FIELDS
    if product_version.status != ProductAssetVersionStatus.DRAFT or product_version.frozen_at is not None:
        if content_changes:
            _conflict("已确认、已冻结或已归档的产品生产版本内容不可覆盖，请创建下一版本")
    if "source_analysis_version_id" in changes:
        validate_product_version_source_analysis(
            db,
            product_asset_id=product_version.product_asset_id,
            source_analysis_version_id=changes["source_analysis_version_id"],
        )
    _apply_fields(product_version, changes, _PRODUCT_VERSION_CONTENT_FIELDS, "产品生产版本")
    db.flush()
    return product_version


def create_next_story_outline_version(db: Session, *, story_run_id: str, **contents: Any) -> StoryOutlineVersion:
    """为 StoryRun 创建 vN+1 草稿大纲。"""

    unexpected = set(contents) - _OUTLINE_CONTENT_FIELDS
    if unexpected:
        _invalid(f"故事大纲版本不支持字段：{'、'.join(sorted(unexpected))}")
    _lock_parent_for_next_version(db, StoryRun, story_run_id, "StoryRun")
    version = _next_version(db, StoryOutlineVersion, StoryOutlineVersion.story_run_id, story_run_id)
    outline = StoryOutlineVersion(
        story_run_id=story_run_id,
        version=version,
        status=OutlineVersionStatus.DRAFT,
        **contents,
    )
    return _flush_append(db, outline, "故事大纲")


def transition_story_outline_version_status(
    db: Session, *, story_outline_version_id: str, next_status: OutlineVersionStatus
) -> StoryOutlineVersion:
    """执行明确的大纲状态迁移，不允许借状态调用覆写正文。"""

    outline = _get_or_404(db, StoryOutlineVersion, story_outline_version_id, "故事大纲版本")
    allowed = {
        OutlineVersionStatus.DRAFT: {OutlineVersionStatus.LOCKED, OutlineVersionStatus.SUPERSEDED},
        OutlineVersionStatus.LOCKED: {OutlineVersionStatus.SUPERSEDED},
        OutlineVersionStatus.SUPERSEDED: set(),
    }
    if next_status == outline.status:
        return outline
    if next_status not in allowed[outline.status]:
        _conflict(f"故事大纲不能从 {outline.status.value} 转为 {next_status.value}")
    outline.status = next_status
    db.flush()
    return outline


def update_story_outline_version(db: Session, *, story_outline_version_id: str, **changes: Any) -> StoryOutlineVersion:
    """仅草稿大纲可编辑，锁定或被取代的大纲只能追加新版本。"""

    outline = _get_or_404(db, StoryOutlineVersion, story_outline_version_id, "故事大纲版本")
    if "status" in changes:
        _invalid("故事大纲状态必须使用专门状态转换方法更新")
    content_changes = set(changes) & _OUTLINE_CONTENT_FIELDS
    if outline.status in {OutlineVersionStatus.LOCKED, OutlineVersionStatus.SUPERSEDED} and content_changes:
        _conflict("已锁定或已被取代的故事大纲内容不可覆盖，请创建下一版本")
    _apply_fields(outline, changes, _OUTLINE_CONTENT_FIELDS, "故事大纲版本")
    db.flush()
    return outline
