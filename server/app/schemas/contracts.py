"""HTTP 接口的稳定输入输出契约。"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class ProjectCreateRequest(BaseModel):
    """创建项目时允许用户填写的最小信息。"""

    title: str = Field(min_length=1, max_length=120, description="项目名称")
    description: Optional[str] = Field(default=None, max_length=2000, description="创作方向或备注")


class AssetResponse(BaseModel):
    """对前端公开的素材元数据；不暴露对象存储内部凭据。"""

    id: str
    kind: str
    original_filename: str
    content_type: str
    byte_size: int
    created_at: datetime


class ProjectSummaryResponse(BaseModel):
    """项目列表页使用的轻量数据。"""

    id: str
    title: str
    description: Optional[str]
    created_at: datetime
    updated_at: datetime
    source_video_count: int


class ProjectDetailResponse(ProjectSummaryResponse):
    """项目详情页数据，含素材和历史工作流。"""

    assets: list[AssetResponse]
    workflow_runs: list["WorkflowRunResponse"]


class WorkflowStepResponse(BaseModel):
    """一个可轮询步骤的展示状态。"""

    id: str
    step_key: str
    position: int
    status: str
    progress: int
    attempt: int
    output_payload: Optional[dict[str, Any]]
    error_message: Optional[str]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


class WorkflowRunResponse(BaseModel):
    """工作流运行摘要；前端通过它驱动步骤进度组件。"""

    id: str
    project_id: str
    workflow_key: str
    status: str
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    steps: list[WorkflowStepResponse]


class HealthResponse(BaseModel):
    """部署监测使用的健康检查响应。"""

    status: str
    service: str


class ReadinessResponse(HealthResponse):
    """依赖就绪检查结果，不暴露数据库地址、密码或底层异常。"""

    dependencies: dict[str, str]


class CreativeLibraryItemCreateRequest(BaseModel):
    """人工创建爆点元素或开头模式时的输入。"""

    kind: str = Field(description="VIRAL_ELEMENT 或 OPENING_PATTERN")
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=5000)
    group_name: Optional[str] = Field(default=None, max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=20)


class CreativeLibraryItemResponse(BaseModel):
    """创作资产库的公开展示字段。"""

    id: str
    kind: str
    title: str
    content: str
    group_name: Optional[str]
    tags: list[str]
    source: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class TopicCandidateResponse(BaseModel):
    """选题卡片页面和后续故事生成的稳定输入。"""

    id: str
    project_id: str
    generation_run_id: str
    position: int
    title: str
    opening_hook: str
    synopsis: str
    score: Optional[int]
    scoring_notes: Optional[str]
    status: str
    created_at: datetime
    updated_at: datetime


class StoryPackageResponse(BaseModel):
    """故事工作台展示的一版可审核创作包。"""

    id: str
    project_id: str
    topic_candidate_id: str
    generation_run_id: str
    title: str
    premise: str
    outline: list[dict[str, Any]]
    roles: list[dict[str, Any]]
    scenes: list[dict[str, Any]]
    status: str
    created_at: datetime
    updated_at: datetime

class StoryboardGenerationRequest(BaseModel):
    """用户可按项目时长需求配置镜头数，禁止写死为参考项目的 200 镜。"""
    shot_count: int = Field(default=12, ge=1, le=200)

class StoryboardPackageResponse(BaseModel):
    id: str; project_id: str; story_package_id: str; generation_run_id: str
    target_shot_count: int; shots: list[dict[str, Any]]; status: str
    created_at: datetime; updated_at: datetime

class StoryboardImageResponse(BaseModel):
    id: str; storyboard_package_id: str; generation_run_id: str; shot_number: int; version: int
    prompt: str; image_url: Optional[str]; status: str; error_message: Optional[str]; created_at: datetime


class VideoGenerationRequest(BaseModel):
    """视频片段生成的可调分组参数。

    默认四镜一组仅是 V1 的生产建议，并非写死规则；用户可按所接模型的时长能力
    调整。传入 group_numbers 时只为这些组创建新版本。
    """

    shots_per_group: int = Field(default=4, ge=1, le=20)
    group_numbers: Optional[list[int]] = Field(default=None, min_length=1)


class VideoClipResponse(BaseModel):
    """前端审核视频片段时需要的分组、版本与溯源信息。"""

    id: str
    storyboard_package_id: str
    generation_run_id: str
    group_number: int
    start_shot_number: int
    end_shot_number: int
    shots_per_group: int
    version: int
    image_ids: list[str]
    prompt: str
    video_url: Optional[str]
    provider_task_id: Optional[str]
    status: str
    error_message: Optional[str]
    created_at: datetime


class FinalVideoResponse(BaseModel):
    """一版完整成片的审核与下载视图。

    ``download_url`` 仅在本地或对象存储结果已经就绪时存在；模拟模式使用
    ``video_url=mock://...`` 表示流程成功，但不会误导用户下载无效 MP4。
    """

    id: str
    storyboard_package_id: str
    generation_run_id: str
    version: int
    clip_ids: list[str]
    video_url: Optional[str]
    download_url: Optional[str]
    status: str
    error_message: Optional[str]
    created_at: datetime
    finished_at: Optional[datetime]


class ModelProfileCreateRequest(BaseModel):
    """新增一版步骤模型配置的输入。

    `provider_config` 仅允许非敏感参数和 `secret_env_name` 环境变量引用；服务层会
    拒绝任何疑似真实密钥字段，避免误入数据库。
    """

    step_key: str = Field(min_length=1, max_length=80)
    provider_key: str = Field(min_length=1, max_length=80)
    model_key: str = Field(min_length=1, max_length=160)
    provider_config: dict[str, Any] = Field(default_factory=dict)
    activate: bool = True


class ModelProfileResponse(BaseModel):
    """配置中心返回的安全模型配置视图，不含真实 API Key。"""

    id: str
    step_key: str
    provider_key: str
    model_key: str
    version: int
    provider_config: dict[str, Any]
    is_active: bool
    adapter_available: bool
    created_at: datetime


class ModelPreflightCheckResponse(BaseModel):
    """一次模型配置预检中的单项结果，不包含密钥值或底层网络异常。"""

    key: str
    status: str
    message: str


class ModelProfilePreflightResponse(BaseModel):
    """候选配置的无扣费预检结果。"""

    profile_id: str
    ready: bool
    checked_at: datetime
    checks: list[ModelPreflightCheckResponse]


class ModelEvaluationCreateRequest(BaseModel):
    """提交一次模型小样本验收的汇总统计，不上传模型原始内容。"""

    scenario: str = Field(min_length=1, max_length=120, description="例如：9:16 五镜图生视频小样")
    sample_count: int = Field(ge=1, le=100_000)
    success_count: int = Field(ge=0, le=100_000)
    total_cost_yuan: float = Field(ge=0, le=10_000_000)
    average_latency_seconds: float = Field(gt=0, le=86_400)
    quality_score: int = Field(ge=0, le=100)
    notes: Optional[str] = Field(default=None, max_length=2000)


class ModelEvaluationResponse(BaseModel):
    """配置页用于横向比较的安全、聚合后的实测记录。"""

    id: str
    model_profile_id: str
    scenario: str
    sample_count: int
    success_count: int
    total_cost_yuan: float
    average_latency_seconds: float
    quality_score: int
    notes: Optional[str]
    success_rate: float
    average_cost_yuan: float
    cost_per_success_yuan: Optional[float]
    created_at: datetime


class ModelEvaluationComparisonResponse(ModelEvaluationResponse):
    """带配置版本标识的评测行，用于同一工作流步骤的横向比较。"""

    step_key: str
    provider_key: str
    model_key: str
    profile_version: int
    display_name: Optional[str]


# Pydantic 需要在全部类型声明后解析前向引用。
ProjectDetailResponse.model_rebuild()
