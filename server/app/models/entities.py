"""数据库实体：保存项目、资产、工作流和模型配置的可审计状态。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum as SqlEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def utcnow() -> datetime:
    """统一保存 UTC 时间，前端再按用户时区展示。"""

    return datetime.now(timezone.utc)


def new_id() -> str:
    """生成不依赖数据库自增序列的公开 ID，便于分布式扩容。"""

    return str(uuid4())


class RunStatus(str, Enum):
    """工作流和步骤共用的状态机终态集合。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AssetKind(str, Enum):
    """资产类型；后续将补充 IMAGE、VIDEO_CLIP、AUDIO 等。"""

    SOURCE_VIDEO = "SOURCE_VIDEO"


class AssetLibraryKind(str, Enum):
    """资产中心的一级类型。

    它与 ``MediaAsset`` 不同：MediaAsset 保存项目文件，而资产中心保存可以被多个
    项目引用的“角色/场景生产资产”及其不可覆盖版本。
    """

    CHARACTER = "CHARACTER"
    SCENE = "SCENE"


class LibraryItemKind(str, Enum):
    """创作资产库的两种首批知识类型。"""

    VIRAL_ELEMENT = "VIRAL_ELEMENT"
    OPENING_PATTERN = "OPENING_PATTERN"


class LibraryItemSource(str, Enum):
    """资产来源决定它能否被后续审核和追溯。"""

    MANUAL = "MANUAL"
    ANALYSIS = "ANALYSIS"


class TopicStatus(str, Enum):
    """选题从候选到人工确认的状态。"""

    DRAFT = "DRAFT"
    SELECTED = "SELECTED"


class StoryStatus(str, Enum):
    """故事包只有经人工确认后才能进入分镜生成。"""

    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"

class StoryboardStatus(str, Enum):
    """图片和视频生成只能消费人工确认的分镜版本。"""
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"

class ImageStatus(str, Enum):
    """单镜图片状态；失败镜头可独立重跑，不阻塞已完成镜头。"""
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class VideoClipStatus(str, Enum):
    """按镜头组生成的视频片段状态。

    视频模型可能需要数十秒或更久，片段状态独立于工作流运行状态保存，便于以后
    对某一组单独重做而不影响其他已经审核通过的组。
    """

    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class FinalVideoStatus(str, Enum):
    """完整成片的合成状态。

    成片并不覆盖片段结果；它只冻结一次导出实际选用的片段版本，允许用户在重做
    某一组后重新合成，保留旧成片以便审核和回退。
    """

    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class WorkflowDefinitionStatus(str, Enum):
    """工作流定义的发布状态；已发布版本不得原地修改。"""

    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"


class ProductionStage(str, Enum):
    """LemonFlow V1 项目生产台的唯一主流程阶段。"""

    LEGACY_READONLY = "LEGACY_READONLY"
    REFERENCE_ANALYSIS = "REFERENCE_ANALYSIS"
    ANALYSIS_REVIEW = "ANALYSIS_REVIEW"
    STORY_GENERATION = "STORY_GENERATION"
    STORY_REVIEW = "STORY_REVIEW"
    CHARACTER_ASSETS = "CHARACTER_ASSETS"
    SCENE_ASSETS = "SCENE_ASSETS"
    DIRECTOR_PLANNING = "DIRECTOR_PLANNING"
    SHOT_KEYFRAMES = "SHOT_KEYFRAMES"
    VIDEO_GENERATION = "VIDEO_GENERATION"
    VIDEO_REVIEW = "VIDEO_REVIEW"
    FINAL_EXPORT = "FINAL_EXPORT"
    COMPLETED = "COMPLETED"


class ReviewStatus(str, Enum):
    """可生成对象的通用人工审核状态。"""

    PENDING_REVIEW = "PENDING_REVIEW"
    LOCKED = "LOCKED"
    REJECTED = "REJECTED"


class StoryProposalStatus(str, Enum):
    """故事候选只能由人工选择进入后续资产生产。"""

    CANDIDATE = "CANDIDATE"
    SELECTED = "SELECTED"
    REJECTED = "REJECTED"


class DirectorPlanStatus(str, Enum):
    """导演方案生成状态，不等同于图片或视频生成状态。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    READY = "READY"
    FAILED = "FAILED"


class DesignStatus(str, Enum):
    """角色和场景文字设定的可编辑/冻结状态。"""

    DRAFT = "DRAFT"
    READY = "READY"
    LOCKED = "LOCKED"


class VideoReviewStatus(str, Enum):
    """视频生成成功后必须独立进入人工审核。"""

    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ModelSelectionMode(str, Enum):
    """模型槽位的选择策略；V1 不自动切换生产模型。"""

    SINGLE = "SINGLE"
    MULTI_PARALLEL = "MULTI_PARALLEL"
    AB_TEST = "AB_TEST"


class PromptTemplateStatus(str, Enum):
    """Prompt 模板版本状态；激活版本不可原地修改。"""

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


# ---------------------------------------------------------------------------
# 带货短剧（Commerce）工作流的领域枚举。
#
# 它们不复用 V1 的 ``ProductionStage``：V1 仍服务于参考视频二创主流程，而
# Commerce 是一个可与 V1 并存、以选题独立运行和产品版本冻结为核心的新工作流。
# ---------------------------------------------------------------------------


class ScriptAnalysisStatus(str, Enum):
    """脚本分析版本的执行状态；分析结果只追加新版本，绝不原地覆盖。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ProductAnalysisStatus(str, Enum):
    """产品原始素材分析版本的执行状态。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ProductAssetVersionStatus(str, Enum):
    """产品生产版本的人工可用状态。"""

    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    ARCHIVED = "ARCHIVED"


class StoryRunMode(str, Enum):
    """带货短剧运行的推进方式。"""

    STEPWISE = "STEPWISE"
    AUTO = "AUTO"


class StoryRunStage(str, Enum):
    """Commerce 工作流专用阶段，独立于项目级 V1 生产状态。"""

    TOPIC = "TOPIC"
    OUTLINE = "OUTLINE"
    CHAPTERS = "CHAPTERS"
    STORYBOARD = "STORYBOARD"
    VISUAL_ASSETS = "VISUAL_ASSETS"
    VIDEO_PROMPTS = "VIDEO_PROMPTS"
    SEGMENT_RENDER = "SEGMENT_RENDER"
    COMPLETED = "COMPLETED"


class StoryRunStatus(str, Enum):
    """一个选题的独立带货短剧运行状态。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class OutlineVersionStatus(str, Enum):
    """大纲版本的人工确认状态。"""

    DRAFT = "DRAFT"
    LOCKED = "LOCKED"
    SUPERSEDED = "SUPERSEDED"


class SegmentPlanStatus(str, Enum):
    """视频片段规划状态；实际任务仍由 WorkflowRun/WorkflowStep 调度。"""

    DRAFT = "DRAFT"
    READY = "READY"
    RENDERING = "RENDERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ProductPlacementMethod(str, Enum):
    """结构化产品植入方式，避免只用不可检索的自由文本。"""

    SOFT_PROP = "SOFT_PROP"
    EXPERIENCE_DEMO = "EXPERIENCE_DEMO"
    VOICEOVER = "VOICEOVER"
    HYBRID = "HYBRID"


