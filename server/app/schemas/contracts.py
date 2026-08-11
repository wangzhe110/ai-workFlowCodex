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


class V1GenerationRunRequest(BaseModel):
    """V1 生成请求中允许显式选择参考素材或指定视频重做镜头。

    参考视频分析必须由浏览器提交 ``source_asset_id``，不允许服务端再猜测“最新上传”；
    视频重做可提交 ``shot_plan_ids``。模型、Prompt 和供应商协议仍由服务端冻结。
    """

    source_asset_id: Optional[str] = Field(default=None, min_length=1, max_length=36)
    shot_plan_ids: list[str] = Field(default_factory=list, max_length=80)


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


# ---------------------------------------------------------------------------
# LemonFlow V1 生产台：状态、审核、模型槽位与 Prompt 版本接口。
# 这些契约不复用旧“原创选题/故事包/单镜图片”页面结构，避免旧流程阻塞主链路。
# ---------------------------------------------------------------------------


class ReviewActionRequest(BaseModel):
    """人工审核统一输入；评分可选，避免历史审核与“只做决定”被强制阻断。"""

    reviewer_label: Optional[str] = Field(default=None, max_length=120)
    note: Optional[str] = Field(default=None, max_length=2000)
    quality_score: Optional[int] = Field(default=None, ge=1, le=10, description="制作人对本次结果的 1 至 10 分评分")


class ProductionStateResponse(BaseModel):
    """生产台顶部进度条所需的当前阶段与冻结对象指针。"""

    project_id: str
    active_stage: str
    workflow_definition_id: str
    locked_reference_analysis_id: Optional[str]
    selected_story_proposal_id: Optional[str]
    director_plan_id: Optional[str]
    created_at: datetime
    updated_at: datetime


class ReferenceAnalysisResponse(BaseModel):
    """爆款视频分析的五类可人工审核结果；不输出原视频逐字复刻内容。"""

    id: str
    project_id: str
    workflow_run_id: str
    version: int
    video_script_structure: dict[str, Any]
    opening_analysis: dict[str, Any]
    viral_elements: list[dict[str, Any]]
    scene_analysis: list[dict[str, Any]]
    creative_brief: dict[str, Any]
    generation_status: str
    review_status: str
    locked_snapshot: Optional[dict[str, Any]]
    locked_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class StoryProposalV1Response(BaseModel):
    """并行编剧模型产出的原创候选；选择状态由人工决定。"""

    id: str
    project_id: str
    batch_id: str
    model_invocation_id: Optional[str]
    candidate_number: int
    content: dict[str, Any]
    status: str
    created_at: datetime


class CharacterReferenceImageV1Response(BaseModel):
    """角色资产审核卡片；含角色代码和当前设计状态，便于锁图。"""

    id: str
    project_id: str
    character_id: str
    character_code: str
    character_name: str
    version: int
    asset_version_id: Optional[str]
    image_url: Optional[str]
    generation_status: str
    review_status: str
    created_at: datetime


class SceneReferenceImageV1Response(BaseModel):
    """场景资产审核卡片；含场景代码和当前设计状态，便于锁图。"""

    id: str
    project_id: str
    scene_id: str
    scene_code: str
    scene_name: str
    version: int
    asset_version_id: Optional[str]
    image_url: Optional[str]
    generation_status: str
    review_status: str
    created_at: datetime


class ShotKeyframeV1Response(BaseModel):
    """分镜关键帧审核卡片；明确它服务于哪个导演镜头。"""

    id: str
    project_id: str
    shot_id: str
    shot_number: int
    version: int
    image_url: Optional[str]
    generation_status: str
    review_status: str
    input_asset_snapshot: dict[str, Any]
    created_at: datetime


class VideoClipV1Response(BaseModel):
    """V1 视频审核卡片；输入资产快照可用于追溯角色、场景与关键帧版本。"""

    id: str
    project_id: str
    shot_plan_id: str
    shot_number: int
    version: int
    video_url: Optional[str]
    provider_task_id: Optional[str]
    task_status: Optional[str]
    is_current: bool
    generation_status: Optional[str]
    review_status: Optional[str]
    review_note: Optional[str]
    input_asset_snapshot: Optional[dict[str, Any]]
    created_at: datetime


