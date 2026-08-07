"""LemonFlow V1 的 Workflow 定义、模型槽位与 Prompt 配置服务。

本模块只管理可审计的配置数据；真实 API Key 仍只通过 Adapter 在运行环境读取。
业务工作流不得根据 Gemini、Claude、Banana 或 Seedance 的名字分支，而应读取模型槽位。
"""

from copy import deepcopy
from datetime import datetime, timezone
import re
from typing import Any, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ModelEvaluation,
    ModelProfile,
    ModelInvocation,
    ModelQualityEvaluation,
    ModelSelectionMode,
    ModelSlot,
    ModelSlotProfileBinding,
    ProductionStage,
    ProjectProductionState,
    PromptTemplate,
    PromptTemplateStatus,
    RunStatus,
    WorkflowDefinition,
    WorkflowDefinitionStatus,
    WorkflowRun,
)


V1_WORKFLOW_CODE = "LEMONFLOW_PRODUCTION"
V1_WORKFLOW_VERSION = "LemonFlow_V1"
V1_DEFAULT_PROMPT_VERSION = 1

V1_SLOT_SPECS: tuple[tuple[str, str, ModelSelectionMode, str], ...] = (
    ("VIDEO_ANALYSIS", "VIDEO_ANALYSIS", ModelSelectionMode.SINGLE, "视频结构与创作简报分析"),
    ("STORY_GENERATE", "STORY_GENERATE", ModelSelectionMode.MULTI_PARALLEL, "多模型并行原创故事生成"),
    ("CHARACTER_DESIGN", "CHARACTER_DESIGN", ModelSelectionMode.SINGLE, "角色文字资产设计"),
    ("SCENE_DESIGN", "SCENE_DESIGN", ModelSelectionMode.SINGLE, "场景文字资产设计"),
    ("DIRECTOR_PLAN", "DIRECTOR_PLAN", ModelSelectionMode.SINGLE, "导演视觉方案与分镜规划"),
    ("CHARACTER_IMAGE_GENERATE", "IMAGE_GENERATE", ModelSelectionMode.SINGLE, "角色参考图生成"),
    ("SCENE_IMAGE_GENERATE", "IMAGE_GENERATE", ModelSelectionMode.SINGLE, "场景参考图生成"),
    ("SHOT_KEYFRAME_GENERATE", "IMAGE_GENERATE", ModelSelectionMode.SINGLE, "分镜关键画面生成"),
    ("VIDEO_GENERATE", "VIDEO_GENERATE", ModelSelectionMode.SINGLE, "锁定资产驱动的视频生成"),
    ("FINAL_COMPOSE", "FINAL_COMPOSE", ModelSelectionMode.SINGLE, "审核通过片段的成片合成"),
)

# 本地模拟配置只用于在没有 API Key 时验证状态机和审核闭环。它们并不冒充 Gemini、
# Claude、Banana 或 Seedance；生产部署应在模型中心把对应槽位切换到已验收的真实配置。
V1_MOCK_PROFILE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("VIDEO_ANALYSIS", "mock-v1-video-analysis", "本地模拟：参考视频分析"),
    ("STORY_GENERATE", "mock-v1-story-director-a", "本地模拟：故事导演 A"),
    ("STORY_GENERATE", "mock-v1-story-director-b", "本地模拟：故事导演 B"),
    ("STORY_GENERATE", "mock-v1-story-director-c", "本地模拟：故事导演 C"),
    ("CHARACTER_DESIGN", "mock-v1-character-design", "本地模拟：角色资产设计"),
    ("SCENE_DESIGN", "mock-v1-scene-design", "本地模拟：场景资产设计"),
    ("DIRECTOR_PLAN", "mock-v1-director-plan", "本地模拟：导演分镜"),
    ("CHARACTER_IMAGE_GENERATE", "mock-v1-character-image", "本地模拟：角色参考图"),
    ("SCENE_IMAGE_GENERATE", "mock-v1-scene-image", "本地模拟：场景参考图"),
    ("SHOT_KEYFRAME_GENERATE", "mock-v1-shot-keyframe", "本地模拟：分镜关键帧"),
    ("VIDEO_GENERATE", "mock-v1-video-generate", "本地模拟：视频片段"),
    ("FINAL_COMPOSE", "mock-v1-final-compose", "本地模拟：成片合成"),
)