class ProductPlacementStrength(str, Enum):
    """产品植入强度。"""

    LIGHT = "LIGHT"
    MEDIUM = "MEDIUM"
    STRONG = "STRONG"


class RenderBatchStatus(str, Enum):
    """批量片段生成的聚合状态，而非另一套任务调度系统。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Project(Base):
    """一个从参考素材到原创产物的工作空间。"""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    assets: Mapped[list["MediaAsset"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    workflow_runs: Mapped[list["WorkflowRun"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    topic_candidates: Mapped[list["TopicCandidate"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    story_packages: Mapped[list["StoryPackage"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    storyboard_packages: Mapped[list["StoryboardPackage"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    storyboard_images: Mapped[list["StoryboardImage"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    video_clips: Mapped[list["VideoClip"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    final_videos: Mapped[list["FinalVideo"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    # Commerce 运行与项目同生命周期；其产品资产版本仍是共享资产，绝不会被这条
    # 级联关系删除。
    script_assets: Mapped[list["ScriptAsset"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    project_product_selections: Mapped[list["ProjectProductSelection"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    story_runs: Mapped[list["StoryRun"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class MediaAsset(Base):
    """对象存储文件的元信息，不持有二进制内容。"""

    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    kind: Mapped[AssetKind] = mapped_column(SqlEnum(AssetKind), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    project: Mapped[Project] = relationship(back_populates="assets")


# ---------------------------------------------------------------------------
# 带货短剧领域基础。
#
# 以下表只表达新的 Commerce 工作流事实，不会修改 V1 的 ProjectProductionState、
# StoryProposal、ShotPlan 或旧项目数据。产品主体没有 project_id，因此是共享资产；
# 项目和 StoryRun 只保存“采用哪个冻结版本”的引用。
# ---------------------------------------------------------------------------


class ScriptAsset(Base):
    """一个可分析、可版本化的脚本逻辑资产，来源于上传的 ``MediaAsset``。"""

    __tablename__ = "script_assets"
    __table_args__ = (UniqueConstraint("media_asset_id", name="uq_script_asset_media_asset"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    media_asset_id: Mapped[str] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    project: Mapped[Project] = relationship(back_populates="script_assets")
    analyses: Mapped[list["ScriptAnalysisVersion"]] = relationship(
        back_populates="script_asset", cascade="all, delete-orphan", order_by="ScriptAnalysisVersion.version"
    )


class ScriptAnalysisVersion(Base):
    """脚本分析的不可覆盖快照；真实视频分析将在后续阶段填充这些字段。"""

    __tablename__ = "script_analysis_versions"
    __table_args__ = (
        UniqueConstraint("script_asset_id", "version", name="uq_script_analysis_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    script_asset_id: Mapped[str] = mapped_column(
        ForeignKey("script_assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    timeline_transcript: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    story_beats: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    role_archetypes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    conflicts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    turning_points: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    emotional_curve: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    chapter_candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    product_slot_candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    narrative_function_sequence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    raw_analysis: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    analysis_status: Mapped[ScriptAnalysisStatus] = mapped_column(
        SqlEnum(ScriptAnalysisStatus, native_enum=False, create_constraint=True),
        default=ScriptAnalysisStatus.PENDING,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    script_asset: Mapped[ScriptAsset] = relationship(back_populates="analyses")


class ProductAsset(Base):
    """跨项目共享的产品逻辑主体；任何可生产内容都必须引用其冻结版本。"""

    __tablename__ = "product_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    analyses: Mapped[list["ProductAnalysisVersion"]] = relationship(
        back_populates="product_asset", cascade="all, delete-orphan", order_by="ProductAnalysisVersion.version"
    )
    versions: Mapped[list["ProductAssetVersion"]] = relationship(
        back_populates="product_asset", cascade="all, delete-orphan", order_by="ProductAssetVersion.version"
    )


class ProductAnalysisVersion(Base):
    """产品原始视频/素材的分析版本，供人工整理为生产版本。"""

    __tablename__ = "product_analysis_versions"
    __table_args__ = (
        UniqueConstraint("product_asset_id", "version", name="uq_product_analysis_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    product_asset_id: Mapped[str] = mapped_column(
        ForeignKey("product_assets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_media_asset_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_analysis: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    analysis_status: Mapped[ProductAnalysisStatus] = mapped_column(
        SqlEnum(ProductAnalysisStatus, native_enum=False, create_constraint=True),
        default=ProductAnalysisStatus.PENDING,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    product_asset: Mapped[ProductAsset] = relationship(back_populates="analyses")


class ProductAssetVersion(Base):
    """人工确认后可冻结给 StoryRun 使用的产品生产版本。"""

    __tablename__ = "product_asset_versions"
    __table_args__ = (
        UniqueConstraint("product_asset_id", "version", name="uq_product_asset_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    product_asset_id: Mapped[str] = mapped_column(
        ForeignKey("product_assets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_analysis_version_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("product_analysis_versions.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    product_name: Mapped[str] = mapped_column(String(180), nullable=False)
    appearance_description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    selling_points: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    user_pain_points: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    usage_scenarios: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    package_ocr: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    reference_images: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[ProductAssetVersionStatus] = mapped_column(
        SqlEnum(ProductAssetVersionStatus, native_enum=False, create_constraint=True),
        default=ProductAssetVersionStatus.DRAFT,
        nullable=False,
    )
    frozen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    product_asset: Mapped[ProductAsset] = relationship(back_populates="versions")


class ProjectProductSelection(Base):
    """项目采用的具体产品版本；删除项目只删除该引用，不会删除共享产品。"""

    __tablename__ = "project_product_selections"
    __table_args__ = (
        UniqueConstraint("project_id", "product_asset_version_id", name="uq_project_product_version_selection"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_asset_id: Mapped[str] = mapped_column(
        ForeignKey("product_assets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    product_asset_version_id: Mapped[str] = mapped_column(
        ForeignKey("product_asset_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    project: Mapped[Project] = relationship(back_populates="project_product_selections")


class StoryRun(Base):
    """一个选题的一次独立带货短剧运行，冻结其采用的产品版本。"""

    __tablename__ = "story_runs"
    __table_args__ = (
        UniqueConstraint("project_id", "topic_candidate_id", "run_number", name="uq_story_run_topic_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    topic_candidate_id: Mapped[str] = mapped_column(
        ForeignKey("topic_candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_product_selection_id: Mapped[str] = mapped_column(
        ForeignKey("project_product_selections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 此列是运行创建时的冻结指针，不能靠项目当前产品选择倒推。
    product_asset_version_id: Mapped[str] = mapped_column(
        ForeignKey("product_asset_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    run_number: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[StoryRunMode] = mapped_column(
        SqlEnum(StoryRunMode, native_enum=False, create_constraint=True),
        default=StoryRunMode.STEPWISE,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    project: Mapped[Project] = relationship(back_populates="story_runs")
    state: Mapped["StoryRunState"] = relationship(
        back_populates="story_run", cascade="all, delete-orphan", uselist=False
    )
    outlines: Mapped[list["StoryOutlineVersion"]] = relationship(
        back_populates="story_run", cascade="all, delete-orphan", order_by="StoryOutlineVersion.version"
    )
    chapters: Mapped[list["ChapterPlan"]] = relationship(
        back_populates="story_run", cascade="all, delete-orphan", order_by="ChapterPlan.chapter_number"
    )
    scene_mappings: Mapped[list["SceneMappingVersion"]] = relationship(
        back_populates="story_run", cascade="all, delete-orphan", order_by="SceneMappingVersion.version"
    )
    segments: Mapped[list["VideoSegmentPlan"]] = relationship(
        back_populates="story_run", cascade="all, delete-orphan", order_by="VideoSegmentPlan.segment_number"
    )
    placements: Mapped[list["ProductPlacementPlan"]] = relationship(
        back_populates="story_run", cascade="all, delete-orphan"
    )
    render_batches: Mapped[list["RenderBatch"]] = relationship(
        back_populates="story_run", cascade="all, delete-orphan", order_by="RenderBatch.batch_number"
    )


class StoryRunState(Base):
    """Commerce 运行自己的状态机；不读写 ``ProjectProductionState``。"""

    __tablename__ = "story_run_states"

    story_run_id: Mapped[str] = mapped_column(
        ForeignKey("story_runs.id", ondelete="CASCADE"), primary_key=True
    )
    current_stage: Mapped[StoryRunStage] = mapped_column(
        SqlEnum(StoryRunStage, native_enum=False, create_constraint=True),
        default=StoryRunStage.TOPIC,
        nullable=False,
    )
    status: Mapped[StoryRunStatus] = mapped_column(
        SqlEnum(StoryRunStatus, native_enum=False, create_constraint=True),
        default=StoryRunStatus.PENDING,
        nullable=False,
    )
    stage_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    story_run: Mapped[StoryRun] = relationship(back_populates="state")


class StoryOutlineVersion(Base):
    """按 StoryRun 追加保存的故事大纲版本。"""

    __tablename__ = "story_outline_versions"
    __table_args__ = (
        UniqueConstraint("story_run_id", "version", name="uq_story_outline_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    story_run_id: Mapped[str] = mapped_column(
        ForeignKey("story_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    premise: Mapped[str] = mapped_column(Text, nullable=False)
    story_beats: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    product_placement_strategy: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[OutlineVersionStatus] = mapped_column(
        SqlEnum(OutlineVersionStatus, native_enum=False, create_constraint=True),
        default=OutlineVersionStatus.DRAFT,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    story_run: Mapped[StoryRun] = relationship(back_populates="outlines")


class ChapterPlan(Base):
    """一个 StoryRun 的章节规划；章节顺序在同一运行内唯一。"""

    __tablename__ = "chapter_plans"
    __table_args__ = (
        UniqueConstraint("story_run_id", "chapter_number", name="uq_chapter_plan_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    story_run_id: Mapped[str] = mapped_column(
        ForeignKey("story_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    outline_version_id: Mapped[str] = mapped_column(
        ForeignKey("story_outline_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    narrative_purpose: Mapped[str] = mapped_column(Text, nullable=False)
    content_summary: Mapped[str] = mapped_column(Text, nullable=False)
    product_plan: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    story_run: Mapped[StoryRun] = relationship(back_populates="chapters")
    segments: Mapped[list["VideoSegmentPlan"]] = relationship(
        back_populates="chapter", cascade="all, delete-orphan", order_by="VideoSegmentPlan.segment_number"
    )


class SceneMappingVersion(Base):
    """章节、片段与已存在场景资产版本之间的版本化映射快照。

    ``mapping_snapshot`` 的每项可保存 ``chapter_id``、``video_segment_id``、
    ``scene_asset_id``、``scene_asset_version_id`` 等 ID。使用 JSON 快照是为了在本
    阶段不复制 Phase 4 的 ``SceneAssetVersion`` 表，同时冻结未来实际渲染要用的映射。
    """

    __tablename__ = "scene_mapping_versions"
    __table_args__ = (
        UniqueConstraint("story_run_id", "version", name="uq_scene_mapping_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    story_run_id: Mapped[str] = mapped_column(
        ForeignKey("story_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    outline_version_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("story_outline_versions.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    mapping_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    story_run: Mapped[StoryRun] = relationship(back_populates="scene_mappings")


class VideoSegmentPlan(Base):
    """最终渲染为一个独立 MP4 的 4 至 15 秒视频片段规划。"""

    __tablename__ = "video_segment_plans"
    __table_args__ = (
        UniqueConstraint("story_run_id", "segment_number", name="uq_video_segment_plan_number"),
        CheckConstraint(
            "target_duration_ms >= 4000 AND target_duration_ms <= 15000",
            name="ck_video_segment_duration_range",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    story_run_id: Mapped[str] = mapped_column(
        ForeignKey("story_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("chapter_plans.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    segment_number: Mapped[int] = mapped_column(Integer, nullable=False)
    target_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    narrative_target: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[SegmentPlanStatus] = mapped_column(
        SqlEnum(SegmentPlanStatus, native_enum=False, create_constraint=True),
        default=SegmentPlanStatus.DRAFT,
        nullable=False,
    )
    video_prompt_version: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    video_prompt_trace: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    story_run: Mapped[StoryRun] = relationship(back_populates="segments")
    chapter: Mapped[ChapterPlan] = relationship(back_populates="segments")
    sub_shots: Mapped[list["SubShotPlan"]] = relationship(
        back_populates="video_segment", cascade="all, delete-orphan", order_by="SubShotPlan.shot_number"
    )
    dialogue_lines: Mapped[list["DialogueLine"]] = relationship(
        back_populates="video_segment", cascade="all, delete-orphan"
    )


class SubShotPlan(Base):
    """片段内部子镜头，时间以片段起点为零的毫秒相对时间保存。"""

    __tablename__ = "sub_shot_plans"
    __table_args__ = (
        UniqueConstraint("video_segment_id", "shot_number", name="uq_sub_shot_plan_number"),
        CheckConstraint("start_ms >= 0", name="ck_sub_shot_start_nonnegative"),
        CheckConstraint("end_ms > start_ms", name="ck_sub_shot_end_after_start"),
        CheckConstraint("end_ms <= 15000", name="ck_sub_shot_end_maximum"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    video_segment_id: Mapped[str] = mapped_column(
        ForeignKey("video_segment_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    character_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    emotion: Mapped[str] = mapped_column(Text, nullable=False)
    shot_scale: Mapped[str] = mapped_column(String(80), nullable=False)
    camera_move: Mapped[str] = mapped_column(Text, nullable=False)
    lighting: Mapped[str] = mapped_column(Text, nullable=False)
    visual_description: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    video_segment: Mapped[VideoSegmentPlan] = relationship(back_populates="sub_shots")
    dialogue_lines: Mapped[list["DialogueLine"]] = relationship(
        back_populates="sub_shot", cascade="all, delete-orphan"
    )


class DialogueLine(Base):
    """可单独查询和计时的对白；必须归属片段或具体子镜头中的一个。"""

    __tablename__ = "dialogue_lines"
    __table_args__ = (
        CheckConstraint(
            "(video_segment_id IS NOT NULL AND sub_shot_id IS NULL) OR "
            "(video_segment_id IS NULL AND sub_shot_id IS NOT NULL)",
            name="ck_dialogue_line_single_owner",
        ),
        CheckConstraint("start_ms >= 0", name="ck_dialogue_line_start_nonnegative"),
        CheckConstraint("end_ms > start_ms", name="ck_dialogue_line_end_after_start"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    video_segment_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("video_segment_plans.id", ondelete="CASCADE"), nullable=True, index=True
    )
    sub_shot_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("sub_shot_plans.id", ondelete="CASCADE"), nullable=True, index=True
    )
    speaker: Mapped[str] = mapped_column(String(160), nullable=False)
    dialogue: Mapped[str] = mapped_column(Text, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    video_segment: Mapped[Optional[VideoSegmentPlan]] = relationship(back_populates="dialogue_lines")
    sub_shot: Mapped[Optional[SubShotPlan]] = relationship(back_populates="dialogue_lines")


class ProductPlacementPlan(Base):
    """StoryRun 中对冻结产品版本的结构化植入计划。"""

    __tablename__ = "product_placement_plans"
    __table_args__ = (
        CheckConstraint(
            "chapter_id IS NOT NULL OR video_segment_id IS NOT NULL OR sub_shot_id IS NOT NULL",
            name="ck_product_placement_has_location",
        ),
        CheckConstraint("planned_duration_ms >= 0", name="ck_product_placement_duration_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    story_run_id: Mapped[str] = mapped_column(
        ForeignKey("story_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_asset_version_id: Mapped[str] = mapped_column(
        ForeignKey("product_asset_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    chapter_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("chapter_plans.id", ondelete="CASCADE"), nullable=True, index=True
    )
    video_segment_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("video_segment_plans.id", ondelete="CASCADE"), nullable=True, index=True
    )
    sub_shot_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("sub_shot_plans.id", ondelete="CASCADE"), nullable=True, index=True
    )
    placement_method: Mapped[ProductPlacementMethod] = mapped_column(
        SqlEnum(ProductPlacementMethod, native_enum=False, create_constraint=True), nullable=False
    )
    placement_strength: Mapped[ProductPlacementStrength] = mapped_column(
        SqlEnum(ProductPlacementStrength, native_enum=False, create_constraint=True), nullable=False
    )
    pain_point_trigger: Mapped[str] = mapped_column(Text, nullable=False)
    product_action: Mapped[str] = mapped_column(Text, nullable=False)
    ad_entry_point: Mapped[str] = mapped_column(Text, nullable=False)
    story_recovery_point: Mapped[str] = mapped_column(Text, nullable=False)
    planned_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    story_run: Mapped[StoryRun] = relationship(back_populates="placements")


class RenderBatch(Base):
    """一次批量视频片段生成的聚合记录，实际子任务仍复用既有 WorkflowRun/Step。"""

    __tablename__ = "render_batches"
    __table_args__ = (
        UniqueConstraint("story_run_id", "batch_number", name="uq_render_batch_number"),
        CheckConstraint("total_tasks >= 0", name="ck_render_batch_total_nonnegative"),
        CheckConstraint("completed_tasks >= 0", name="ck_render_batch_completed_nonnegative"),
        CheckConstraint("failed_tasks >= 0", name="ck_render_batch_failed_nonnegative"),
        CheckConstraint("running_tasks >= 0", name="ck_render_batch_running_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    story_run_id: Mapped[str] = mapped_column(
        ForeignKey("story_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workflow_run_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    batch_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[RenderBatchStatus] = mapped_column(
        SqlEnum(RenderBatchStatus, native_enum=False, create_constraint=True),
        default=RenderBatchStatus.PENDING,
        nullable=False,
    )
    total_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    running_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    generation_parameters_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    estimated_cost: Mapped[Optional[float]] = mapped_column(Numeric(14, 6), nullable=True)
    currency: Mapped[str] = mapped_column(String(12), nullable=False, default="CNY")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    story_run: Mapped[StoryRun] = relationship(back_populates="render_batches")


# ---------------------------------------------------------------------------
# Phase 4 资产中心。
#
# 资产中心不取代 V1 原有的 CharacterDefinition / SceneDefinition：前者是跨项目可
# 复用的版本资产，后者仍是“本故事里的角色/场景语义”。项目引用表把两者连接起来，
# 使既有工作流、审核与冻结机制不需要重写。
# ---------------------------------------------------------------------------


class AssetLibrary(Base):
    """角色或场景资产的逻辑资料库。

    V1 默认提供一套角色库和一套场景库；未来多团队、品牌项目可以新增更多资料库，
    但版本内容始终保存在下方的具体 AssetVersion 表中。
    """

    __tablename__ = "asset_libraries"
    __table_args__ = (UniqueConstraint("kind", "name", name="uq_asset_library_kind_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[AssetLibraryKind] = mapped_column(SqlEnum(AssetLibraryKind), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class CharacterAsset(Base):
    """可跨项目复用的角色资产主体；具体内容只追加到版本表。"""

    __tablename__ = "character_assets"
    __table_args__ = (UniqueConstraint("library_id", "name", name="uq_character_asset_library_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    library_id: Mapped[str] = mapped_column(ForeignKey("asset_libraries.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class CharacterAssetVersion(Base):
    """角色资产的不可变版本，``reference_images`` 支持正侧全身和表情多视图。"""

    __tablename__ = "character_asset_versions"
    __table_args__ = (UniqueConstraint("character_asset_id", "version", name="uq_character_asset_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    character_asset_id: Mapped[str] = mapped_column(ForeignKey("character_assets.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    age: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    gender: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    personality: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    style: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    appearance: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    costume: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # [{"view": "front|side|full_body|expression", "url": "...", "label": "..."}]
    # JSON 允许后续接入对象存储 ID、版权来源和审核信息，而不破坏既有版本。
    reference_images: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SceneAsset(Base):
    """可跨项目复用的场景资产主体；具体内容只追加到版本表。"""

    __tablename__ = "scene_assets"
    __table_args__ = (UniqueConstraint("library_id", "name", name="uq_scene_asset_library_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    library_id: Mapped[str] = mapped_column(ForeignKey("asset_libraries.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class SceneAssetVersion(Base):
    """场景资产的不可变版本，保存环境风格、天气、时段和多张参考图。"""

    __tablename__ = "scene_asset_versions"
    __table_args__ = (UniqueConstraint("scene_asset_id", "version", name="uq_scene_asset_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scene_asset_id: Mapped[str] = mapped_column(ForeignKey("scene_assets.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    style: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    weather: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    time_of_day: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    location: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    environment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    mood: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reference_images: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ModelProfile(Base):
    """可替换模型的无密钥配置快照。

    `provider_config` 只能保存模型名、超时、采样参数等非敏感字段；真正 Key
    由部署环境中的 secret reference 注入对应适配器。
    """

    __tablename__ = "model_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    step_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    provider_key: Mapped[str] = mapped_column(String(80), nullable=False)
    model_key: Mapped[str] = mapped_column(String(160), nullable=False)
    # 旧 provider_key/model_key 继续保留给历史项目；V1 用以下字段表达适配器和模型版本。
    adapter_key: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    model_version: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # V1 以版本化配置替代原地覆盖：新建或复制的候选是 DRAFT，绑定启用后为 ACTIVE，
    # 已被替换的旧版本保留为 HISTORICAL。是否允许编辑仍须以 ModelInvocation 为准。
    profile_status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")
    provider_config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    evaluations: Mapped[list["ModelEvaluation"]] = relationship(
        back_populates="model_profile", cascade="all, delete-orphan", order_by="ModelEvaluation.created_at.desc()"
    )


class ModelEvaluation(Base):
    """人工记录的一次模型小样本验收汇总。

    它记录的是一组可重复的测试统计，不保存参考视频、模型原始输出或任何密钥。成本
    以人民币元为单位保留四位小数，便于比较不同中转站、不同版本的成功率、耗时和质量。
    """

    __tablename__ = "model_evaluations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    model_profile_id: Mapped[str] = mapped_column(ForeignKey("model_profiles.id"), nullable=False, index=True)
    scenario: Mapped[str] = mapped_column(String(120), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_cost_yuan: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    average_latency_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    quality_score: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    model_profile: Mapped[ModelProfile] = relationship(back_populates="evaluations")


class CreativeLibraryItem(Base):
    """可人工维护、可被选题生成引用的抽象创作资产。"""

    __tablename__ = "creative_library_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[LibraryItemKind] = mapped_column(SqlEnum(LibraryItemKind), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    group_name: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    source: Mapped[LibraryItemSource] = mapped_column(
        SqlEnum(LibraryItemSource), nullable=False, default=LibraryItemSource.MANUAL
    )
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class WorkflowRun(Base):
    """一次完整工作流运行，聚合其下各步骤的总体状态。"""

    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index(
            "uq_v1_active_run_project_key",
            "project_id",
            "workflow_key",
            unique=True,
            postgresql_where=text("workflow_key LIKE 'v1_%' AND status IN ('PENDING', 'RUNNING')"),
            sqlite_where=text("workflow_key LIKE 'v1_%' AND status IN ('PENDING', 'RUNNING')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    workflow_key: Mapped[str] = mapped_column(String(80), nullable=False)
    # 外键由 0003 增量迁移补充。这里不直接声明 ForeignKey，避免 0001 历史基线在
    # 新建数据库时先创建 workflow_runs 却尚未创建 workflow_definitions。
    workflow_definition_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    workflow_version: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    # 由创建请求确定的幂等键。真正的唯一性由 0007 的“活动任务部分唯一索引”保证，
    # 历史运行可保留相同语义键，供制作人追溯每次人工重做。
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(160), nullable=True, index=True)
    input_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    status: Mapped[RunStatus] = mapped_column(SqlEnum(RunStatus), default=RunStatus.PENDING, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship(back_populates="workflow_runs")
    steps: Mapped[list["WorkflowStep"]] = relationship(
        back_populates="workflow_run", cascade="all, delete-orphan", order_by="WorkflowStep.position"
    )


class WorkflowStep(Base):
    """可观察、可重试的一个工作流节点。"""

    __tablename__ = "workflow_steps"
    __table_args__ = (
        Index(
            "uq_workflow_steps_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workflow_run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False, index=True)
    step_key: Mapped[str] = mapped_column(String(80), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[RunStatus] = mapped_column(SqlEnum(RunStatus), default=RunStatus.PENDING, nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    output_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model_profile_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    # 视频阶段会为每个 ShotPlan 建立独立子步骤。provider_task_id 一旦写入，只能继续
    # 查询，不得因 Worker 重启而重复提交可能收费的供应商任务。
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    shot_plan_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    video_clip_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    provider_task_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    workflow_run: Mapped[WorkflowRun] = relationship(back_populates="steps")


class TopicCandidate(Base):
    """一次选题生成得到的原创候选。

    候选保留生成任务 ID 与模型/分析结果的追溯关系；确认哪个选题由人工完成，
    不能由模型自动替用户做内容决策。
    """

    __tablename__ = "topic_candidates"
    __table_args__ = (UniqueConstraint("generation_run_id", "position", name="uq_topic_candidate_run_position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    generation_run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    opening_hook: Mapped[str] = mapped_column(Text, nullable=False)
    synopsis: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    scoring_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[TopicStatus] = mapped_column(SqlEnum(TopicStatus), default=TopicStatus.DRAFT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    project: Mapped[Project] = relationship(back_populates="topic_candidates")


class StoryPackage(Base):
    """一个选题生成的一版故事大纲、角色卡和场景卡快照。"""

    __tablename__ = "story_packages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    topic_candidate_id: Mapped[str] = mapped_column(ForeignKey("topic_candidates.id"), nullable=False, index=True)
    generation_run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    premise: Mapped[str] = mapped_column(Text, nullable=False)
    outline: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    roles: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    scenes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[StoryStatus] = mapped_column(SqlEnum(StoryStatus), default=StoryStatus.DRAFT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    project: Mapped[Project] = relationship(back_populates="story_packages")

class StoryboardPackage(Base):
    """一版可审阅的镜头细纲；镜头以 JSON 保存，后续可拆成独立镜头表。"""
    __tablename__ = "storyboard_packages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    story_package_id: Mapped[str] = mapped_column(ForeignKey("story_packages.id"), nullable=False, index=True)
    generation_run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False, index=True)
    target_shot_count: Mapped[int] = mapped_column(Integer, nullable=False)
    shots: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[StoryboardStatus] = mapped_column(SqlEnum(StoryboardStatus), default=StoryboardStatus.DRAFT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    project: Mapped[Project] = relationship(back_populates="storyboard_packages")

class StoryboardImage(Base):
    """一个镜头的一版图片结果；同一镜头可保留多个版本以方便人工比较。"""
    __tablename__ = "storyboard_images"
    __table_args__ = (UniqueConstraint("storyboard_package_id", "shot_number", "version", name="uq_storyboard_image_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    storyboard_package_id: Mapped[str] = mapped_column(ForeignKey("storyboard_packages.id"), nullable=False, index=True)
    generation_run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False, index=True)
    shot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[ImageStatus] = mapped_column(SqlEnum(ImageStatus), default=ImageStatus.PENDING, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    project: Mapped[Project] = relationship(back_populates="storyboard_images")


class VideoClip(Base):
    """一组连续镜头生成的一版短视频片段。

    `image_ids` 冻结本次渲染所采用的图片版本，`prompt` 冻结组装后的模型输入。
    后续替换模型、调整镜头分组或重跑某一组时，都不会改写旧版本的审计记录。
    """

    __tablename__ = "video_clips"
    __table_args__ = (
        UniqueConstraint(
            "storyboard_package_id",
            "group_number",
            "version",
            name="uq_video_clip_group_version",
        ),
        Index(
            "uq_video_clips_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    storyboard_package_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("storyboard_packages.id"), nullable=True, index=True
    )
    # V1 以一个导演分镜生成一个视频片段；旧 storyboard_package_id 保留历史兼容。
    shot_plan_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    model_invocation_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    generation_run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False, index=True)
    group_number: Mapped[int] = mapped_column(Integer, nullable=False)
    start_shot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    end_shot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    shots_per_group: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    image_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    video_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    provider_task_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # 同一收费调用的追溯键，与 WorkflowStep / ModelInvocation 对齐。空值保留给迁移
    # 前历史片段，绝不覆盖既有版本。
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    status: Mapped[VideoClipStatus] = mapped_column(
        SqlEnum(VideoClipStatus), default=VideoClipStatus.PENDING, nullable=False
    )
    # 旧表通过增量迁移补列，因此使用受服务层校验的字符串以兼容已存在的 PostgreSQL
    # 枚举类型和 SQLite 本地库；V1 新表才使用专属枚举。
    generation_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    review_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    input_asset_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    project: Mapped[Project] = relationship(back_populates="video_clips")


class FinalVideo(Base):
    """一版由人工可见片段顺序合成的完整成片。

    ``clip_ids`` 是严格有序的输入快照，避免后续重做片段或改变默认分组时改写已经
    导出的内容。``storage_key`` 为本地/对象存储内部键，浏览器只能使用受项目范围
    校验的下载接口，不能获取底层存储路径。
    """

    __tablename__ = "final_videos"
    __table_args__ = (
        UniqueConstraint("storyboard_package_id", "version", name="uq_final_video_board_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    storyboard_package_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("storyboard_packages.id"), nullable=True, index=True
    )
    director_plan_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    workflow_definition_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    workflow_version: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    generation_run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    clip_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    approved_clip_ids: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    input_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    output_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    storage_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True, unique=True)
    status: Mapped[FinalVideoStatus] = mapped_column(
        SqlEnum(FinalVideoStatus), default=FinalVideoStatus.PENDING, nullable=False
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship(back_populates="final_videos")


# ---------------------------------------------------------------------------
# LemonFlow V1 生产链路实体。
# 旧选题/故事包/单镜图片表继续保留给历史项目，但新项目只使用以下实体。
# ---------------------------------------------------------------------------


class WorkflowDefinition(Base):
    """一个不可变的工作流定义版本，例如 ``LemonFlow_V1``。"""

    __tablename__ = "workflow_definitions"
    __table_args__ = (UniqueConstraint("workflow_code", "version", name="uq_workflow_definition_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workflow_code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(80), nullable=False)
    definition_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[WorkflowDefinitionStatus] = mapped_column(
        SqlEnum(WorkflowDefinitionStatus), default=WorkflowDefinitionStatus.DRAFT, nullable=False
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ProjectProductionState(Base):
    """项目 V1 主流程的当前指针。

    这些指针只指向人工已确认的对象。外键在迁移中通过延迟约束处理，避免角色图、
    场景图和关键帧的版本表形成建表循环。
    """

    __tablename__ = "project_production_states"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    active_stage: Mapped[ProductionStage] = mapped_column(
        SqlEnum(ProductionStage), default=ProductionStage.REFERENCE_ANALYSIS, nullable=False
    )
    workflow_definition_id: Mapped[str] = mapped_column(ForeignKey("workflow_definitions.id"), nullable=False)
    locked_reference_analysis_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("reference_analyses.id", use_alter=True, name="fk_state_locked_analysis"), nullable=True
    )
    selected_story_proposal_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("story_proposals.id", use_alter=True, name="fk_state_selected_story"), nullable=True
    )
    director_plan_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("director_plans.id", use_alter=True, name="fk_state_director_plan"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ReferenceAnalysis(Base):
    """Gemini 等视频分析模型输出的五类可审核创作结果。"""

    __tablename__ = "reference_analyses"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_reference_analysis_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    workflow_run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    video_script_structure: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    opening_analysis: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    viral_elements: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    scene_analysis: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    creative_brief: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    generation_status: Mapped[RunStatus] = mapped_column(SqlEnum(RunStatus), default=RunStatus.PENDING, nullable=False)
    review_status: Mapped[ReviewStatus] = mapped_column(
        SqlEnum(ReviewStatus), default=ReviewStatus.PENDING_REVIEW, nullable=False
    )
    # LOCKED 时保存完整快照，保证后续模板或展示字段升级不影响历史创作输入。
    locked_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ReviewDecision(Base):
    """不依赖登录系统的通用人工审核审计事件。

    ``quality_score`` 是制作人对本次生成结果的主观质量评分（1 至 10 分）。它和
    “是否采用”是两个独立信号：例如一个 8 分的故事也可能因项目方向不匹配而未被选中。
    """

    __tablename__ = "review_decisions"
    __table_args__ = (Index("ix_review_decision_target", "target_type", "target_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(60), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    reviewer_label: Mapped[str] = mapped_column(String(120), nullable=False, default="人工审核")
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    quality_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class StoryGenerationBatch(Base):
    """对同一锁定创作简报并行调用多个故事模型的一次批次。"""

    __tablename__ = "story_generation_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    reference_analysis_id: Mapped[str] = mapped_column(ForeignKey("reference_analyses.id"), nullable=False, index=True)
    workflow_run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False, index=True)
    request_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[RunStatus] = mapped_column(SqlEnum(RunStatus), default=RunStatus.PENDING, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class StoryProposal(Base):
    """一个模型在故事生成批次内给出的原创故事候选。"""

    __tablename__ = "story_proposals"
    __table_args__ = (
        UniqueConstraint("batch_id", "candidate_number", name="uq_story_proposal_batch_number"),
        # 一个生成批次只能选中一份故事；新批次可保留历史已选方案并产生新的选择。
        # 项目当前采用哪一份由 ProjectProductionState 的指针冻结，不能用“每项目
        # 只有一个 SELECTED”限制版本迭代。
        Index(
            "uq_selected_story_per_batch",
            "batch_id",
            unique=True,
            postgresql_where=text("status = 'SELECTED'"),
            sqlite_where=text("status = 'SELECTED'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(ForeignKey("story_generation_batches.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    model_invocation_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("model_invocations.id", use_alter=True, name="fk_story_invocation"), nullable=True
    )
    candidate_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[StoryProposalStatus] = mapped_column(
        SqlEnum(StoryProposalStatus), default=StoryProposalStatus.CANDIDATE, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DirectorPlan(Base):
    """已选故事对应的角色、场景和分镜视觉总方案。"""

    __tablename__ = "director_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    story_proposal_id: Mapped[str] = mapped_column(ForeignKey("story_proposals.id"), nullable=False, index=True)
    workflow_run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False, index=True)
    visual_bible: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[DirectorPlanStatus] = mapped_column(
        SqlEnum(DirectorPlanStatus), default=DirectorPlanStatus.PENDING, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class CharacterDefinition(Base):
    """角色文字设计；它在导演分镜前由已选故事派生。"""

    __tablename__ = "character_definitions"
    __table_args__ = (UniqueConstraint("story_proposal_id", "character_code", name="uq_character_story_code"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 角色设计顺序在导演分镜之前，故主归属是已选择的故事，而不是 DirectorPlan。
    story_proposal_id: Mapped[str] = mapped_column(ForeignKey("story_proposals.id"), nullable=False, index=True)
    # 仅为 0003 旧草稿数据兼容保留；新 V1 不写入这个字段。
    legacy_director_plan_id: Mapped[Optional[str]] = mapped_column(
        "director_plan_id", ForeignKey("director_plans.id"), nullable=True, index=True
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    # 指向资产中心中的角色主体。该字段不会替代项目内角色语义，而是让此角色可以
    # 追溯到可跨项目复用的资产版本；旧项目允许为空并在首次继续生产时懒迁移。
    asset_library_character_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("character_assets.id"), nullable=True, index=True
    )
    character_code: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    age_description: Mapped[str] = mapped_column(String(120), nullable=False)
    appearance: Mapped[str] = mapped_column(Text, nullable=False)
    costume: Mapped[str] = mapped_column(Text, nullable=False)
    temperament: Mapped[str] = mapped_column(Text, nullable=False)
    design_status: Mapped[DesignStatus] = mapped_column(SqlEnum(DesignStatus), default=DesignStatus.DRAFT, nullable=False)
    locked_reference_image_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("character_reference_images.id", use_alter=True, name="fk_character_locked_image"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class SceneDefinition(Base):
    """场景文字设计；它在导演分镜前由已选故事派生。"""

    __tablename__ = "scene_definitions"
    __table_args__ = (UniqueConstraint("story_proposal_id", "scene_code", name="uq_scene_story_code"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    story_proposal_id: Mapped[str] = mapped_column(ForeignKey("story_proposals.id"), nullable=False, index=True)
    # 仅保留给升级前误关联到 DirectorPlan 的历史草稿数据；新 V1 不使用。
    legacy_director_plan_id: Mapped[Optional[str]] = mapped_column(
        "director_plan_id", ForeignKey("director_plans.id"), nullable=True, index=True
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    # 与角色同理：项目场景语义保持原表，资产中心保存可复用的版本实体。
    asset_library_scene_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("scene_assets.id"), nullable=True, index=True
    )
    scene_code: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    location: Mapped[str] = mapped_column(Text, nullable=False)
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    visual_style: Mapped[str] = mapped_column(Text, nullable=False)
    mood: Mapped[str] = mapped_column(Text, nullable=False)
    design_status: Mapped[DesignStatus] = mapped_column(SqlEnum(DesignStatus), default=DesignStatus.DRAFT, nullable=False)
    locked_reference_image_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("scene_reference_images.id", use_alter=True, name="fk_scene_locked_image"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class CharacterReferenceImage(Base):
    """角色参考图的不可覆盖版本；角色指针选择当前采用版本。"""

    __tablename__ = "character_reference_images"
    __table_args__ = (UniqueConstraint("character_id", "version", name="uq_character_reference_image_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    character_id: Mapped[str] = mapped_column(ForeignKey("character_definitions.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    generation_run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False, index=True)
    model_invocation_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("model_invocations.id", use_alter=True, name="fk_character_image_invocation"), nullable=True
    )
    # 对应资产中心的不可变角色版本。一张生成图默认作为 full_body 参考；人工可在
    # 资产中心基于同一角色追加正面、侧面、表情等完整版本，而不会覆盖此历史图片。
    asset_version_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("character_asset_versions.id"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    generation_status: Mapped[RunStatus] = mapped_column(SqlEnum(RunStatus), default=RunStatus.PENDING, nullable=False)
    review_status: Mapped[ReviewStatus] = mapped_column(
        SqlEnum(ReviewStatus), default=ReviewStatus.PENDING_REVIEW, nullable=False
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SceneReferenceImage(Base):
    """场景参考图的不可覆盖版本；场景指针选择当前采用版本。"""

    __tablename__ = "scene_reference_images"
    __table_args__ = (UniqueConstraint("scene_id", "version", name="uq_scene_reference_image_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scene_id: Mapped[str] = mapped_column(ForeignKey("scene_definitions.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    generation_run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False, index=True)
    model_invocation_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("model_invocations.id", use_alter=True, name="fk_scene_image_invocation"), nullable=True
    )
    asset_version_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("scene_asset_versions.id"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    generation_status: Mapped[RunStatus] = mapped_column(SqlEnum(RunStatus), default=RunStatus.PENDING, nullable=False)
    review_status: Mapped[ReviewStatus] = mapped_column(
        SqlEnum(ReviewStatus), default=ReviewStatus.PENDING_REVIEW, nullable=False
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ProjectCharacterAssetReference(Base):
    """项目角色对资产中心某个角色版本的明确采用记录。

    ``is_selected`` 是项目内的当前采用指针；取消采用或改选只改变引用记录，绝不修改
    CharacterAssetVersion 的内容，从而保留过去 WorkflowRun 的可复现性。
    """

    __tablename__ = "project_character_asset_references"
    __table_args__ = (
        UniqueConstraint(
            "character_definition_id",
            "character_asset_version_id",
            name="uq_project_character_asset_version_reference",
        ),
        Index(
            "uq_selected_project_character_asset",
            "character_definition_id",
            unique=True,
            postgresql_where=text("is_selected = true"),
            sqlite_where=text("is_selected = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    character_definition_id: Mapped[str] = mapped_column(ForeignKey("character_definitions.id"), nullable=False, index=True)
    character_asset_id: Mapped[str] = mapped_column(ForeignKey("character_assets.id"), nullable=False, index=True)
    character_asset_version_id: Mapped[str] = mapped_column(
        ForeignKey("character_asset_versions.id"), nullable=False, index=True
    )
    source_reference_image_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("character_reference_images.id"), nullable=True, index=True
    )
    is_selected: Mapped[bool] = mapped_column(default=False, nullable=False)
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ProjectSceneAssetReference(Base):
    """项目场景对资产中心某个场景版本的明确采用记录。"""

    __tablename__ = "project_scene_asset_references"
    __table_args__ = (
        UniqueConstraint(
            "scene_definition_id",
            "scene_asset_version_id",
            name="uq_project_scene_asset_version_reference",
        ),
        Index(
            "uq_selected_project_scene_asset",
            "scene_definition_id",
            unique=True,
            postgresql_where=text("is_selected = true"),
            sqlite_where=text("is_selected = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    scene_definition_id: Mapped[str] = mapped_column(ForeignKey("scene_definitions.id"), nullable=False, index=True)
    scene_asset_id: Mapped[str] = mapped_column(ForeignKey("scene_assets.id"), nullable=False, index=True)
    scene_asset_version_id: Mapped[str] = mapped_column(
        ForeignKey("scene_asset_versions.id"), nullable=False, index=True
    )
    source_reference_image_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("scene_reference_images.id"), nullable=True, index=True
    )
    is_selected: Mapped[bool] = mapped_column(default=False, nullable=False)
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ShotPlan(Base):
    """AI 导演定义的一个镜头；只引用已锁定的角色与场景资产。"""

    __tablename__ = "shot_plans"
    __table_args__ = (UniqueConstraint("director_plan_id", "shot_number", name="uq_shot_plan_number"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    director_plan_id: Mapped[str] = mapped_column(ForeignKey("director_plans.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    shot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    action_description: Mapped[str] = mapped_column(Text, nullable=False)
    camera_description: Mapped[str] = mapped_column(Text, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    video_action_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # Phase 4 导演方案使用可被图片、视频和声音步骤直接消费的结构化字段。旧字段保留
    # 作为兼容和可读摘要，所有新增字段都有迁移默认值以不破坏已经存在的导演方案。
    emotion: Mapped[str] = mapped_column(Text, nullable=False, default="未指定")
    camera_type: Mapped[str] = mapped_column(String(120), nullable=False, default="中景")
    camera_move: Mapped[str] = mapped_column(Text, nullable=False, default="固定机位")
    lighting: Mapped[str] = mapped_column(Text, nullable=False, default="自然光")
    image_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    video_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sound_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    locked_keyframe_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("shot_keyframes.id", use_alter=True, name="fk_shot_locked_keyframe"), nullable=True
    )
    # 当前采用的视频版本是显式指针，不以“最新一版”推断。历史 REJECTED / 已替换
    # 版本永久保留，但不会被审核闸门或成片合成读取。
    selected_video_clip_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ShotAssetBinding(Base):
    """一个分镜与其锁定角色图、锁定场景图的明确关联。"""

    __tablename__ = "shot_asset_bindings"
    __table_args__ = (
        CheckConstraint(
            "character_id IS NULL OR character_reference_image_id IS NOT NULL",
            name="ck_shot_character_image_required",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    shot_id: Mapped[str] = mapped_column(ForeignKey("shot_plans.id"), nullable=False, index=True)
    character_id: Mapped[Optional[str]] = mapped_column(ForeignKey("character_definitions.id"), nullable=True)
    character_reference_image_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("character_reference_images.id"), nullable=True
    )
    # 角色/场景图是具体项目锁图，资产中心版本则是跨项目可复用资产的冻结身份。二者
    # 同时记录，令任意镜头和视频片段都能回答“使用了哪个资产版本”。
    character_asset_version_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("character_asset_versions.id"), nullable=True, index=True
    )
    scene_id: Mapped[str] = mapped_column(ForeignKey("scene_definitions.id"), nullable=False)
    scene_reference_image_id: Mapped[str] = mapped_column(ForeignKey("scene_reference_images.id"), nullable=False)
    scene_asset_version_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("scene_asset_versions.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ShotKeyframe(Base):
    """一个分镜关键画面的不可覆盖版本，镜头指针选择当前采用版本。"""

    __tablename__ = "shot_keyframes"
    __table_args__ = (UniqueConstraint("shot_id", "version", name="uq_shot_keyframe_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    shot_id: Mapped[str] = mapped_column(ForeignKey("shot_plans.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    generation_run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False, index=True)
    model_invocation_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("model_invocations.id", use_alter=True, name="fk_keyframe_invocation"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    input_asset_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    generation_status: Mapped[RunStatus] = mapped_column(SqlEnum(RunStatus), default=RunStatus.PENDING, nullable=False)
    review_status: Mapped[ReviewStatus] = mapped_column(
        SqlEnum(ReviewStatus), default=ReviewStatus.PENDING_REVIEW, nullable=False
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ModelSlot(Base):
    """业务能力槽位，不等同于某个具体模型或供应商。"""

    __tablename__ = "model_slots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    slot_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    capability: Mapped[str] = mapped_column(String(80), nullable=False)
    selection_mode: Mapped[ModelSelectionMode] = mapped_column(
        SqlEnum(ModelSelectionMode), default=ModelSelectionMode.SINGLE, nullable=False
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ModelSlotProfileBinding(Base):
    """模型槽位和模型配置之间的启用、优先级、权重关系。"""

    __tablename__ = "model_slot_profile_bindings"
    __table_args__ = (UniqueConstraint("slot_id", "model_profile_id", name="uq_slot_profile_binding"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    slot_id: Mapped[str] = mapped_column(ForeignKey("model_slots.id"), nullable=False, index=True)
    model_profile_id: Mapped[str] = mapped_column(ForeignKey("model_profiles.id"), nullable=False, index=True)
    is_enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    weight: Mapped[Optional[float]] = mapped_column(Numeric(8, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PromptTemplate(Base):
    """可版本化且可校验变量的生产 Prompt 模板。"""

    __tablename__ = "prompt_templates"
    __table_args__ = (UniqueConstraint("task_type", "name", "version", name="uq_prompt_template_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    variables_schema: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[PromptTemplateStatus] = mapped_column(
        SqlEnum(PromptTemplateStatus), default=PromptTemplateStatus.DRAFT, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ModelInvocation(Base):
    """一次真实或模拟模型调用的脱敏、可重放审计记录。"""

    __tablename__ = "model_invocations"
    __table_args__ = (
        Index("ix_model_invocation_profile_task", "model_profile_id", "task_type", "created_at"),
        Index("ix_model_invocation_project", "project_id", "created_at"),
        Index(
            "uq_model_invocations_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    workflow_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("workflow_runs.id"), nullable=True, index=True)
    workflow_step_id: Mapped[Optional[str]] = mapped_column(ForeignKey("workflow_steps.id"), nullable=True, index=True)
    model_slot_id: Mapped[str] = mapped_column(ForeignKey("model_slots.id"), nullable=False, index=True)
    model_profile_id: Mapped[str] = mapped_column(ForeignKey("model_profiles.id"), nullable=False, index=True)
    prompt_template_id: Mapped[Optional[str]] = mapped_column(ForeignKey("prompt_templates.id"), nullable=True, index=True)
    task_type: Mapped[str] = mapped_column(String(80), nullable=False)
    model_profile_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    prompt_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    output_reference: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    provider_task_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    media_units: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    cost_amount: Mapped[Optional[float]] = mapped_column(Numeric(14, 6), nullable=True)
    currency: Mapped[str] = mapped_column(String(12), nullable=False, default="CNY")
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[RunStatus] = mapped_column(SqlEnum(RunStatus), default=RunStatus.PENDING, nullable=False)
    error_code: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ModelQualityEvaluation(Base):
    """按模型、Prompt、任务和场景聚合的质量快照；V1 只用于人工决策。

    每次刷新会新增一条不可变快照，绝不覆盖原有报表。因此后续可以按当时的模型、
    Prompt 和审核数据回溯“为何建议人工考虑某个候选”。
    """

    __tablename__ = "model_quality_evaluations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    model_profile_id: Mapped[str] = mapped_column(ForeignKey("model_profiles.id"), nullable=False, index=True)
    prompt_template_id: Mapped[Optional[str]] = mapped_column(ForeignKey("prompt_templates.id"), nullable=True, index=True)
    task_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    scenario: Mapped[str] = mapped_column(String(160), nullable=False)
    aggregation_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    aggregation_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False)
    success_rate: Mapped[float] = mapped_column(Numeric(8, 4), nullable=False)
    average_cost_amount: Mapped[Optional[float]] = mapped_column(Numeric(14, 6), nullable=True)
    currency: Mapped[str] = mapped_column(String(12), nullable=False, default="CNY")
    average_latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    average_human_score: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    adoption_rate: Mapped[Optional[float]] = mapped_column(Numeric(8, 4), nullable=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="AUTO_AGGREGATED")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class VideoClipAssetBinding(Base):
    """视频片段使用的锁图、关键帧及资产中心版本外键。

    项目参考图保证本次视频生成的实际输入可下载；资产版本字段保证生产审计可跨项目
    追溯角色/场景资产的版本来源。
    """

    __tablename__ = "video_clip_asset_bindings"
    __table_args__ = (
        CheckConstraint(
            "(asset_type = 'CHARACTER_REFERENCE' AND character_reference_image_id IS NOT NULL "
            "AND scene_reference_image_id IS NULL AND shot_keyframe_id IS NULL) OR "
            "(asset_type = 'SCENE_REFERENCE' AND character_reference_image_id IS NULL "
            "AND scene_reference_image_id IS NOT NULL AND shot_keyframe_id IS NULL) OR "
            "(asset_type = 'SHOT_KEYFRAME' AND character_reference_image_id IS NULL "
            "AND scene_reference_image_id IS NULL AND shot_keyframe_id IS NOT NULL)",
            name="ck_video_clip_asset_kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    video_clip_id: Mapped[str] = mapped_column(ForeignKey("video_clips.id"), nullable=False, index=True)
    asset_type: Mapped[str] = mapped_column(String(40), nullable=False)
    character_reference_image_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("character_reference_images.id"), nullable=True
    )
    character_asset_version_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("character_asset_versions.id"), nullable=True, index=True
    )
    scene_reference_image_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("scene_reference_images.id"), nullable=True
    )
    scene_asset_version_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("scene_asset_versions.id"), nullable=True, index=True
    )
    shot_keyframe_id: Mapped[Optional[str]] = mapped_column(ForeignKey("shot_keyframes.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