# ---------------------------------------------------------------------------
# Phase 4 资产中心与结构化导演方案。
# 资产版本只有 POST 创建，不提供 PATCH，防止覆盖已被 Workflow / VideoClip 冻结的输入。
# ---------------------------------------------------------------------------


class AssetReferenceImageInput(BaseModel):
    """资产版本中的一张参考图，支持角色多视图和场景多角度图。"""

    view: str = Field(default="generated", min_length=1, max_length=40)
    url: str = Field(min_length=1, max_length=4000)
    label: Optional[str] = Field(default=None, max_length=160)


class CharacterAssetVersionCreateRequest(BaseModel):
    """创建角色资产首版或下一版；旧版始终保持只读。"""

    description: str = Field(default="", max_length=10_000)
    age: Optional[str] = Field(default=None, max_length=120)
    gender: Optional[str] = Field(default=None, max_length=40)
    personality: Optional[str] = Field(default=None, max_length=10_000)
    style: Optional[str] = Field(default=None, max_length=10_000)
    appearance: Optional[str] = Field(default=None, max_length=10_000)
    costume: Optional[str] = Field(default=None, max_length=10_000)
    reference_images: list[AssetReferenceImageInput] = Field(default_factory=list, max_length=16)


class CharacterAssetCreateRequest(CharacterAssetVersionCreateRequest):
    """资产中心手工新建角色与 v1 的输入。"""

    name: str = Field(min_length=1, max_length=160)
    library_id: Optional[str] = Field(default=None, min_length=1, max_length=36)


class CharacterAssetVersionResponse(BaseModel):
    id: str
    character_asset_id: str
    version: int
    description: str
    age: Optional[str]
    gender: Optional[str]
    personality: Optional[str]
    style: Optional[str]
    appearance: Optional[str]
    costume: Optional[str]
    reference_images: list[dict[str, Any]]
    created_at: datetime


class CharacterAssetResponse(BaseModel):
    """角色资产主体和所有不可变版本，供资产中心卡片展示。"""

    id: str
    library_id: str
    name: str
    description: str
    status: str
    versions: list[CharacterAssetVersionResponse]
    created_at: datetime
    updated_at: datetime


class SceneAssetVersionCreateRequest(BaseModel):
    """创建场景资产首版或下一版；天气和时段独立于风格，便于商业复用。"""

    description: str = Field(default="", max_length=10_000)
    style: Optional[str] = Field(default=None, max_length=10_000)
    weather: Optional[str] = Field(default=None, max_length=120)
    time_of_day: Optional[str] = Field(default=None, max_length=120)
    location: Optional[str] = Field(default=None, max_length=10_000)
    environment: Optional[str] = Field(default=None, max_length=10_000)
    mood: Optional[str] = Field(default=None, max_length=10_000)
    reference_images: list[AssetReferenceImageInput] = Field(default_factory=list, max_length=16)


class SceneAssetCreateRequest(SceneAssetVersionCreateRequest):
    name: str = Field(min_length=1, max_length=160)
    library_id: Optional[str] = Field(default=None, min_length=1, max_length=36)


class SceneAssetVersionResponse(BaseModel):
    id: str
    scene_asset_id: str
    version: int
    description: str
    style: Optional[str]
    weather: Optional[str]
    time_of_day: Optional[str]
    location: Optional[str]
    environment: Optional[str]
    mood: Optional[str]
    reference_images: list[dict[str, Any]]
    created_at: datetime


class SceneAssetResponse(BaseModel):
    id: str
    library_id: str
    name: str
    description: str
    status: str
    versions: list[SceneAssetVersionResponse]
    created_at: datetime
    updated_at: datetime