V1_PROMPT_SEEDS: tuple[tuple[str, str, str], ...] = (
    ("VIDEO_ANALYSIS", "V1 默认视频分析", "只提炼结构、开头机制、爆款元素与场景作用；不得复刻人物、台词、画面或音乐。"),
    ("STORY_GENERATE", "V1 默认原创故事", "保留已锁定简报中的节奏和情绪机制，创作全新人设、关系和剧情，不复制参考故事。"),
    ("CHARACTER_DESIGN", "V1 默认角色设计", "根据已选原创故事设计稳定角色资产：年龄、外貌、服装与性格，不使用参考视频人物。"),
    ("SCENE_DESIGN", "V1 默认场景设计", "根据已选原创故事设计稳定场景资产：地点、环境、视觉风格与氛围。"),
    ("DIRECTOR_PLAN", "V1 默认导演分镜", "只引用已锁定角色图和场景图，输出动作、机位、时长和视频动作描述。"),
    ("IMAGE_GENERATE", "V1 默认图片生成", "保持输入角色/场景资产一致，生成原创视觉画面；不得复用参考视频具体画面。"),
    ("VIDEO_GENERATE", "V1 默认视频生成", "根据锁定角色图、场景图、关键帧与动作描述生成连续视频片段。"),
    ("FINAL_COMPOSE", "V1 默认成片合成", "按人工审核通过的视频片段顺序合成，不插入未经审核的片段。"),
)

# 这些是“协议 Adapter”与业务槽位的兼容关系，不是具体模型白名单。新的 Gemini、
# Claude、Banana 或其他模型只要使用其中一个已接通协议，即可创建新版本配置。
V1_SLOT_ADAPTERS: dict[str, set[str]] = {
    "VIDEO_ANALYSIS": {"mock_v1", "openai_compatible_vision"},
    "STORY_GENERATE": {"mock_v1", "openai_compatible"},
    "CHARACTER_DESIGN": {"mock_v1", "openai_compatible"},
    "SCENE_DESIGN": {"mock_v1", "openai_compatible"},
    "DIRECTOR_PLAN": {"mock_v1", "openai_compatible"},
    "CHARACTER_IMAGE_GENERATE": {"mock_v1", "openai_compatible_image"},
    "SCENE_IMAGE_GENERATE": {"mock_v1", "openai_compatible_image"},
    "SHOT_KEYFRAME_GENERATE": {"mock_v1", "openai_compatible_image"},
    "VIDEO_GENERATE": {"mock_v1", "volcengine_ark_video", "configurable_async_video"},
    "FINAL_COMPOSE": {"mock_v1", "ffmpeg_concat"},
}

_ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SAFE_FIELD_NAME = re.compile(r"^[A-Za-z0-9_]+$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3,12}$")
_FORBIDDEN_CONFIG_KEY_PARTS = ("api_key", "apikey", "token", "secret", "password", "authorization")


def utcnow() -> datetime:
    """统一生成 UTC 时间，便于工作流和配置审计关联。"""

    return datetime.now(timezone.utc)


def v1_definition_payload() -> dict[str, Any]:
    """返回发布版 V1 的阶段定义快照，禁止在项目运行后原地修改。"""

    return {
        "stages": [stage.value for stage in ProductionStage if stage not in {ProductionStage.LEGACY_READONLY, ProductionStage.COMPLETED}],
        "review_gates": ["ANALYSIS_LOCK", "STORY_SELECT", "ASSET_LOCK", "VIDEO_APPROVE"],
        "model_selection": {"STORY_GENERATE": ModelSelectionMode.MULTI_PARALLEL.value},
    }


def ensure_v1_foundation(db: Session) -> WorkflowDefinition:
    """幂等确保 V1 的初始化配置存在。

    该函数会被模型中心、项目生产台和 Worker 多次调用，因此每一种初始化对象都必须
    先按自身唯一业务键查询，再决定是否创建：Workflow 用 ``code + version``、槽位用
    ``slot_key``、本地模拟绑定用 ``slot + profile``、默认 Prompt 用
    ``task_type + name + version``。它只补齐缺失的种子数据，绝不删除或覆盖已有配置。
    """

    definition = db.scalars(
        select(WorkflowDefinition).where(
            WorkflowDefinition.workflow_code == V1_WORKFLOW_CODE,
            WorkflowDefinition.version == V1_WORKFLOW_VERSION,
        )
    ).first()
    if definition is None:
        definition = WorkflowDefinition(
            workflow_code=V1_WORKFLOW_CODE,
            version=V1_WORKFLOW_VERSION,
            definition_json=v1_definition_payload(),
            status=WorkflowDefinitionStatus.PUBLISHED,
            published_at=utcnow(),
        )
        db.add(definition)

    existing_slots = {
        slot.slot_key: slot
        for slot in db.scalars(select(ModelSlot).where(ModelSlot.slot_key.in_([spec[0] for spec in V1_SLOT_SPECS]))).all()
    }
    for slot_key, capability, selection_mode, description in V1_SLOT_SPECS:
        if slot_key not in existing_slots:
            db.add(
                ModelSlot(
                    slot_key=slot_key,
                    capability=capability,
                    selection_mode=selection_mode,
                    description=description,
                    is_enabled=True,
                )
            )
    db.flush()
    slots_by_key = {
        slot.slot_key: slot
        for slot in db.scalars(select(ModelSlot).where(ModelSlot.slot_key.in_([spec[0] for spec in V1_SLOT_SPECS]))).all()
    }
    _ensure_local_mock_profiles(db, slots_by_key)
    _ensure_prompt_templates(db)
    db.commit()
    db.refresh(definition)
    return definition


