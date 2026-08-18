"""Commerce 业务工作流预设与 StoryRun 配置冻结。

这里的预设刻意只管理产品生产的业务目标：平台、时长、画幅、节奏、审核推进方式
和已经由 Profile 声明的质量档位。它不是供应商请求 JSON，不能携带密钥、Header、
Base URL 或 Adapter 参数。创建 StoryRun 时解析一次并冻结，Worker 后续只消费冻结
快照，绝不重新读取当前活动预设、Prompt 或模型槽位。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import ceil
from time import sleep
from typing import Any, Mapping, Optional

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models import (
    CommerceStoryRunWorkflowConfig,
    CommerceWorkflowPresetDefinition,
    CommerceWorkflowPresetVersion,
    CommerceWorkflowPresetVersionStatus,
    ModelProfile,
    ModelSlot,
    PromptTemplateDefinition,
    PromptTemplateVersion,
    PromptTemplateVersionStatus,
    StoryRun,
)
from app.services.model_parameter_service import profile_parameter_config, resolve_effective_model_parameters
from app.services.provider_config_security import assert_safe_execution_metadata, redact_provider_config
from app.services.sensitive_data import is_sensitive_key, redact_sensitive_data
from app.services.v1_configuration_service import enabled_profiles_for_slot


CONFIG_SCHEMA_VERSION = 1
MAX_TARGET_DURATION_SECONDS = 120
MAX_CHAPTERS = 12
MAX_ESTIMATED_SHOTS = 24
MAX_ESTIMATED_IMAGES = 40
MAX_ESTIMATED_VIDEOS = 24

# 这些是 Commerce 正常生产链真正会消费的槽位；VIDEO_ANALYSIS 发生在 StoryRun 之前，
# 因而只作为上游分析审计保留，不假装属于 StoryRun 后续执行配置。
COMMERCE_SLOT_KEYS: tuple[str, ...] = (
    "STORY_GENERATE",
    "CHARACTER_DESIGN",
    "SCENE_DESIGN",
    "DIRECTOR_PLAN",
    "CHARACTER_IMAGE_GENERATE",
    "SCENE_IMAGE_GENERATE",
    "SHOT_KEYFRAME_GENERATE",
    "VIDEO_GENERATE",
    "FINAL_COMPOSE",
)

COMMERCE_PROMPT_KEYS: tuple[str, ...] = (
    "commerce.story_outline",
    "commerce.character_design",
    "commerce.scene_design",
    "commerce.director_storyboard",
    "commerce.image_prompt_organize",
    "commerce.keyframe_prompt_organize",
    "commerce.video_prompt_generate",
    "v1.video_prompt_generate",
)

_TEXT_SLOT_KEYS = {"STORY_GENERATE", "CHARACTER_DESIGN", "SCENE_DESIGN", "DIRECTOR_PLAN"}
_IMAGE_SLOT_KEYS = {"CHARACTER_IMAGE_GENERATE", "SCENE_IMAGE_GENERATE", "SHOT_KEYFRAME_GENERATE"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _error(detail: str, code: int = status.HTTP_422_UNPROCESSABLE_CONTENT) -> None:
    raise HTTPException(status_code=code, detail=detail)


class CommerceWorkflowBusinessConfig(BaseModel):
    """普通用户可编辑的强类型业务配置，不接受任意 JSON 字段。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=CONFIG_SCHEMA_VERSION, ge=CONFIG_SCHEMA_VERSION, le=CONFIG_SCHEMA_VERSION)
    target_platform: str = Field(default="douyin", min_length=1, max_length=40)
    target_duration_seconds: int = Field(default=30, ge=10, le=MAX_TARGET_DURATION_SECONDS)
    aspect_ratio: str = Field(default="9:16", pattern=r"^(9:16|16:9|1:1|3:4|4:3|21:9)$")
    language: str = Field(default="zh-CN", min_length=2, max_length=20)
    visual_style: str = Field(default="电影短剧写实风格", min_length=1, max_length=160)
    execution_mode: str = Field(default="STEPWISE", pattern=r"^(STEPWISE|AUTO)$")
    idea_candidate_count: int = Field(default=10, ge=1, le=10)
    run_variant_count: int = Field(default=1, ge=1, le=10)
    chapter_mode: str = Field(default="AUTO", pattern=r"^(AUTO|MANUAL)$")
    chapter_count: Optional[int] = Field(default=None, ge=1, le=MAX_CHAPTERS)
    pacing: str = Field(default="STANDARD", pattern=r"^(FAST|STANDARD|SLOW)$")
    target_shot_duration_seconds: int = Field(default=5, gt=0, le=15)
    product_integration: str = Field(default="STANDARD", pattern=r"^(LIGHT|STANDARD|STRONG)$")
    ending_interaction_enabled: bool = True
    cta_enabled: bool = True
    image_quality_preset: str = Field(default="standard", pattern=r"^(preview|standard|high)$")
    video_quality_preset: str = Field(default="standard", pattern=r"^(preview|standard|high)$")
    final_compose_quality_preset: str = Field(default="standard", pattern=r"^(preview|standard|high)$")

    @field_validator("target_platform", "language", "visual_style")
    @classmethod
    def _trim_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能是空文本")
        return value

    @model_validator(mode="after")
    def _validate_chapter_mode_and_shot_bound(self) -> "CommerceWorkflowBusinessConfig":
        if self.chapter_mode == "MANUAL" and self.chapter_count is None:
            raise ValueError("chapter_mode=MANUAL 时必须填写 chapter_count")
        if self.chapter_mode == "AUTO" and self.chapter_count is not None:
            raise ValueError("chapter_mode=AUTO 时不能填写 chapter_count")
        if ceil(self.target_duration_seconds / self.target_shot_duration_seconds) > MAX_ESTIMATED_SHOTS:
            raise ValueError("目标时长和单镜时长会产生超过安全上限的镜头数量")
        return self