class DirectorShotResponse(BaseModel):
    """图片、视频、声音生产直接使用的结构化导演镜头。"""

    id: str
    shot_number: int
    duration: float
    character_ids: list[str]
    character_asset_version_ids: list[str]
    scene_id: str
    scene_asset_version_id: Optional[str]
    action: str
    emotion: str
    camera_type: str
    camera_move: str
    lighting: str
    image_prompt: str
    video_prompt: str
    sound_prompt: str
    locked_keyframe_id: Optional[str]
    created_at: datetime


class DirectorPlanV1Response(BaseModel):
    id: str
    project_id: str
    story_proposal_id: str
    workflow_run_id: str
    visual_bible: dict[str, Any]
    status: str
    shots: list[DirectorShotResponse]
    created_at: datetime
    updated_at: datetime


class ModelInvocationTraceResponse(BaseModel):
    """项目生产追溯行；刻意不返回原始 Prompt、输入和输出，避免泄露素材内容。"""

    id: str
    workflow_run_id: Optional[str]
    workflow_key: Optional[str]
    workflow_version: Optional[str]
    task_type: str
    slot_key: str
    model_display_name: str
    model_key: str
    model_version: str
    model_profile_version: Optional[int]
    prompt_template_id: Optional[str]
    prompt_name: Optional[str]
    prompt_version: Optional[int]
    status: str
    provider_task_id: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    media_units: dict[str, Any]
    cost_amount: Optional[float]
    currency: str
    latency_ms: Optional[int]
    error_code: Optional[str]
    created_at: datetime
    finished_at: Optional[datetime]


class ModelSlotResponse(BaseModel):
    """能力槽位展示模型，业务名称与具体供应商/模型名称分离。"""

    id: str
    slot_key: str
    capability: str
    selection_mode: str
    description: str
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class ModelSlotStrategyRequest(BaseModel):
    """模型中心由人工调整单模型/并行策略；V1 禁止自动 A/B 分流。"""

    selection_mode: str = Field(min_length=1, max_length=40)


class ModelSlotBindingRequest(BaseModel):
    """将无密钥模型配置绑定给一个能力槽位。"""

    model_profile_id: str = Field(min_length=1, max_length=36)
    enabled: bool = True
    # 只有用户在模型中心明确确认时，单模型槽位才会停用旧绑定并切换到新配置。
    # 它不是模型质量系统的自动切换开关。
    replace_existing: bool = False
    priority: int = Field(default=100, ge=0, le=10_000)
    weight: Optional[float] = Field(default=None, gt=0, le=1)


class ModelSlotBindingResponse(BaseModel):
    """模型槽位绑定记录；API 不返回环境变量实际值或任何密钥。"""

    id: str
    slot_id: str
    model_profile_id: str
    is_enabled: bool
    priority: int
    weight: Optional[float]
    created_at: datetime


class V1ModelProfileCreateRequest(BaseModel):
    """为 V1 能力槽位新建一版候选模型配置，真实密钥只允许部署在服务器环境。"""

    slot_key: str = Field(min_length=1, max_length=80)
    adapter_key: str = Field(min_length=1, max_length=80)
    model_key: str = Field(min_length=1, max_length=160)
    display_name: str = Field(min_length=1, max_length=160)
    model_version: Optional[str] = Field(default=None, max_length=160)
    provider_config: dict[str, Any] = Field(default_factory=dict)
    enable_in_slot: bool = False
    replace_existing: bool = False
    priority: int = Field(default=100, ge=0, le=10_000)


class V1ModelProfileResponse(BaseModel):
    """V1 模型中心展示使用；不返回环境变量值或密钥内容。"""

    id: str
    slot_key: str
    adapter_key: str
    model_key: str
    display_name: str
    model_version: Optional[str]
    version: int
    provider_config: dict[str, Any]
    is_bound: bool
    is_enabled_in_slot: bool
    priority: Optional[int]
    profile_status: str
    has_model_invocations: bool
    can_edit: bool
    active_run_count: int
    can_delete: bool
    delete_block_reason: Optional[str]
    created_at: datetime