def _ensure_local_mock_profiles(db: Session, slots_by_key: dict[str, ModelSlot]) -> None:
    """写入显式标记为本地模拟的 V1 绑定，供无密钥开发环境跑通审核闭环。"""

    for priority, (slot_key, model_key, display_name) in enumerate(V1_MOCK_PROFILE_SPECS, start=1):
        profile = db.scalars(
            select(ModelProfile).where(
                ModelProfile.step_key == slot_key,
                ModelProfile.adapter_key == "mock_v1",
                ModelProfile.model_key == model_key,
            )
        ).first()
        if profile is None:
            version = db.scalar(select(func.max(ModelProfile.version)).where(ModelProfile.step_key == slot_key)) or 0
            profile = ModelProfile(
                step_key=slot_key,
                provider_key="mock_v1",
                adapter_key="mock_v1",
                model_key=model_key,
                model_version=model_key,
                display_name=display_name,
                version=version + 1,
                provider_config={"display_name": display_name, "local_only": True},
                # 旧模型配置页不应把模拟 V1 配置误显示为旧流程的活动生产模型。
                is_active=False,
                profile_status="DRAFT",
            )
            db.add(profile)
            db.flush()
        slot = slots_by_key[slot_key]
        binding = db.scalars(
            select(ModelSlotProfileBinding).where(
                ModelSlotProfileBinding.slot_id == slot.id,
                ModelSlotProfileBinding.model_profile_id == profile.id,
            )
        ).first()
        if binding is None:
            db.add(
                ModelSlotProfileBinding(
                    slot_id=slot.id,
                    model_profile_id=profile.id,
                    is_enabled=True,
                    priority=priority,
                )
            )
        # 种子模拟配置是 V1 开箱即用的当前绑定；无论数据库是新建还是从旧版补齐，
        # 都应显示为 ACTIVE，不能因绕过 ``bind_profile_to_slot`` 而误标为 DRAFT。
        profile.profile_status = "ACTIVE"


def _ensure_prompt_templates(db: Session) -> None:
    """按 ``task_type + name + version`` 幂等写入默认 Prompt。

    默认 Prompt 的身份固定为版本 1。即使该模板后来被归档、或制作人启用了另一个
    Prompt，也不能在服务初始化时重新插入同一个版本，更不能改变已有模板状态。
    当某个任务已有自定义 ACTIVE Prompt 但默认模板缺失时同样不补建，避免初始化行为
    偷偷改变制作人已经确认的生产配置。
    """

    for task_type, name, content in V1_PROMPT_SEEDS:
        existing_default = db.scalars(
            select(PromptTemplate).where(
                PromptTemplate.task_type == task_type,
                PromptTemplate.name == name,
                PromptTemplate.version == V1_DEFAULT_PROMPT_VERSION,
            )
        ).first()
        if existing_default is not None:
            continue
        active = db.scalars(
            select(PromptTemplate).where(
                PromptTemplate.task_type == task_type,
                PromptTemplate.status == PromptTemplateStatus.ACTIVE,
            )
        ).first()
        if active is not None:
            continue
        db.add(
            PromptTemplate(
                task_type=task_type,
                name=name,
                version=V1_DEFAULT_PROMPT_VERSION,
                content=content,
                variables_schema={"type": "object", "properties": {}},
                status=PromptTemplateStatus.ACTIVE,
            )
        )


def get_v1_definition(db: Session) -> WorkflowDefinition:
    """读取已发布 V1 定义；缺失时只创建配置种子，不触碰旧项目内容。"""

    return ensure_v1_foundation(db)


def get_or_create_project_state(db: Session, project_id: str) -> ProjectProductionState:
    """为新项目初始化 V1 生产状态；已有历史状态绝不被覆盖。"""

    existing = db.get(ProjectProductionState, project_id)
    if existing is not None:
        return existing
    definition = get_v1_definition(db)
    state = ProjectProductionState(
        project_id=project_id,
        active_stage=ProductionStage.REFERENCE_ANALYSIS,
        workflow_definition_id=definition.id,
    )
    db.add(state)
    db.commit()
    db.refresh(state)
    return state


def list_model_slots(db: Session) -> list[ModelSlot]:
    """按生产使用顺序读取槽位，不把历史兼容槽位混入 V1 模型中心。"""

    v1_keys = [spec[0] for spec in V1_SLOT_SPECS]
    slots = list(db.scalars(select(ModelSlot).where(ModelSlot.slot_key.in_(v1_keys))).all())
    positions = {key: position for position, key in enumerate(v1_keys)}
    return sorted(slots, key=lambda item: positions[item.slot_key])