_DEFAULT_STANDARD = CommerceWorkflowBusinessConfig().model_dump()
PRESET_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "preset_key": "preview",
        "display_name": "快速预览",
        "description": "用于低成本验证节奏与工作流；不改变审核和原创规则。",
        "config": {
            **_DEFAULT_STANDARD,
            "target_duration_seconds": 20,
            "idea_candidate_count": 3,
            "image_quality_preset": "preview",
            "video_quality_preset": "preview",
            "final_compose_quality_preset": "preview",
        },
    },
    {
        "preset_key": "standard",
        "display_name": "标准生产",
        "description": "保持现有 Commerce 默认生产行为与标准质量档位。",
        "config": deepcopy(_DEFAULT_STANDARD),
    },
    {
        "preset_key": "high",
        "display_name": "高质量",
        "description": "仅在当前所有活动 Profile 已明确支持 high 时可创建。",
        "config": {
            **_DEFAULT_STANDARD,
            "target_duration_seconds": 45,
            "idea_candidate_count": 10,
            "image_quality_preset": "high",
            "video_quality_preset": "high",
            "final_compose_quality_preset": "high",
        },
    },
)


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_hash(value: Mapping[str, Any]) -> str:
    return sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _assert_safe(value: Any, *, path: str = "workflow_config") -> None:
    """递归拒绝连接、鉴权和二进制内容，避免业务预设成为配置侧信道。"""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                _error(f"{path} 字段名必须为文本")
            normalized = "".join(ch for ch in key.casefold() if ch.isalnum())
            if (
                is_sensitive_key(key)
                or normalized in {
                    "apikey", "token", "secret", "credential", "authorization", "cookie", "header",
                    "headers", "url", "endpoint", "apibaseurl", "secretenvname", "adapterkey",
                    "modelkey", "providerconfig",
                }
            ):
                _error(f"{path}.{key} 不属于业务工作流配置")
            _assert_safe(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_safe(item, path=f"{path}[{index}]")
    elif isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered.startswith("data:") or "base64," in lowered or lowered.startswith("http://") or lowered.startswith("https://"):
            _error(f"{path} 不能包含 URL 或 Data URL")


def validate_business_config(value: Mapping[str, Any]) -> dict[str, Any]:
    _assert_safe(value)
    try:
        return CommerceWorkflowBusinessConfig.model_validate(dict(value)).model_dump()
    except ValidationError as exc:
        message = "; ".join(item["msg"] for item in exc.errors())
        _error(f"工作流业务配置无效：{message}")
    raise AssertionError("unreachable")  # pragma: no cover


def ensure_commerce_workflow_preset_foundation(db: Session) -> None:
    """幂等补齐内置预设；不覆盖任何人工版本或活动选择。"""

    for seed in PRESET_SEEDS:
        definition = db.scalar(
            select(CommerceWorkflowPresetDefinition).where(
                CommerceWorkflowPresetDefinition.preset_key == seed["preset_key"]
            )
        )
        if definition is None:
            definition = CommerceWorkflowPresetDefinition(
                preset_key=seed["preset_key"], display_name=seed["display_name"], description=seed["description"]
            )
            db.add(definition)
            db.flush()
        version = db.scalar(
            select(CommerceWorkflowPresetVersion).where(
                CommerceWorkflowPresetVersion.preset_definition_id == definition.id,
                CommerceWorkflowPresetVersion.version == 1,
            )
        )
        if version is None:
            config = validate_business_config(seed["config"])
            version = CommerceWorkflowPresetVersion(
                preset_definition_id=definition.id,
                version=1,
                status=CommerceWorkflowPresetVersionStatus.PUBLISHED,
                schema_version=CONFIG_SCHEMA_VERSION,
                config=config,
                content_hash=_content_hash(config),
                change_summary="系统初始已发布预设；保持当前 Commerce 默认行为。",
            )
            db.add(version)
            db.flush()
        if definition.active_version_id is None:
            definition.active_version_id = version.id
    db.flush()


def list_preset_definitions(db: Session) -> list[CommerceWorkflowPresetDefinition]:
    ensure_commerce_workflow_preset_foundation(db)
    return list(db.scalars(select(CommerceWorkflowPresetDefinition).order_by(CommerceWorkflowPresetDefinition.preset_key)).all())


def get_preset_definition(db: Session, preset_key: str) -> CommerceWorkflowPresetDefinition:
    ensure_commerce_workflow_preset_foundation(db)
    definition = db.scalar(
        select(CommerceWorkflowPresetDefinition).where(CommerceWorkflowPresetDefinition.preset_key == preset_key)
    )
    if definition is None:
        _error("工作流预设不存在", status.HTTP_404_NOT_FOUND)
    return definition


def list_preset_versions(db: Session, preset_key: str) -> list[CommerceWorkflowPresetVersion]:
    definition = get_preset_definition(db, preset_key)
    return list(
        db.scalars(
            select(CommerceWorkflowPresetVersion)
            .where(CommerceWorkflowPresetVersion.preset_definition_id == definition.id)
            .order_by(CommerceWorkflowPresetVersion.version.desc())
        ).all()
    )


def _version_for_request(
    db: Session, *, preset_key: str | None, preset_version_id: str | None
) -> tuple[CommerceWorkflowPresetDefinition, CommerceWorkflowPresetVersion]:
    ensure_commerce_workflow_preset_foundation(db)
    if preset_version_id:
        version = db.get(CommerceWorkflowPresetVersion, preset_version_id)
        if version is None:
            _error("工作流预设版本不存在", status.HTTP_404_NOT_FOUND)
        definition = db.get(CommerceWorkflowPresetDefinition, version.preset_definition_id)
        if definition is None or (preset_key and definition.preset_key != preset_key):
            _error("工作流预设版本不属于指定预设")
    else:
        definition = get_preset_definition(db, preset_key or "standard")
        if not definition.active_version_id:
            _error("工作流预设没有活动版本", status.HTTP_503_SERVICE_UNAVAILABLE)
        version = db.get(CommerceWorkflowPresetVersion, definition.active_version_id)
        if version is None:
            _error("工作流预设活动版本不存在", status.HTTP_503_SERVICE_UNAVAILABLE)
    if version.status != CommerceWorkflowPresetVersionStatus.PUBLISHED:
        _error("只能使用已发布的工作流预设版本")
    return definition, version


def _next_version_number(db: Session, definition_id: str) -> int:
    return int(
        db.scalar(
            select(func.max(CommerceWorkflowPresetVersion.version)).where(
                CommerceWorkflowPresetVersion.preset_definition_id == definition_id
            )
        )
        or 0
    ) + 1


def copy_preset_draft(
    db: Session, *, preset_key: str, source_version_id: str | None = None
) -> CommerceWorkflowPresetVersion:
    """复制 Published 历史版本为 Draft；并发时仅重试本地版本号分配。"""

    definition = get_preset_definition(db, preset_key)
    source = db.get(CommerceWorkflowPresetVersion, source_version_id) if source_version_id else db.get(
        CommerceWorkflowPresetVersion, definition.active_version_id
    )
    if source is None or source.preset_definition_id != definition.id:
        _error("源工作流预设版本不属于当前预设", status.HTTP_409_CONFLICT)
    for attempt in range(5):
        draft = CommerceWorkflowPresetVersion(
            preset_definition_id=definition.id,
            version=_next_version_number(db, definition.id),
            status=CommerceWorkflowPresetVersionStatus.DRAFT,
            schema_version=source.schema_version,
            config=deepcopy(source.config),
            content_hash=source.content_hash,
            change_summary=f"复制自 v{source.version}，等待编辑和发布。",
        )
        db.add(draft)
        try:
            db.commit()
            db.refresh(draft)
            return draft
        except (IntegrityError, OperationalError):
            db.rollback()
            if attempt < 4:
                sleep(0.02 * (attempt + 1))
    _error("并发创建工作流预设草稿冲突，请重试", status.HTTP_409_CONFLICT)
    raise AssertionError("unreachable")  # pragma: no cover


def update_preset_draft(
    db: Session, *, version_id: str, config: Mapping[str, Any], change_summary: str
) -> CommerceWorkflowPresetVersion:
    version = db.get(CommerceWorkflowPresetVersion, version_id)
    if version is None:
        _error("工作流预设版本不存在", status.HTTP_404_NOT_FOUND)
    if version.status != CommerceWorkflowPresetVersionStatus.DRAFT:
        _error("已发布预设不可编辑；请先复制创建新草稿", status.HTTP_409_CONFLICT)
    normalized = validate_business_config(config)
    version.schema_version = CONFIG_SCHEMA_VERSION
    version.config = normalized
    version.content_hash = _content_hash(normalized)
    version.change_summary = change_summary.strip()[:4000]
    db.commit()
    db.refresh(version)
    return version


def publish_preset_draft(db: Session, *, version_id: str) -> CommerceWorkflowPresetVersion:
    version = db.get(CommerceWorkflowPresetVersion, version_id)
    if version is None:
        _error("工作流预设版本不存在", status.HTTP_404_NOT_FOUND)
    if version.status != CommerceWorkflowPresetVersionStatus.DRAFT:
        _error("只有草稿预设可以发布", status.HTTP_409_CONFLICT)
    version.config = validate_business_config(version.config)
    version.content_hash = _content_hash(version.config)
    version.status = CommerceWorkflowPresetVersionStatus.PUBLISHED
    db.commit()
    db.refresh(version)
    return version


def activate_preset_version(
    db: Session, *, preset_key: str, version_id: str
) -> CommerceWorkflowPresetDefinition:
    definition = get_preset_definition(db, preset_key)
    version = db.get(CommerceWorkflowPresetVersion, version_id)
    if version is None or version.preset_definition_id != definition.id:
        _error("工作流预设版本不属于当前预设", status.HTTP_409_CONFLICT)
    if version.status != CommerceWorkflowPresetVersionStatus.PUBLISHED:
        _error("只有已发布预设可以激活", status.HTTP_409_CONFLICT)
    definition.active_version_id = version.id
    db.commit()
    db.refresh(definition)
    return definition


def _quality_for_slot(config: Mapping[str, Any], slot_key: str) -> str:
    if slot_key in _IMAGE_SLOT_KEYS:
        return str(config["image_quality_preset"])
    if slot_key == "VIDEO_GENERATE":
        return str(config["video_quality_preset"])
    if slot_key == "FINAL_COMPOSE":
        return str(config["final_compose_quality_preset"])
    return "standard"


def _profile_snapshot(
    db: Session, binding: Any, slot_key: str, *, quality_preset: str
) -> dict[str, Any]:
    profile = db.get(ModelProfile, binding.model_profile_id)
    slot = db.get(ModelSlot, binding.slot_id)
    if profile is None or slot is None:
        _error("模型槽位绑定引用的模型配置不存在", status.HTTP_503_SERVICE_UNAVAILABLE)
    adapter = profile.adapter_key or profile.provider_key
    # 运行冻结只保存已脱敏、可执行的非凭证配置。历史异常数据即使含有诱饵/旧敏感
    # 字段，也不能进入 Workflow 快照或触发环境变量读取。
    safe_provider_config = redact_provider_config(profile.provider_config)
    assert_safe_execution_metadata(adapter_key=adapter, provider_config=safe_provider_config)
    profile_snapshot = {
        "profile_id": profile.id,
        "adapter_key": adapter,
        "provider_key": profile.provider_key,
        "model_key": profile.model_key,
        "model_version": profile.model_version or profile.model_key,
        "display_name": profile.display_name or profile.model_key,
        "version": profile.version,
        "provider_config": safe_provider_config,
    }
    parameter_config, _ = profile_parameter_config(
        profile_snapshot["adapter_key"], profile_snapshot["provider_config"], profile.parameter_config
    )
    profile_snapshot["parameter_config"] = parameter_config
    execution_context: dict[str, Any] = {"operation": slot_key}
    if slot_key == "VIDEO_GENERATE":
        execution_context["input_mode"] = "first_frame"
    profile_snapshot["parameter_resolution"] = resolve_effective_model_parameters(
        profile_snapshot, preset=quality_preset, execution_context=execution_context
    )
    return {
        "position": binding.priority,
        "slot_id": binding.slot_id,
        "slot_key": slot_key,
        "slot_snapshot": {
            "id": slot.id,
            "slot_key": slot.slot_key,
            "capability": slot.capability,
            "selection_mode": slot.selection_mode.value,
            "description": slot.description,
        },
        "model_profile_id": profile.id,
        "profile_snapshot": profile_snapshot,
        "adapter_snapshot": {
            "key": profile.adapter_key or profile.provider_key,
            "provider_key": profile.provider_key,
            "model_key": profile.model_key,
            "model_version": profile.model_version or profile.model_key,
        },
    }


def _validate_aspect_ratio(config: Mapping[str, Any], bindings: Mapping[str, list[dict[str, Any]]]) -> None:
    """验证项目画幅与视频 Profile 的声明一致。

    Seedream 当前只暴露 ``size=2K``，未把画幅作为 Adapter 参数；关键帧画幅由已冻结
    分镜/首帧构图表达。Seedance 的首帧模式又会省略 ratio，因此仅检查其 *能力声明*
    是否能表达用户选择，而不把该字段错误发送给视频供应商。
    """

    videos = bindings.get("VIDEO_GENERATE") or []
    if not videos:
        _error("视频模型槽位没有可用 Profile", status.HTTP_503_SERVICE_UNAVAILABLE)
    ratio = config["aspect_ratio"]
    for binding in videos:
        profile_snapshot = binding.get("profile_snapshot") or {}
        # 测试/历史兼容用的 mock Profile 没有真实供应商能力声明。它不代表生产可用
        # 的视频模型；跳过它是为了让旧的无付费测试数据继续走兼容路径，真实 Adapter
        # 仍必须显式声明可用画幅。
        if profile_snapshot.get("adapter_key") == "mock_v1":
            return
        supported = (profile_snapshot.get("parameter_config") or {}).get("supported_parameters") or {}
        values = ((supported.get("aspect_ratio") or {}).get("values"))
        if isinstance(values, list) and ratio in values:
            return
    _error("当前视频 Profile 不支持所选成片画幅；请切换已声明该画幅的 Profile 或预设")


def _freeze_model_bindings(db: Session, config: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    bindings: dict[str, list[dict[str, Any]]] = {}
    for slot_key in COMMERCE_SLOT_KEYS:
        raw = enabled_profiles_for_slot(db, slot_key)
        if not raw:
            _error(f"模型槽位 {slot_key} 没有启用的模型配置", status.HTTP_503_SERVICE_UNAVAILABLE)
        quality = _quality_for_slot(config, slot_key)
        bindings[slot_key] = [
            _profile_snapshot(db, binding, slot_key, quality_preset=quality) for binding in raw
        ]
    _validate_aspect_ratio(config, bindings)
    return bindings


def _freeze_prompt_versions(db: Session) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for prompt_key in COMMERCE_PROMPT_KEYS:
        definition = db.scalar(
            select(PromptTemplateDefinition).where(PromptTemplateDefinition.prompt_key == prompt_key)
        )
        if definition is None or not definition.active_version_id:
            _error(f"Prompt {prompt_key} 没有活动版本", status.HTTP_503_SERVICE_UNAVAILABLE)
        version = db.get(PromptTemplateVersion, definition.active_version_id)
        if version is None or version.prompt_template_id != definition.id or version.status != PromptTemplateVersionStatus.PUBLISHED:
            _error(f"Prompt {prompt_key} 的活动版本无效", status.HTTP_503_SERVICE_UNAVAILABLE)
        result[prompt_key] = {
            "prompt_template_id": definition.id,
            "prompt_version_id": version.id,
            "prompt_version": version.version,
            "content_hash": version.content_hash,
            "operation_key": definition.operation_key,
            "model_slot_key": definition.model_slot_key,
            "capability": definition.capability,
            "output_contract_key": version.output_contract_key,
        }
    return result


def _estimate(config: Mapping[str, Any]) -> dict[str, int]:
    shots = ceil(int(config["target_duration_seconds"]) / int(config["target_shot_duration_seconds"]))
    chapters = int(config["chapter_count"]) if config["chapter_mode"] == "MANUAL" else max(1, ceil(int(config["target_duration_seconds"]) / 60))
    images = 1 + chapters + shots  # 单角色、每章一个场景、每镜一个关键帧的安全上界估算
    if shots > MAX_ESTIMATED_SHOTS or images > MAX_ESTIMATED_IMAGES or shots > MAX_ESTIMATED_VIDEOS:
        _error("预计镜头或媒体任务超过安全上限，请降低时长、章节数或提高单镜头时长")
    return {
        "estimated_chapters": chapters,
        "estimated_shots": shots,
        "estimated_character_images": 1,
        "estimated_scene_images": chapters,
        "estimated_keyframes": shots,
        "estimated_image_tasks": images,
        "estimated_video_tasks": shots,
    }


def resolve_story_run_workflow_config(
    db: Session,
    *,
    preset_key: str | None = None,
    preset_version_id: str | None = None,
    run_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """解析预设并冻结其真实可执行依赖；全过程不调用模型或创建任务。"""

    definition, version = _version_for_request(db, preset_key=preset_key, preset_version_id=preset_version_id)
    base = validate_business_config(version.config)
    requested = deepcopy(dict(run_overrides or {}))
    _assert_safe(requested, path="run_overrides")
    if "schema_version" in requested:
        _error("run_overrides 不允许修改 schema_version")
    unknown = sorted(set(requested) - set(base))
    if unknown:
        _error(f"run_overrides 不支持字段：{', '.join(unknown)}")
    effective = validate_business_config({**base, **requested})
    bindings = _freeze_model_bindings(db, effective)
    prompts = _freeze_prompt_versions(db)
    sources = {key: "preset" for key in effective}
    sources.update({key: "run_override" for key in requested})
    return {
        "preset_definition_id": definition.id,
        "preset_key": definition.preset_key,
        "preset_version_id": version.id,
        "preset_version": version.version,
        "preset_content_hash": version.content_hash,
        "requested_overrides": redact_sensitive_data(requested),
        "effective_workflow_config": effective,
        "config_sources": sources,
        "estimates": _estimate(effective),
        "model_bindings": bindings,
        "prompt_templates": prompts,
        "quality_presets_by_slot": {slot: _quality_for_slot(effective, slot) for slot in COMMERCE_SLOT_KEYS},
        "frozen_at": utcnow().isoformat(),
    }


def freeze_story_run_workflow_config(
    db: Session,
    *,
    story_run: StoryRun,
    preset_key: str | None = None,
    preset_version_id: str | None = None,
    run_overrides: Mapping[str, Any] | None = None,
    resolved: Mapping[str, Any] | None = None,
) -> CommerceStoryRunWorkflowConfig:
    if story_run.workflow_config_freeze is not None:
        _error("StoryRun 已冻结工作流配置，不能原地修改", status.HTTP_409_CONFLICT)
    resolved = dict(resolved) if resolved is not None else resolve_story_run_workflow_config(
        db, preset_key=preset_key, preset_version_id=preset_version_id, run_overrides=run_overrides
    )
    row = CommerceStoryRunWorkflowConfig(
        story_run_id=story_run.id,
        preset_definition_id=resolved["preset_definition_id"],
        preset_version_id=resolved["preset_version_id"],
        preset_version=resolved["preset_version"],
        preset_content_hash=resolved["preset_content_hash"],
        requested_overrides=deepcopy(resolved["requested_overrides"]),
        effective_workflow_config=deepcopy(resolved["effective_workflow_config"]),
        config_sources=deepcopy(resolved["config_sources"]),
        estimates=deepcopy(resolved["estimates"]),
        model_bindings=deepcopy(resolved["model_bindings"]),
        prompt_templates=deepcopy(resolved["prompt_templates"]),
    )
    db.add(row)
    db.flush()
    return row


def copy_story_run_workflow_config(
    db: Session, *, source_story_run: StoryRun, target_story_run: StoryRun
) -> CommerceStoryRunWorkflowConfig | None:
    """默认 rerun 逐字复制冻结配置；不重新读取活动预设/模型/Prompt。"""

    source = source_story_run.workflow_config_freeze
    if source is None:
        return None
    row = CommerceStoryRunWorkflowConfig(
        story_run_id=target_story_run.id,
        preset_definition_id=source.preset_definition_id,
        preset_version_id=source.preset_version_id,
        preset_version=source.preset_version,
        preset_content_hash=source.preset_content_hash,
        requested_overrides=deepcopy(source.requested_overrides),
        effective_workflow_config=deepcopy(source.effective_workflow_config),
        config_sources={**deepcopy(source.config_sources), "rerun": "source_frozen_config"},
        estimates=deepcopy(source.estimates),
        model_bindings=deepcopy(source.model_bindings),
        prompt_templates=deepcopy(source.prompt_templates),
    )
    db.add(row)
    db.flush()
    return row


def story_run_workflow_config_snapshot(story_run: StoryRun) -> dict[str, Any] | None:
    """返回安全、无正文的已冻结配置，供 Run/Step 快照与只读 API 复用。"""

    row = story_run.workflow_config_freeze
    if row is None:
        return None
    return {
        "preset_definition_id": row.preset_definition_id,
        "preset_version_id": row.preset_version_id,
        "preset_version": row.preset_version,
        "preset_content_hash": row.preset_content_hash,
        "requested_overrides": deepcopy(row.requested_overrides),
        "effective_workflow_config": deepcopy(row.effective_workflow_config),
        "config_sources": deepcopy(row.config_sources),
        "estimates": deepcopy(row.estimates),
        "model_bindings": deepcopy(row.model_bindings),
        "prompt_templates": deepcopy(row.prompt_templates),
    }