class V1ModelProfileUpdateRequest(BaseModel):
    """安全更新同一模型版本的可编辑配置字段；槽位和版本本身不可原地迁移。"""

    adapter_key: str = Field(min_length=1, max_length=80)
    model_key: str = Field(min_length=1, max_length=160)
    display_name: str = Field(min_length=1, max_length=160)
    model_version: Optional[str] = Field(default=None, max_length=160)
    provider_config: dict[str, Any] = Field(default_factory=dict)


class ModelQualityEvaluationResponse(BaseModel):
    """模型中心的质量报表行；结果仅供人工比较，不能触发自动切换。"""

    id: str
    model_profile_id: str
    display_name: str
    model_key: str
    model_version: str
    prompt_template_id: Optional[str]
    prompt_name: Optional[str]
    prompt_version: Optional[int]
    task_type: str
    scenario: str
    sample_count: int
    success_count: int
    success_rate: float
    average_cost_amount: Optional[float]
    currency: str
    average_latency_ms: Optional[int]
    average_human_score: Optional[float]
    adoption_rate: Optional[float]
    created_at: datetime


class ModelQualityRefreshRequest(BaseModel):
    """按需生成一份新的质量报表快照；不重跑模型、不更改生产配置。"""

    task_type: Optional[str] = Field(default=None, min_length=1, max_length=80)


class PromptTemplateCreateRequest(BaseModel):
    """创建一个新 Prompt 草稿版本；生产版本不可通过 PATCH 原地改写。"""

    task_type: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=50_000)
    variables_schema: dict[str, Any] = Field(default_factory=dict)


class PromptTemplateResponse(BaseModel):
    """Prompt 模板版本的审计视图。"""

    id: str
    task_type: str
    name: str
    version: int
    content: str
    variables_schema: dict[str, Any]
    status: str
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Commerce Phase 2：带货短剧工作流控制面。
# ---------------------------------------------------------------------------


class CommerceStoryRunCreateRequest(BaseModel):
    topic_candidate_id: str = Field(min_length=1, max_length=36)
    project_product_selection_id: str = Field(min_length=1, max_length=36)
    mode: str = Field(default="STEPWISE", pattern="^(STEPWISE|AUTO)$")


class CommerceReviewRequest(BaseModel):
    reviewer_label: str = Field(default="人工审核", min_length=1, max_length=120)
    note: Optional[str] = Field(default=None, max_length=4000)
    quality_score: Optional[int] = Field(default=None, ge=1, le=10)
    # OUTLINE 可明确选择一个当前运行中的草稿版本；其余阶段由当前成功 Step 的结果决定。
    outline_id: Optional[str] = Field(default=None, min_length=1, max_length=36)


class CommerceOutlineCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    premise: str = Field(min_length=1, max_length=20_000)
    story_beats: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    product_placement_strategy: dict[str, Any] = Field(default_factory=dict)


class CommerceOutlinePatchRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=180)
    premise: Optional[str] = Field(default=None, min_length=1, max_length=20_000)
    story_beats: Optional[list[dict[str, Any]]] = Field(default=None, max_length=100)
    product_placement_strategy: Optional[dict[str, Any]] = Field(default=None)


class CommerceWorkflowStepResponse(BaseModel):
    id: str
    step_key: str
    position: int
    status: str
    attempt: int
    progress: int
    output_payload: Optional[dict[str, Any]]
    error_message: Optional[str]
    provider_task_id: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


class CommerceWorkflowRunResponse(BaseModel):
    id: str
    story_run_id: Optional[str]
    project_id: str
    workflow_key: str
    workflow_definition_id: Optional[str]
    workflow_version: Optional[str]
    status: str
    idempotency_key: Optional[str]
    input_snapshot: Optional[dict[str, Any]]
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    steps: list[CommerceWorkflowStepResponse]


class CommerceOutlineResponse(BaseModel):
    id: str
    story_run_id: str
    version: int
    title: str
    premise: str
    story_beats: list[dict[str, Any]]
    product_placement_strategy: dict[str, Any]
    status: str
    created_at: datetime


class CommerceReviewResponse(BaseModel):
    id: str
    project_id: str
    target_type: str
    target_id: str
    decision: str
    reviewer_label: str
    note: Optional[str]
    quality_score: Optional[int]
    created_at: datetime