def set_slot_strategy(db: Session, slot_key: str, selection_mode: ModelSelectionMode) -> ModelSlot:
    """人工切换槽位策略；V1 仅允许故事槽位使用多模型并行。"""

    slot = db.scalars(select(ModelSlot).where(ModelSlot.slot_key == slot_key)).first()
    if slot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模型槽位不存在")
    if selection_mode == ModelSelectionMode.MULTI_PARALLEL and slot.slot_key != "STORY_GENERATE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="V1 只有故事生成槽位允许并行生产模型")
    if selection_mode == ModelSelectionMode.AB_TEST:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="V1 仅展示模型评估，不执行自动 A/B 分流")
    slot.selection_mode = selection_mode
    db.commit()
    db.refresh(slot)
    return slot


def _normalize_v1_provider_config(value: dict[str, Any]) -> dict[str, Any]:
    """拒绝把密钥值写入数据库，只允许通过 ``secret_env_name`` 间接引用。"""

    if not isinstance(value, dict):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="模型配置必须是 JSON 对象")
    normalized = deepcopy(value)
    for key, item in normalized.items():
        if not isinstance(key, str):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="模型配置的字段名必须是文本")
        lowered = key.lower()
        if key != "secret_env_name" and any(part in lowered for part in _FORBIDDEN_CONFIG_KEY_PARTS):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="配置中不能填 API Key、Token 或密码；请填写服务器密钥变量名",
            )
        if isinstance(item, dict):
            _normalize_v1_provider_config(item)
    secret_env_name = normalized.get("secret_env_name")
    if secret_env_name is not None and (
        not isinstance(secret_env_name, str) or not _ENV_NAME_PATTERN.fullmatch(secret_env_name)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="服务器密钥变量名必须是大写英文，例如 YUNWU_API_KEY",
        )
    estimated_cost = normalized.get("estimated_cost_per_call")
    if estimated_cost is not None:
        # 费用只是制作人在模型中心填写的“单次预估”，不是供应商账单；实际账单对接
        # 后可以由 Adapter 覆盖这个字段，但禁止把文本或负数混入统计。
        if isinstance(estimated_cost, bool) or not isinstance(estimated_cost, (int, float)) or not 0 <= estimated_cost <= 10_000_000:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="预计单次成本必须是 0 至 10000000 的数字")
        normalized["estimated_cost_per_call"] = float(estimated_cost)
        currency = normalized.get("currency", "CNY")
        if not isinstance(currency, str) or not _CURRENCY_PATTERN.fullmatch(currency.upper()):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="成本币种请填写大写代码，例如 CNY 或 USD")
        normalized["currency"] = currency.upper()
    return normalized


def _require_openai_compatible_base(config: dict[str, Any], label: str) -> None:
    """校验文本、视觉和图片中转模型共同的非敏感连接信息。"""

    api_base_url = config.get("api_base_url")
    secret_env_name = config.get("secret_env_name")
    if not isinstance(api_base_url, str) or not api_base_url.startswith("https://"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"{label}需要填写 https:// API 地址")
    if not isinstance(secret_env_name, str) or not _ENV_NAME_PATTERN.fullmatch(secret_env_name):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"{label}需要填写服务器密钥变量名")


def _validate_v1_profile_config(slot_key: str, adapter: str, config: dict[str, Any]) -> None:
    """按 Adapter 验证 V1 候选配置；模型名不在这里写死。"""

    if adapter == "mock_v1":
        if not config.get("local_only"):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="本地模拟配置必须标记 local_only")
        return
    if adapter in {"openai_compatible", "openai_compatible_vision", "openai_compatible_image"}:
        _require_openai_compatible_base(config, "OpenAI 兼容模型")
        if adapter == "openai_compatible_vision":
            frame_count = config.get("frame_sample_count", 6)
            if not isinstance(frame_count, int) or not 1 <= frame_count <= 12:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="视频分析抽帧数必须在 1 至 12 张之间")
            contract = config.get("result_contract", "V1_REFERENCE_ANALYSIS")
            if contract != "V1_REFERENCE_ANALYSIS":
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="V1 视频分析结果类型必须保持为 V1_REFERENCE_ANALYSIS")
        if adapter == "openai_compatible_image":
            reference_field = config.get("reference_image_field")
            if slot_key == "SHOT_KEYFRAME_GENERATE" and (
                not isinstance(reference_field, str) or not _SAFE_FIELD_NAME.fullmatch(reference_field)
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="分镜关键帧需要填写图片中转站的参考图字段名，例如 images",
                )
            if reference_field is not None and (
                not isinstance(reference_field, str) or not _SAFE_FIELD_NAME.fullmatch(reference_field)
            ):
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="参考图字段名只能包含字母、数字和下划线")
        return
    if adapter == "volcengine_ark_video":
        secret_env_name = config.get("secret_env_name", "ARK_API_KEY")
        if not isinstance(secret_env_name, str) or not _ENV_NAME_PATTERN.fullmatch(secret_env_name):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="豆包视频需要 ARK_API_KEY 服务器变量名")
        ratio = config.get("ratio", "9:16")
        if ratio not in {"16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="豆包视频画幅不受支持")
        duration = config.get("duration", 5)
        if not isinstance(duration, int) or not 2 <= duration <= 12:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="豆包视频时长必须在 2 至 12 秒之间")
        return
    if adapter == "configurable_async_video":
        _require_openai_compatible_base(config, "异步视频中转模型")
        if not isinstance(config.get("submit_path"), str) or not isinstance(config.get("query_path_template"), str):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="异步视频中转模型需要提交地址和查询地址")
        if "{task_id}" not in config["query_path_template"]:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="异步视频查询地址必须包含 {task_id}")
        return
    if adapter == "ffmpeg_concat":
        return
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="不支持的 V1 Adapter")


