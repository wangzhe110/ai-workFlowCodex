"""数据库实体：保存项目、资产、工作流和模型配置的可审计状态。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import JSON, DateTime, Enum as SqlEnum, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
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
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
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

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    workflow_key: Mapped[str] = mapped_column(String(80), nullable=False)
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
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    storyboard_package_id: Mapped[str] = mapped_column(
        ForeignKey("storyboard_packages.id"), nullable=False, index=True
    )
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
    status: Mapped[VideoClipStatus] = mapped_column(
        SqlEnum(VideoClipStatus), default=VideoClipStatus.PENDING, nullable=False
    )
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
    storyboard_package_id: Mapped[str] = mapped_column(
        ForeignKey("storyboard_packages.id"), nullable=False, index=True
    )
    generation_run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    clip_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    output_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    storage_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True, unique=True)
    status: Mapped[FinalVideoStatus] = mapped_column(
        SqlEnum(FinalVideoStatus), default=FinalVideoStatus.PENDING, nullable=False
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship(back_populates="final_videos")