class CommerceStoryRunResponse(BaseModel):
    id: str
    project_id: str
    topic_candidate_id: str
    project_product_selection_id: str
    product_asset_version_id: str
    run_number: int
    mode: str
    current_stage: str
    current_status: str
    blocked_reason: Optional[str]
    can_start: bool
    can_continue: bool
    can_confirm: bool
    current_workflow_run: Optional[CommerceWorkflowRunResponse]
    current_workflow_step: Optional[CommerceWorkflowStepResponse]
    latest_error: Optional[str]
    stage_result_references: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class CommerceWorkflowDefinitionResponse(BaseModel):
    id: str
    workflow_code: str
    version: str
    definition_json: dict[str, Any]
    status: str
    published_at: Optional[datetime]


# ---------------------------------------------------------------------------
# Commerce Phase 3：知识库和图片/视频生成能力预埋。
# ---------------------------------------------------------------------------


class ViralCaseCreateRequest(BaseModel):
    source_type: str = Field(min_length=1, max_length=60)
    source_identifier: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=240)
    source_url: Optional[str] = Field(default=None, max_length=1024)
    summary: Optional[str] = Field(default=None, max_length=20_000)
    raw_text: Optional[str] = Field(default=None, max_length=100_000)
    transcript_reference: Optional[str] = Field(default=None, max_length=512)
    raw_analysis: dict[str, Any] = Field(default_factory=dict)
    structured_analysis: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list, max_length=50)
    category: Optional[str] = Field(default=None, max_length=100)


class ViralCasePatchRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=240)
    source_url: Optional[str] = Field(default=None, max_length=1024)
    summary: Optional[str] = Field(default=None, max_length=20_000)
    raw_text: Optional[str] = Field(default=None, max_length=100_000)
    transcript_reference: Optional[str] = Field(default=None, max_length=512)
    raw_analysis: Optional[dict[str, Any]] = None
    structured_analysis: Optional[dict[str, Any]] = None
    tags: Optional[list[str]] = Field(default=None, max_length=50)
    category: Optional[str] = Field(default=None, max_length=100)


class ViralCaseResponse(BaseModel):
    id: str
    project_id: str
    source_type: str
    source_identifier: str
    source_url: Optional[str]
    title: str
    summary: Optional[str]
    raw_text: Optional[str]
    transcript_reference: Optional[str]
    raw_analysis: dict[str, Any]
    structured_analysis: dict[str, Any]
    tags: list[str]
    category: Optional[str]
    status: str
    created_by: Optional[str]
    created_at: datetime
    updated_at: datetime


class ViralCasePageResponse(BaseModel):
    items: list[ViralCaseResponse]
    page: int
    page_size: int
    total: int


class ViralPatternCreateRequest(BaseModel):
    pattern_type: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=240)
    source_case_id: Optional[str] = Field(default=None, min_length=1, max_length=36)
    summary: Optional[str] = Field(default=None, max_length=20_000)
    structured_rules: dict[str, Any] = Field(default_factory=dict)
    applicable_scenarios: list[str] = Field(default_factory=list, max_length=50)
    tags: list[str] = Field(default_factory=list, max_length=50)


class ViralPatternPatchRequest(BaseModel):
    pattern_type: Optional[str] = Field(default=None, min_length=1, max_length=80)
    name: Optional[str] = Field(default=None, min_length=1, max_length=240)
    source_case_id: Optional[str] = Field(default=None, min_length=1, max_length=36)
    summary: Optional[str] = Field(default=None, max_length=20_000)
    structured_rules: Optional[dict[str, Any]] = None
    applicable_scenarios: Optional[list[str]] = Field(default=None, max_length=50)
    tags: Optional[list[str]] = Field(default=None, max_length=50)


class ViralPatternPublishRequest(BaseModel):
    """草稿可带更新发布；已发布模式传入内容会创建不可覆盖的新版本。"""

    updates: Optional[ViralPatternPatchRequest] = None