def create_v1_model_profile(
    db: Session,
    *,
    slot_key: str,
    adapter: str,
    model_key: str,
    display_name: str,
    model_version: Optional[str],
    provider_config: dict[str, Any],
    enable_in_slot: bool,
    replace_existing: bool,
    priority: int,
) -> ModelProfile:
    """创建 V1 候选模型版本，并在用户明确选择时绑定到能力槽位。

    ``enable_in_slot`` 和 ``replace_existing`` 是人工模型切换动作；服务不会依据价格、
    评分或任何自动策略替用户替换生产模型。
    """

    get_v1_definition(db)
    clean_slot = slot_key.strip().upper()
    clean_adapter = adapter.strip()
    clean_model_key = model_key.strip()
    clean_display_name = display_name.strip()
    if clean_slot not in V1_SLOT_ADAPTERS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="V1 模型槽位不存在")
    if clean_adapter not in V1_SLOT_ADAPTERS[clean_slot]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该模型协议不能用于当前 V1 功能")
    if not clean_model_key or not clean_display_name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="模型名称和显示名称不能为空")
    if priority < 0 or priority > 10_000:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="优先级必须在 0 至 10000 之间")
    config = _normalize_v1_provider_config(provider_config)
    _validate_v1_profile_config(clean_slot, clean_adapter, config)
    version = db.scalar(select(func.max(ModelProfile.version)).where(ModelProfile.step_key == clean_slot)) or 0
    profile = ModelProfile(
        step_key=clean_slot,
        provider_key=clean_adapter,
        adapter_key=clean_adapter,
        model_key=clean_model_key,
        model_version=(model_version or clean_model_key).strip()[:160],
        display_name=clean_display_name,
        version=version + 1,
        provider_config=config,
        # V1 使用槽位绑定决定启用态，避免旧流程的“活动模型”混入新主流程。
        is_active=False,
        profile_status="DRAFT",
    )
    db.add(profile)
    db.flush()
    bind_profile_to_slot(
        db,
        slot_key=clean_slot,
        model_profile_id=profile.id,
        enabled=enable_in_slot,
        replace_existing=replace_existing,
        priority=priority,
        weight=None,
    )
    db.refresh(profile)
    return profile


def profile_has_model_invocations(db: Session, profile_id: str) -> bool:
    """判断配置版本是否已经成为生产调用事实的一部分。"""

    return db.scalar(
        select(ModelInvocation.id).where(ModelInvocation.model_profile_id == profile_id).limit(1)
    ) is not None


def active_v1_run_count_for_profile(db: Session, profile_id: str) -> int:
    """统计仍冻结引用某模型版本的 V1 活动任务数。

    运行一经创建就把实际模型列表写进 ``WorkflowRun.input_snapshot``。删除模型前必须
    检查该快照，而不能只看当前槽位绑定：否则制作人切换模型后，正在执行的旧任务会
    失去所需的可追溯配置。
    """

    runs = db.scalars(
        select(WorkflowRun).where(
            WorkflowRun.workflow_key.like("v1_%"),
            WorkflowRun.status.in_((RunStatus.PENDING, RunStatus.RUNNING)),
        )
    ).all()
    matching_runs = 0
    for run in runs:
        snapshot = run.input_snapshot or {}
        slots = snapshot.get("model_bindings") if isinstance(snapshot, dict) else None
        if not isinstance(slots, dict):
            continue
        if any(
            isinstance(binding, dict) and binding.get("model_profile_id") == profile_id
            for bindings in slots.values()
            if isinstance(bindings, list)
            for binding in bindings
        ):
            matching_runs += 1
    return matching_runs


def _profile_deletion_block_reason(
    db: Session,
    profile: ModelProfile,
    *,
    active_run_count: Optional[int] = None,
) -> Optional[str]:
    """返回阻止物理删除的原因；``None`` 表示可安全删除无历史的候选版本。"""

    active_runs = active_run_count if active_run_count is not None else active_v1_run_count_for_profile(db, profile.id)
    if active_runs:
        return f"该模型正被 {active_runs} 个进行中的 V1 任务冻结使用，任务结束前不能删除"
    if profile_has_model_invocations(db, profile.id):
        return "该模型已有调用记录，删除会破坏历史追溯；请保留或复制新版本"
    if db.scalar(select(ModelEvaluation.id).where(ModelEvaluation.model_profile_id == profile.id).limit(1)) is not None:
        return "该模型已有历史评测记录，不能删除"
    if db.scalar(select(ModelQualityEvaluation.id).where(ModelQualityEvaluation.model_profile_id == profile.id).limit(1)) is not None:
        return "该模型已进入质量报表历史，不能删除"
    # 本地模拟配置由初始化服务保证存在。允许停用，但删除后会在下一次初始化时被重新
    # 建立，容易造成“删除后又出现”的误解，因此明确把它视为系统保留配置。
    if (profile.adapter_key or profile.provider_key) == "mock_v1" and profile.model_key.startswith("mock-v1-"):
        return "这是系统本地模拟配置，可停用但不建议删除"
    return None


def model_profile_deletion_status(db: Session, profile: ModelProfile) -> tuple[int, Optional[str]]:
    """为模型中心计算删除开关和提示文案，不把安全判断留给浏览器。"""

    active_run_count = active_v1_run_count_for_profile(db, profile.id)
    return active_run_count, _profile_deletion_block_reason(db, profile, active_run_count=active_run_count)


def delete_v1_model_profile(db: Session, profile_id: str) -> None:
    """删除未使用且未被运行冻结的 V1 模型候选及其槽位绑定。

    已启用配置只要没有活动任务、没有历史调用或评测，同样允许显式删除；对应绑定会
    一并移除。真正的生产历史绝不删除，需改用“复制为新草稿”继续测试。
    """

    profile = db.get(ModelProfile, profile_id)
    if profile is None or profile.step_key not in V1_SLOT_ADAPTERS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="V1 模型配置不存在")
    reason = _profile_deletion_block_reason(db, profile)
    if reason:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=reason)
    bindings = db.scalars(
        select(ModelSlotProfileBinding).where(ModelSlotProfileBinding.model_profile_id == profile.id)
    ).all()
    for binding in bindings:
        db.delete(binding)
    db.delete(profile)
    db.commit()


def update_v1_model_profile(
    db: Session,
    *,
    profile_id: str,
    adapter: str,
    model_key: str,
    display_name: str,
    model_version: Optional[str],
    provider_config: dict[str, Any],
) -> ModelProfile:
    """更新尚未产生调用记录的同一模型版本。

    ``ModelInvocation`` 一旦存在，配置即为可追溯生产事实，必须通过复制创建新版本，
    不允许原地修改。Draft 与已启用但从未被调用的配置都可以安全编辑。
    """

    profile = db.get(ModelProfile, profile_id)
    if profile is None or profile.step_key not in V1_SLOT_ADAPTERS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="V1 模型配置不存在")
    if profile_has_model_invocations(db, profile.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该模型版本已产生调用记录，不能覆盖修改；请复制创建新版本",
        )
    clean_adapter = adapter.strip()
    clean_model_key = model_key.strip()
    clean_display_name = display_name.strip()
    if clean_adapter not in V1_SLOT_ADAPTERS[profile.step_key]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该模型协议不能用于当前 V1 功能")
    if not clean_model_key or not clean_display_name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="模型名称和显示名称不能为空")
    config = _normalize_v1_provider_config(provider_config)
    _validate_v1_profile_config(profile.step_key, clean_adapter, config)
    profile.provider_key = clean_adapter
    profile.adapter_key = clean_adapter
    profile.model_key = clean_model_key
    profile.display_name = clean_display_name
    profile.model_version = (model_version or clean_model_key).strip()[:160]
    profile.provider_config = config
    db.commit()
    db.refresh(profile)
    return profile