class ViralPatternResponse(BaseModel):
    id: str
    project_id: str
    pattern_key: str
    source_case_id: Optional[str]
    pattern_type: str
    name: str
    summary: Optional[str]
    structured_rules: dict[str, Any]
    applicable_scenarios: list[str]
    tags: list[str]
    version: int
    is_current: bool
    status: str
    created_by: Optional[str]
    created_at: datetime
    updated_at: datetime


class ViralPatternPageResponse(BaseModel):
    items: list[ViralPatternResponse]
    page: int
    page_size: int
    total: int


class KnowledgeChunkCreateRequest(BaseModel):
    viral_case_id: Optional[str] = Field(default=None, min_length=1, max_length=36)
    viral_pattern_id: Optional[str] = Field(default=None, min_length=1, max_length=36)
    chunk_index: int = Field(ge=0)
    content: str = Field(min_length=1, max_length=100_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    embedding_provider: Optional[str] = Field(default=None, max_length=80)
    embedding_model: Optional[str] = Field(default=None, max_length=160)
    embedding_dimension: Optional[int] = Field(default=None, gt=0)
    external_vector_id: Optional[str] = Field(default=None, max_length=255)


class KnowledgeChunkResponse(BaseModel):
    id: str
    viral_case_id: Optional[str]
    viral_pattern_id: Optional[str]
    resource_type: str
    resource_id: str
    chunk_index: int
    content: str
    content_hash: str
    metadata: dict[str, Any]
    embedding_provider: Optional[str]
    embedding_model: Optional[str]
    embedding_dimension: Optional[int]
    external_vector_id: Optional[str]
    created_at: datetime
    updated_at: datetime


class RetrievalPreviewRequest(BaseModel):
    provider_key: str = Field(default="fake_in_memory", min_length=1, max_length=80)
    query_text: str = Field(min_length=1, max_length=20_000)
    top_k: int = Field(default=5, ge=1, le=50)
    resource_types: list[str] = Field(default_factory=list, max_length=2)
    tags: list[str] = Field(default_factory=list, max_length=50)
    statuses: list[str] = Field(default_factory=lambda: ["ACTIVE"], max_length=3)
    request_id: Optional[str] = Field(default=None, max_length=80)


class RetrievalHitResponse(BaseModel):
    rank: int
    chunk_id: str
    resource_type: str
    resource_id: str
    score: float
    metadata: dict[str, Any]


class RetrievalPreviewResponse(BaseModel):
    retrieval_call_id: str
    provider_key: str
    status: str
    hits: list[RetrievalHitResponse]


class GenerationTaskCreateRequest(BaseModel):
    modality: str = Field(pattern="^(IMAGE|VIDEO)$")
    capability: str = Field(min_length=1, max_length=80)
    model_key: str = Field(min_length=1, max_length=160)
    parameters: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=160)
    preferred_provider: Optional[str] = Field(default=None, min_length=1, max_length=80)
    fallback_providers: list[str] = Field(default_factory=list, max_length=10)


class GenerationCallbackRequest(BaseModel):
    provider_key: str = Field(min_length=1, max_length=80)
    provider_task_id: str = Field(min_length=1, max_length=255)
    status: str = Field(pattern="^(RUNNING|SUCCEEDED|FAILED|CANCELLED)$")
    output_reference: Optional[dict[str, Any]] = None
    usage: dict[str, Any] = Field(default_factory=dict)
    sanitized_response: dict[str, Any] = Field(default_factory=dict)
    error_code: Optional[str] = Field(default=None, max_length=120)
    error_message: Optional[str] = Field(default=None, max_length=500)


class GenerationTaskResponse(BaseModel):
    id: str
    project_id: str
    modality: str
    capability: str
    idempotency_key: Optional[str]
    request_snapshot: dict[str, Any]
    provider_key: Optional[str]
    model_key: Optional[str]
    provider_task_id: Optional[str]
    output_reference: Optional[dict[str, Any]]
    usage: dict[str, Any]
    fallback_used: bool
    status: str
    error_code: Optional[str]
    error_message: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


# Pydantic 需要在全部类型声明后解析前向引用。
ProjectDetailResponse.model_rebuild()