def copy_v1_model_profile(db: Session, profile_id: str) -> ModelProfile:
    """复制任一 V1 模型版本为同槽位下一版 DRAFT，历史版本和调用永不改写。"""

    source = db.get(ModelProfile, profile_id)
    if source is None or source.step_key not in V1_SLOT_ADAPTERS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="V1 模型配置不存在")
    version = db.scalar(select(func.max(ModelProfile.version)).where(ModelProfile.step_key == source.step_key)) or 0
    copied = ModelProfile(
        step_key=source.step_key,
        provider_key=source.provider_key,
        adapter_key=source.adapter_key or source.provider_key,
        model_key=source.model_key,
        model_version=source.model_version,
        display_name=f"{source.display_name or source.model_key}（复制草稿）"[:160],
        version=version + 1,
        profile_status="DRAFT",
        provider_config=deepcopy(source.provider_config),
        is_active=False,
    )
    db.add(copied)
    db.flush()
    source_binding = db.scalars(
        select(ModelSlotProfileBinding).where(ModelSlotProfileBinding.model_profile_id == source.id)
    ).first()
    slot = db.scalars(select(ModelSlot).where(ModelSlot.slot_key == source.step_key)).first()
    if slot is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="模型槽位不存在，无法复制配置")
    db.add(
        ModelSlotProfileBinding(
            slot_id=slot.id,
            model_profile_id=copied.id,
            is_enabled=False,
            priority=source_binding.priority if source_binding is not None else 100,
            weight=source_binding.weight if source_binding is not None else None,
        )
    )
    db.commit()
    db.refresh(copied)
    return copied


def list_v1_model_profiles(db: Session) -> list[tuple[ModelProfile, Optional[ModelSlotProfileBinding]]]:
    """列出 V1 配置版本与绑定状态，不把旧流程模型混入制作人界面。"""

    get_v1_definition(db)
    slot_ids = {slot.id for slot in list_model_slots(db)}
    profiles = list(
        db.scalars(
            select(ModelProfile)
            .where(ModelProfile.step_key.in_([spec[0] for spec in V1_SLOT_SPECS]))
            .order_by(ModelProfile.step_key, ModelProfile.version.desc())
        ).all()
    )
    bindings = {
        item.model_profile_id: item
        for item in db.scalars(
            select(ModelSlotProfileBinding).where(ModelSlotProfileBinding.slot_id.in_(slot_ids))
        ).all()
    }
    return [(profile, bindings.get(profile.id)) for profile in profiles]


def bind_profile_to_slot(
    db: Session,
    *,
    slot_key: str,
    model_profile_id: str,
    enabled: bool,
    priority: int,
    weight: Optional[float],
    replace_existing: bool = False,
) -> ModelSlotProfileBinding:
    """把一个已存在的安全模型配置绑定到能力槽位，不在此处读取真实密钥。"""

    slot = db.scalars(select(ModelSlot).where(ModelSlot.slot_key == slot_key)).first()
    if slot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模型槽位不存在")
    profile = db.get(ModelProfile, model_profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模型配置不存在")
    profile_adapter = profile.adapter_key or profile.provider_key
    allowed_adapters = V1_SLOT_ADAPTERS.get(slot.slot_key)
    if allowed_adapters is None or profile_adapter not in allowed_adapters:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该模型协议不能绑定到当前 V1 功能")
    if priority < 0 or priority > 10_000:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="priority 必须在 0 至 10000 之间")
    if weight is not None and not 0 < weight <= 1:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="weight 必须大于 0 且不超过 1")

    binding = db.scalars(
        select(ModelSlotProfileBinding).where(
            ModelSlotProfileBinding.slot_id == slot.id,
            ModelSlotProfileBinding.model_profile_id == profile.id,
        )
    ).first()
    if binding is None:
        if enabled and slot.selection_mode == ModelSelectionMode.SINGLE:
            existing_enabled = db.scalar(
                select(ModelSlotProfileBinding.id).where(
                    ModelSlotProfileBinding.slot_id == slot.id,
                    ModelSlotProfileBinding.is_enabled.is_(True),
                )
            )
            if existing_enabled is not None and not replace_existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="单模型槽位已有启用配置；请先停用旧配置后再切换",
                )
        binding = ModelSlotProfileBinding(
            slot_id=slot.id,
            model_profile_id=profile.id,
            is_enabled=enabled,
            priority=priority,
            weight=weight,
        )
        db.add(binding)
    else:
        if enabled and not binding.is_enabled and slot.selection_mode == ModelSelectionMode.SINGLE:
            existing_enabled = db.scalar(
                select(ModelSlotProfileBinding.id).where(
                    ModelSlotProfileBinding.slot_id == slot.id,
                    ModelSlotProfileBinding.is_enabled.is_(True),
                    ModelSlotProfileBinding.id != binding.id,
                )
            )
            if existing_enabled is not None and not replace_existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="单模型槽位已有启用配置；请先停用旧配置后再切换",
                )
        binding.is_enabled = enabled
        binding.priority = priority
        binding.weight = weight
    if enabled:
        profile.profile_status = "ACTIVE"
    else:
        profile.profile_status = "HISTORICAL" if profile_has_model_invocations(db, profile.id) else "DRAFT"
    if enabled and replace_existing and slot.selection_mode == ModelSelectionMode.SINGLE:
        for existing in db.scalars(
            select(ModelSlotProfileBinding).where(
                ModelSlotProfileBinding.slot_id == slot.id,
                ModelSlotProfileBinding.is_enabled.is_(True),
                ModelSlotProfileBinding.model_profile_id != profile.id,
            )
        ):
            existing.is_enabled = False
            previous_profile = db.get(ModelProfile, existing.model_profile_id)
            if previous_profile is not None:
                previous_profile.profile_status = (
                    "HISTORICAL" if profile_has_model_invocations(db, previous_profile.id) else "DRAFT"
                )
    db.commit()
    db.refresh(binding)
    return binding


def enabled_profiles_for_slot(db: Session, slot_key: str) -> list[ModelSlotProfileBinding]:
    """读取槽位可用模型，故事槽位可以返回多条，其余槽位至多一条。"""

    slot = db.scalars(select(ModelSlot).where(ModelSlot.slot_key == slot_key)).first()
    if slot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模型槽位不存在")
    bindings = list(
        db.scalars(
            select(ModelSlotProfileBinding)
            .where(ModelSlotProfileBinding.slot_id == slot.id, ModelSlotProfileBinding.is_enabled.is_(True))
            .order_by(ModelSlotProfileBinding.priority, ModelSlotProfileBinding.created_at)
        ).all()
    )
    if slot.selection_mode == ModelSelectionMode.SINGLE and len(bindings) > 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="单模型槽位存在多个启用配置，请在模型中心人工处理")
    return bindings


def _validate_variables_schema(value: dict[str, Any]) -> None:
    """限制模板变量定义为 JSON Schema 对象，避免 Prompt 渲染出现未声明变量。"""

    if not isinstance(value, dict):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="variables_schema 必须是 JSON 对象")
    properties = value.get("properties", {})
    if properties and not isinstance(properties, dict):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="variables_schema.properties 必须是对象")
    required = value.get("required", [])
    if required and (
        not isinstance(required, list) or not all(isinstance(item, str) for item in required)
    ):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="variables_schema.required 必须是字符串数组")


def create_prompt_template(
    db: Session,
    *,
    task_type: str,
    name: str,
    content: str,
    variables_schema: dict[str, Any],
) -> PromptTemplate:
    """创建一个 DRAFT Prompt 新版本，生产模板不可原地修改。"""

    clean_task_type = task_type.strip().upper()
    clean_name = name.strip()
    clean_content = content.strip()
    if not clean_task_type or not clean_name or not clean_content:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="任务类型、名称和模板内容不能为空")
    _validate_variables_schema(variables_schema)
    latest = db.scalar(
        select(func.max(PromptTemplate.version)).where(
            PromptTemplate.task_type == clean_task_type,
            PromptTemplate.name == clean_name,
        )
    ) or 0
    template = PromptTemplate(
        task_type=clean_task_type,
        name=clean_name,
        version=latest + 1,
        content=clean_content,
        variables_schema=deepcopy(variables_schema),
        status=PromptTemplateStatus.DRAFT,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def list_prompt_templates(db: Session, task_type: Optional[str] = None) -> list[PromptTemplate]:
    """列出 Prompt 历史版本；调用层只允许读取 ACTIVE 版本。"""

    statement = select(PromptTemplate).order_by(PromptTemplate.task_type, PromptTemplate.name, PromptTemplate.version.desc())
    if task_type:
        statement = statement.where(PromptTemplate.task_type == task_type.strip().upper())
    return list(db.scalars(statement).all())


def activate_prompt_template(db: Session, template_id: str) -> PromptTemplate:
    """激活一个模板版本，并归档同任务的旧活动版本。

    一个任务类型只能有一个生产 Prompt，避免 Worker 因“同任务多个 ACTIVE 模板”而
    按版本号猜测输入。模板名称只用于人类识别实验分支，不得改变这个运行时约束。
    """

    template = db.get(PromptTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prompt 模板不存在")
    for candidate in db.scalars(
        select(PromptTemplate).where(
            PromptTemplate.task_type == template.task_type,
            PromptTemplate.status == PromptTemplateStatus.ACTIVE,
        )
    ):
        candidate.status = PromptTemplateStatus.ARCHIVED
    template.status = PromptTemplateStatus.ACTIVE
    db.commit()
    db.refresh(template)
    return template


def archive_prompt_template(db: Session, template_id: str) -> PromptTemplate:
    """归档非生效 Prompt；不允许留下没有生产模板的任务类型。"""

    template = db.get(PromptTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prompt 模板不存在")
    if template.status == PromptTemplateStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="当前生效的 Prompt 不能直接归档；请先激活同一任务的另一版模板",
        )
    template.status = PromptTemplateStatus.ARCHIVED
    db.commit()
    db.refresh(template)
    return template
