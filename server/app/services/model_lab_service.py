"""内部 Model Lab 的实验编排服务。

Model Lab 不创建另一套供应商协议、队列或调用审计。它只把同一份冻结输入和候选
配置编排为已有的 :class:`WorkflowRun`、:class:`WorkflowStep` 与
:class:`ModelInvocation`。真实 Adapter 仍由 ``v1_model_adapter_service`` 管理；
本轮自动化测试只执行明确标记的 ``mock_v1`` 路径。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from time import perf_counter
from typing import Any, Iterable, Mapping

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import (
    CommerceCharacterReferenceImage,
    CommerceSceneReferenceImage,
    CommerceShotKeyframeVersion,
    CharacterReferenceImage,
    ModelExperiment,
    ModelExperimentCapability,
    ModelExperimentComparisonMode,
    ModelExperimentEvaluation,
    ModelExperimentStatus,
    ModelExperimentVariant,
    ModelInvocation,
    ModelProfile,
    ModelSlot,
    ModelSlotProfileBinding,
    PromptTemplateDefinition,
    PromptTemplateVersion,
    PromptTemplateVersionStatus,
    Project,
    SceneReferenceImage,
    ShotKeyframe,
    StoryRun,
    RunStatus,
    WorkflowDefinition,
    WorkflowDefinitionStatus,
    WorkflowRun,
    WorkflowStep,
)
from app.services.model_parameter_service import profile_parameter_config, resolve_effective_model_parameters
from app.services.prompt_template_service import render_prompt_version
from app.services.provider_config_security import redact_provider_config
from app.services.sensitive_data import redact_sensitive_data, sanitize_error_summary
from app.services.storage import local_asset_storage
from app.services.v1_configuration_service import V1_SLOT_ADAPTERS, bind_profile_to_slot
from app.services.v1_model_adapter_service import is_mock_adapter


MODEL_LAB_WORKFLOW_CODE = "LEMONFLOW_MODEL_LAB"
MODEL_LAB_WORKFLOW_VERSION = "ModelLab_V1"
MODEL_LAB_WORKFLOW_KEY = "model_lab_experiment"
MAX_VARIANTS = 4
MAX_REPEAT = 3
_LOCAL_MEDIA_PREFIX = "/media/generated/"
_FORBIDDEN_INPUT_KEYS = {
    "url",
    "uri",
    "path",
    "local_path",
    "file_path",
    "authorization",
    "headers",
    "cookie",
    "token",
    "api_key",
    "secret",
    "credential",
}
_ASSET_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
_SCORE_DIMENSIONS = {
    "text": {"instruction_following", "structure", "story_quality", "commerce_integration", "executability"},
    "image": {"prompt_alignment", "character_consistency", "scene_consistency", "product_fidelity", "visual_quality"},
    "video": {"motion_naturalness", "first_frame_consistency", "visual_consistency", "stability", "prompt_alignment"},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _error(detail: str, code: int = status.HTTP_422_UNPROCESSABLE_CONTENT) -> None:
    raise HTTPException(status_code=code, detail=detail)


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _capability(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _status(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _safe_text(value: Any, *, field: str, maximum: int = 20_000) -> str:
    if not isinstance(value, str) or not value.strip():
        _error(f"{field} 必须是非空文本")
    result = value.strip()
    if len(result) > maximum:
        _error(f"{field} 超过长度限制")
    lowered = result.casefold()
    if (
        "data:" in lowered
        or "base64," in lowered
        or "http://" in lowered
        or "https://" in lowered
        or result.startswith("/")
        or result.startswith("~/")
    ):
        _error(f"{field} 不允许 URL 或 Data URL")
    return result


def _safe_mapping(value: Any, *, path: str = "输入") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _error(f"{path} 必须是对象")
    result: dict[str, Any] = {}
    for raw_key, nested in value.items():
        if not isinstance(raw_key, str):
            _error(f"{path} 字段名必须是文本")
        key = raw_key.casefold().replace("-", "_")
        if key in _FORBIDDEN_INPUT_KEYS:
            _error(f"{path}.{raw_key} 不允许保存")
        if isinstance(nested, Mapping):
            result[raw_key] = _safe_mapping(nested, path=f"{path}.{raw_key}")
        elif isinstance(nested, list):
            result[raw_key] = [
                _safe_mapping(item, path=f"{path}.{raw_key}[{index}]") if isinstance(item, Mapping)
                else _safe_scalar(item, path=f"{path}.{raw_key}[{index}]")
                for index, item in enumerate(nested)
            ]
        else:
            result[raw_key] = _safe_scalar(nested, path=f"{path}.{raw_key}")
    return result


def _safe_scalar(value: Any, *, path: str) -> Any:
    if isinstance(value, str):
        return _safe_text(value, field=path)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    _error(f"{path} 包含不支持的值")


def _asset_metadata(value: Any, *, role: str) -> dict[str, Any]:
    item = _safe_mapping(value, path=f"{role} 资产")
    required = {"asset_id", "sha256", "mime_type", "width", "height"}
    permitted = required | {"role"}
    if set(item) != required and set(item) != permitted:
        _error(f"{role} 资产必须仅包含 asset_id、sha256、mime_type、width、height")
    if "role" in item and role != "reference" and item["role"] != role:
        _error(f"{role} 资产角色不匹配")
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", str(item["asset_id"])):
        _error(f"{role} 资产 ID 无效")
    if not re.fullmatch(r"[0-9a-f]{64}", str(item["sha256"]).casefold()):
        _error(f"{role} 资产 SHA-256 无效")
    if item["mime_type"] not in _ASSET_MIME_TYPES:
        _error(f"{role} 资产 MIME 不支持")
    if any(isinstance(item[field], bool) or not isinstance(item[field], int) or item[field] <= 0 for field in ("width", "height")):
        _error(f"{role} 资产尺寸无效")
    return {"asset_id": item["asset_id"], "sha256": item["sha256"].casefold(), "mime_type": item["mime_type"], "width": item["width"], "height": item["height"], "role": role}


def _verified_asset_metadata(db: Session, *, project_id: str, value: Any, role: str, require_keyframe: bool) -> dict[str, Any]:
    """Resolve one existing local image asset without accepting paths or URLs.

    Input contracts intentionally expose only an asset ID plus its immutable
    metadata.  The server resolves the record's trusted media URL, verifies
    MIME/magic/dimensions/SHA beneath the managed media root, then persists
    only the verified audit metadata.  ``include_data_url=False`` guarantees
    preflight never creates Base64 in the API process.
    """

    requested = _asset_metadata(value, role=role)
    asset_id = requested["asset_id"]
    candidate: tuple[Any, str, str] | None = None
    for model, namespace_field, trusted_role, is_keyframe in (
        (CharacterReferenceImage, "character_id", "character", False),
        (SceneReferenceImage, "scene_id", "scene", False),
        (ShotKeyframe, "shot_id", "first_frame", True),
    ):
        row = db.get(model, asset_id)
        if row is not None and row.project_id == project_id and require_keyframe == is_keyframe:
            candidate = (row, namespace_field, trusted_role)
            break
    if candidate is None:
        for model, namespace_field, trusted_role, is_keyframe in (
            (CommerceCharacterReferenceImage, "role_id", "character", False),
            (CommerceSceneReferenceImage, "scene_id", "scene", False),
            (CommerceShotKeyframeVersion, "shot_id", "first_frame", True),
        ):
            row = db.get(model, asset_id)
            story_run = db.get(StoryRun, row.story_run_id) if row is not None else None
            if row is not None and story_run is not None and story_run.project_id == project_id and require_keyframe == is_keyframe:
                candidate = (row, namespace_field, trusted_role)
                break
    if candidate is None:
        _error("参考资产不存在、不属于当前项目或不是允许的锁定图片", status.HTTP_409_CONFLICT)
    row, namespace_field, trusted_role = candidate
    review_status = _status(getattr(row, "review_status", getattr(row, "status", "")))
    if review_status != "LOCKED":
        _error("参考资产尚未审核锁定", status.HTTP_409_CONFLICT)
    image_url = getattr(row, "image_url", None)
    if not isinstance(image_url, str) or not image_url:
        _error("参考资产尚未保存受控本地图片", status.HTTP_409_CONFLICT)
    try:
        reference = local_asset_storage.load_generated_image_reference(
            project_id=project_id,
            asset_id=row.id,
            role=trusted_role,
            image_url=image_url,
            storage_namespace_id=getattr(row, namespace_field),
            include_data_url=False,
        )
    except RuntimeError as exc:
        _error(f"参考资产校验失败：{sanitize_error_summary(exc)}", status.HTTP_409_CONFLICT)
    verified = reference.audit_metadata()
    if (
        verified["sha256"] != requested["sha256"]
        or verified["mime_type"] != requested["mime_type"]
        or verified["width"] != requested["width"]
        or verified["height"] != requested["height"]
    ):
        _error("参考资产的 MIME、尺寸或 SHA-256 与冻结元数据不一致", status.HTTP_409_CONFLICT)
    return verified


def _validate_input(db: Session, project_id: str, capability: str, source_type: str, raw_input: Any) -> dict[str, Any]:
    payload = _safe_mapping(raw_input, path="实验输入")
    if capability == "text":
        if source_type not in {"text", "frozen_workflow"} or set(payload) != {"text"}:
            _error("文本实验只接受受控 text 或脱敏冻结输入")
        return {"source_type": source_type, "text": _safe_text(payload["text"], field="实验输入.text")}
    if capability == "image":
        if source_type != "image_prompt" or not {"prompt", "reference_assets"}.issuperset(payload):
            _error("图片实验只接受 prompt 和可选 reference_assets")
        prompt = _safe_text(payload.get("prompt"), field="实验输入.prompt")
        references = payload.get("reference_assets", [])
        if not isinstance(references, list) or len(references) > 14:
            _error("图片参考资产数量必须在 0 至 14 之间")
        return {
            "source_type": source_type,
            "prompt": prompt,
            "reference_assets": [
                _verified_asset_metadata(db, project_id=project_id, value=item, role="reference", require_keyframe=False)
                for item in references
            ],
        }
    if capability == "video":
        if source_type != "locked_keyframe" or set(payload) != {"video_prompt", "keyframe_asset"}:
            _error("视频实验只接受锁定关键帧和冻结视频 Prompt")
        return {
            "source_type": source_type,
            "video_prompt": _safe_text(payload["video_prompt"], field="实验输入.video_prompt"),
            "keyframe_asset": _verified_asset_metadata(
                db, project_id=project_id, value=payload["keyframe_asset"], role="first_frame", require_keyframe=True
            ),
        }
    _error("Model Lab capability 仅支持 text、image、video")


def _revalidate_frozen_input_assets(db: Session, experiment: ModelExperiment) -> None:
    """Re-read local image bytes before authorizing a supplier-call attempt.

    The experiment stores only frozen audit metadata. A subsequent Start must
    prove that the controlled asset still exists under its project root, has a
    valid image header, and matches its locked MIME, dimensions and digest.
    """

    capability = _capability(experiment.capability)
    snapshot = experiment.sanitized_input_snapshot or {}
    if capability == "image":
        for item in snapshot.get("reference_assets", []):
            _verified_asset_metadata(
                db,
                project_id=experiment.project_id,
                value=item,
                role="reference",
                require_keyframe=False,
            )
    elif capability == "video":
        _verified_asset_metadata(
            db,
            project_id=experiment.project_id,
            value=snapshot.get("keyframe_asset"),
            role="first_frame",
            require_keyframe=True,
        )


def _profile_snapshot(profile: ModelProfile, slot: ModelSlot, parameter_resolution: dict[str, Any]) -> dict[str, Any]:
    return {
        "slot_id": slot.id,
        "slot_key": slot.slot_key,
        "model_profile_id": profile.id,
        "profile_snapshot": {
            "profile_id": profile.id,
            "adapter_key": profile.adapter_key or profile.provider_key,
            "model_key": profile.model_key,
            "model_version": profile.model_version or profile.model_key,
            "version": profile.version,
            "provider_config": redact_provider_config(profile.provider_config),
            "parameter_config": profile_parameter_config(
                profile.adapter_key or profile.provider_key, profile.provider_config, profile.parameter_config
            )[0],
            "parameter_resolution": deepcopy(parameter_resolution),
        },
    }


def _prompt_snapshot(definition: PromptTemplateDefinition, version: PromptTemplateVersion, variables: dict[str, Any]) -> dict[str, Any]:
    rendered = render_prompt_version(version, variables)
    return {
        "prompt_key": definition.prompt_key,
        "display_name": definition.display_name,
        "prompt_template_id": definition.id,
        "prompt_version_id": version.id,
        "prompt_version": version.version,
        "content_hash": version.content_hash,
        "operation_key": definition.operation_key,
        "model_slot_key": definition.model_slot_key,
        "capability": definition.capability,
        "output_contract_key": version.output_contract_key,
        **rendered,
    }


def _definition(db: Session) -> WorkflowDefinition:
    row = db.scalar(
        select(WorkflowDefinition).where(
            WorkflowDefinition.workflow_code == MODEL_LAB_WORKFLOW_CODE,
            WorkflowDefinition.version == MODEL_LAB_WORKFLOW_VERSION,
        )
    )
    if row is not None:
        return row
    row = WorkflowDefinition(
        workflow_code=MODEL_LAB_WORKFLOW_CODE,
        version=MODEL_LAB_WORKFLOW_VERSION,
        status=WorkflowDefinitionStatus.PUBLISHED,
        published_at=_now(),
        definition_json={
            "workflow_key": MODEL_LAB_WORKFLOW_KEY,
            "nodes": ["VARIANT_EXECUTION"],
            "description": "内部模型对比；实际审计复用 WorkflowStep 与 ModelInvocation。",
        },
    )
    db.add(row)
    db.flush()
    return row


def _get_slot(db: Session, slot_key: str, capability: str) -> ModelSlot:
    slot = db.scalar(select(ModelSlot).where(ModelSlot.slot_key == slot_key))
    if slot is None:
        _error("模型槽位不存在", status.HTTP_404_NOT_FOUND)
    if not slot.is_enabled:
        _error("模型槽位已停用", status.HTTP_409_CONFLICT)
    expected = slot.capability.casefold()
    aliases = {
        "text": {"text", "video_analysis", "story_generate", "character_design", "scene_design", "director_plan"},
        "image": {"image", "image_generate"},
        "video": {"video", "video_generate"},
    }
    if expected not in aliases.get(capability, set()):
        _error("模型槽位与实验能力不匹配", status.HTTP_409_CONFLICT)
    return slot


def _get_prompt(db: Session, *, prompt_version_id: str, operation_key: str, slot_key: str, capability: str) -> tuple[PromptTemplateDefinition, PromptTemplateVersion]:
    version = db.get(PromptTemplateVersion, prompt_version_id)
    if version is None or version.status != PromptTemplateVersionStatus.PUBLISHED:
        _error("Model Lab 只能选择已发布 Prompt 版本", status.HTTP_409_CONFLICT)
    definition = db.get(PromptTemplateDefinition, version.prompt_template_id)
    if definition is None:
        _error("Prompt 定义不存在", status.HTTP_409_CONFLICT)
    if definition.operation_key != operation_key or definition.model_slot_key != slot_key or definition.capability != capability:
        _error("Prompt 版本与当前业务操作、模型槽位或能力不匹配", status.HTTP_409_CONFLICT)
    return definition, version


def _get_profile(db: Session, *, profile_id: str, slot_key: str, capability: str) -> ModelProfile:
    profile = db.get(ModelProfile, profile_id)
    if profile is None:
        _error("候选模型 Profile 不存在", status.HTTP_404_NOT_FOUND)
    adapter = profile.adapter_key or profile.provider_key
    if adapter not in V1_SLOT_ADAPTERS.get(slot_key, set()):
        _error("候选模型 Adapter 不能用于当前模型槽位", status.HTTP_409_CONFLICT)
    # mock_v1 是现有测试/联调 Adapter。它在 V1 的多个能力槽位已有固定模拟分支，
    # 因而 Model Lab 测试可用同一个 mock Profile 覆盖 text/image/video；这不扩展
    # 任何真实 Adapter 或供应商协议。
    if adapter == "mock_v1":
        return profile
    config, _ = profile_parameter_config(adapter, profile.provider_config, profile.parameter_config)
    if config["capability"] != capability:
        _error("候选模型 Profile 的能力与实验不匹配", status.HTTP_409_CONFLICT)
    return profile


def _validate_mode(mode: str, frozen: list[dict[str, Any]]) -> list[str]:
    if len(frozen) < 2:
        _error("实验至少需要两个 Variant")
    dimensions: list[str] = []
    profiles = {item["profile"].id for item in frozen}
    prompts = {item["prompt_version"].id for item in frozen}
    parameters = {_stable_json(item["resolution"]["effective_parameters"]) for item in frozen}
    if mode == "MODEL_ONLY":
        if len(profiles) < 2 or len(prompts) != 1 or len(parameters) != 1:
            _error("MODEL_ONLY 必须只改变 ModelProfile，且 Prompt 与有效参数完全一致")
        return ["model_profile"]
    if mode == "PROMPT_ONLY":
        if len(prompts) < 2 or len(profiles) != 1 or len(parameters) != 1:
            _error("PROMPT_ONLY 必须只改变已发布 Prompt 版本")
        return ["prompt_version"]
    if mode == "PARAMETER_ONLY":
        if len(parameters) < 2 or len(profiles) != 1 or len(prompts) != 1:
            _error("PARAMETER_ONLY 必须只改变质量预设或参数")
        return ["parameters"]
    if mode in {"CUSTOM", "NATIVE_PRESET"}:
        if len(profiles) > 1:
            dimensions.append("model_profile")
        if len(prompts) > 1:
            dimensions.append("prompt_version")
        if len(parameters) > 1:
            dimensions.append("parameters")
        return dimensions or ["no_effective_difference"]
    _error("comparison_mode 不支持")


def _model_only_common_parameters(frozen: list[dict[str, Any]]) -> None:
    """将 MODEL_ONLY 的参数冻结为全部候选共同支持的部分。

    同一个 ``standard`` 预设在两个 Profile 上可能各自带有额外的模型私有参数。
    这些参数不能随着某一个模型进入公平对比；服务会在供应商调用前将其省略并把
    省略原因写入冻结快照。若调用方明确覆盖非公共参数，则拒绝实验而不是静默转换。
    """

    support_sets: list[set[str]] = []
    for item in frozen:
        profile = item["profile"]
        adapter = profile.adapter_key or profile.provider_key
        if adapter == "mock_v1":
            support_sets.append(set())
            continue
        config, _ = profile_parameter_config(adapter, profile.provider_config, profile.parameter_config)
        support_sets.append(set((config.get("supported_parameters") or {}).keys()))
    common = set.intersection(*support_sets) if support_sets else set()
    available_anywhere = set.union(*support_sets) if support_sets else set()
    requested = set().union(*(set(item["requested_overrides"].keys()) for item in frozen))
    if requested - common:
        _error("MODEL_ONLY 参数覆盖必须被所有候选模型共同支持")
    if available_anywhere and not common:
        _error("候选模型没有公共参数能力；请改用 CUSTOM 或 NATIVE_PRESET")
    for item in frozen:
        resolution = item["resolution"]
        effective = dict(resolution.get("effective_parameters") or {})
        omitted = list(resolution.get("omitted_parameters") or [])
        for key in sorted(set(effective) - common):
            omitted.append(
                {
                    "parameter": key,
                    "reason": "MODEL_ONLY 仅使用所有候选共同支持的参数",
                }
            )
        resolution["effective_parameters"] = {key: effective[key] for key in sorted(common) if key in effective}
        sources = dict(resolution.get("parameter_sources") or {})
        resolution["parameter_sources"] = {key: sources[key] for key in sorted(common) if key in sources}
        resolution["omitted_parameters"] = omitted
        resolution["common_parameter_keys"] = sorted(common)
    first = frozen[0]["resolution"]["effective_parameters"]
    if any(item["resolution"]["effective_parameters"] != first for item in frozen[1:]):
        _error("候选模型的公共有效参数取值不一致；请改用 CUSTOM 或 NATIVE_PRESET")


def _build_blueprint(db: Session, payload: Mapping[str, Any]) -> dict[str, Any]:
    project_id = _safe_text(payload.get("project_id"), field="project_id", maximum=64)
    if db.get(Project, project_id) is None:
        _error("项目不存在", status.HTTP_404_NOT_FOUND)
    name = _safe_text(payload.get("name"), field="实验名称", maximum=160)
    description = str(payload.get("description") or "").strip()[:2000]
    operation_key = _safe_text(payload.get("operation_key"), field="operation_key", maximum=120)
    slot_key = _safe_text(payload.get("model_slot_key"), field="model_slot_key", maximum=80)
    capability = _capability(payload.get("capability"))
    comparison_mode = _capability(payload.get("comparison_mode"))
    input_source_type = _safe_text(payload.get("input_source_type"), field="input_source_type", maximum=40)
    input_snapshot = _validate_input(db, project_id, capability, input_source_type, payload.get("input_payload"))
    variables = _safe_mapping(payload.get("prompt_variables") or {}, path="Prompt 变量")
    max_create_calls = payload.get("max_create_calls")
    repeat = payload.get("repeat", 1)
    variants = payload.get("variants")
    if isinstance(repeat, bool) or not isinstance(repeat, int) or not 1 <= repeat <= MAX_REPEAT:
        _error("repeat 必须在 1 至 3 之间")
    if isinstance(max_create_calls, bool) or not isinstance(max_create_calls, int) or max_create_calls < 1:
        _error("max_create_calls 必须为正整数")
    if not isinstance(variants, list) or not 2 <= len(variants) <= MAX_VARIANTS:
        _error("候选 Variant 数量必须在 2 至 4 之间")
    slot = _get_slot(db, slot_key, capability)
    frozen: list[dict[str, Any]] = []
    labels: set[str] = set()
    for raw in variants:
        item = _safe_mapping(raw, path="Variant")
        label = _safe_text(item.get("label"), field="Variant label", maximum=120)
        if label in labels:
            _error("Variant label 不能重复")
        labels.add(label)
        profile = _get_profile(db, profile_id=_safe_text(item.get("model_profile_id"), field="model_profile_id", maximum=64), slot_key=slot_key, capability=capability)
        definition, prompt_version = _get_prompt(
            db,
            prompt_version_id=_safe_text(item.get("prompt_template_version_id"), field="prompt_template_version_id", maximum=64),
            operation_key=operation_key,
            slot_key=slot_key,
            capability=capability,
        )
        preset = item.get("parameter_preset", "standard")
        if not isinstance(preset, str):
            _error("parameter_preset 必须是文本")
        overrides = _safe_mapping(item.get("requested_overrides") or {}, path="参数覆盖")
        if (profile.adapter_key or profile.provider_key) == "mock_v1":
            if preset not in {"preview", "standard", "high"} or overrides:
                _error("Mock Model Lab 只支持无覆盖的 preview、standard 或 high 预设")
            resolution = {
                "schema_version": 1,
                "capability": capability,
                "selected_preset": preset,
                "requested_overrides": {},
                "effective_parameters": {},
                "parameter_sources": {},
                "omitted_parameters": [],
                "parameter_config_complete": True,
                "execution_context": {"operation": operation_key},
            }
        else:
            resolution = resolve_effective_model_parameters(
                {
                    "adapter_key": profile.adapter_key or profile.provider_key,
                    "provider_config": redact_provider_config(profile.provider_config),
                    "parameter_config": profile.parameter_config,
                },
                preset=preset,
                run_overrides=overrides,
                execution_context={"operation": operation_key, "input_mode": "first_frame" if capability == "video" else "text"},
            )
        prompt = _prompt_snapshot(definition, prompt_version, variables)
        frozen.append({"label": label, "profile": profile, "prompt_definition": definition, "prompt_version": prompt_version, "resolution": resolution, "prompt": prompt, "requested_overrides": overrides})
    if comparison_mode == "MODEL_ONLY":
        _model_only_common_parameters(frozen)
    differing = _validate_mode(comparison_mode, frozen)
    estimated = len(frozen) * repeat
    if max_create_calls < estimated:
        _error(f"调用预算不足：预计 {estimated} 次，max_create_calls 至少应为 {estimated}")
    return {
        "project_id": project_id,
        "name": name,
        "description": description,
        "operation_key": operation_key,
        "slot": slot,
        "capability": capability,
        "comparison_mode": comparison_mode,
        "input_source_type": input_source_type,
        "input_snapshot": input_snapshot,
        "input_hash": sha256(_stable_json(input_snapshot).encode("utf-8")).hexdigest(),
        "variables": variables,
        "repeat": repeat,
        "max_create_calls": max_create_calls,
        "estimated_create_calls": estimated,
        "variants": frozen,
        "differing_dimensions": differing,
    }


def _variant_config_hash(variants: Iterable[ModelExperimentVariant]) -> str:
    """Fingerprint only immutable Variant inputs, never runtime result fields."""

    payload = [
        {
            "id": item.id,
            "label": item.label,
            "repeat_index": item.repeat_index,
            "model_profile_id": item.model_profile_id,
            "model_profile_version": item.model_profile_version,
            "prompt_template_version_id": item.prompt_template_version_id,
            "parameter_preset": item.parameter_preset,
            "requested_overrides": item.requested_overrides,
            "effective_parameters": item.effective_parameters,
            "model_profile_snapshot": item.model_profile_snapshot,
            "prompt_snapshot": item.prompt_snapshot,
            "input_snapshot": item.input_snapshot,
        }
        for item in variants
    ]
    return sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def _preflight_hash(
    *, experiment_id: str | None, input_hash: str, variant_config_hash: str,
    expected_create_calls: int, max_create_calls: int,
) -> str:
    return sha256(
        _stable_json(
            {
                "experiment_id": experiment_id,
                "input_hash": input_hash,
                "variant_config_hash": variant_config_hash,
                "expected_create_calls": expected_create_calls,
                "max_create_calls": max_create_calls,
            }
        ).encode("utf-8")
    ).hexdigest()


def _key_checks(variants: Iterable[ModelExperimentVariant]) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    for variant in variants:
        snapshot = (variant.model_profile_snapshot or {}).get("profile_snapshot") or {}
        adapter = str(snapshot.get("adapter_key") or "")
        config = snapshot.get("provider_config") or {}
        env_name = config.get("secret_env_name") if isinstance(config, Mapping) else None
        checks.append(
            {
                "profile_id": variant.model_profile_id,
                "key_status": "NOT_REQUIRED"
                if adapter == "mock_v1"
                else ("SET" if isinstance(env_name, str) and env_name and bool(os.environ.get(env_name)) else "MISSING"),
            }
        )
    return checks


def _preflight_payload(
    *, experiment_id: str | None, capability: str, comparison_mode: str,
    variants: list[ModelExperimentVariant], input_hash: str, max_create_calls: int,
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    expected = len(variants)
    variant_hash = _variant_config_hash(variants)
    return {
        "valid": True,
        "experiment_id": experiment_id,
        "preflight_hash": _preflight_hash(
            experiment_id=experiment_id,
            input_hash=input_hash,
            variant_config_hash=variant_hash,
            expected_create_calls=expected,
            max_create_calls=max_create_calls,
        ),
        "variant_config_hash": variant_hash,
        "checked_at": checked_at or _now(),
        "variant_count": len(variants),
        "repeat": max((item.repeat_index for item in variants), default=0),
        "estimated_create_calls": expected,
        "expected_create_call_count": expected,
        "max_create_calls": max_create_calls,
        "text_calls": expected if capability == "text" else 0,
        "image_create_calls": expected if capability == "image" else 0,
        "video_create_calls": expected if capability == "video" else 0,
        "differing_dimensions": _comparison_dimensions_from_variants(comparison_mode, variants),
        "key_checks": _key_checks(variants),
        "parameters": [
            {
                "label": item.label,
                "effective_parameters": deepcopy((item.effective_parameters or {}).get("effective_parameters", {})),
                "omitted_parameters": deepcopy((item.effective_parameters or {}).get("omitted_parameters", [])),
            }
            for item in variants
        ],
    }


def _comparison_dimensions_from_variants(mode: str, variants: list[ModelExperimentVariant]) -> list[str]:
    profiles = {item.model_profile_id for item in variants}
    prompts = {item.prompt_template_version_id for item in variants}
    parameters = {_stable_json((item.effective_parameters or {}).get("effective_parameters", {})) for item in variants}
    if mode == "MODEL_ONLY":
        return ["model_profile"]
    if mode == "PROMPT_ONLY":
        return ["prompt_version"]
    if mode == "PARAMETER_ONLY":
        return ["parameters"]
    dimensions = []
    if len(profiles) > 1:
        dimensions.append("model_profile")
    if len(prompts) > 1:
        dimensions.append("prompt_version")
    if len(parameters) > 1:
        dimensions.append("parameters")
    return dimensions or ["no_effective_difference"]


def preflight_experiment(db: Session, payload: Mapping[str, Any]) -> dict[str, Any]:
    """纯本地校验，绝不创建 Workflow/Invocation 或访问 Adapter。"""

    blueprint = _build_blueprint(db, payload)
    # Draft preflight has no persistent experiment ID and cannot be used to
    # start work.  The authoritative preflight is refreshed after creation.
    virtual_variants = [
        ModelExperimentVariant(
            id=f"draft-{index}", label=item["label"], model_profile_id=item["profile"].id,
            model_profile_version=item["profile"].version, prompt_template_version_id=item["prompt_version"].id,
            parameter_preset=item["resolution"]["selected_preset"], requested_overrides=deepcopy(item["requested_overrides"]),
            effective_parameters=deepcopy(item["resolution"]),
            model_profile_snapshot=_profile_snapshot(item["profile"], blueprint["slot"], item["resolution"]),
            prompt_snapshot=deepcopy(item["prompt"]), input_snapshot=deepcopy(blueprint["input_snapshot"]), repeat_index=1,
        )
        for index, item in enumerate(blueprint["variants"], start=1)
    ]
    result = _preflight_payload(
        experiment_id=None, capability=blueprint["capability"], comparison_mode=blueprint["comparison_mode"],
        variants=virtual_variants, input_hash=blueprint["input_hash"], max_create_calls=blueprint["max_create_calls"],
    )
    # Virtual rows model one candidate each; expand the declared estimates by
    # repeat without persisting any experiment, WorkflowRun, Step or Invocation.
    expected = blueprint["estimated_create_calls"]
    result.update({
        "valid": True,
        "repeat": blueprint["repeat"],
        "estimated_create_calls": expected,
        "expected_create_call_count": expected,
        "text_calls": expected if blueprint["capability"] == "text" else 0,
        "image_create_calls": expected if blueprint["capability"] == "image" else 0,
        "video_create_calls": expected if blueprint["capability"] == "video" else 0,
        "differing_dimensions": blueprint["differing_dimensions"],
    })
    return result


def create_experiment(db: Session, payload: Mapping[str, Any]) -> ModelExperiment:
    blueprint = _build_blueprint(db, payload)
    row = ModelExperiment(
        project_id=blueprint["project_id"], name=blueprint["name"], description=blueprint["description"],
        operation_key=blueprint["operation_key"], model_slot_key=blueprint["slot"].slot_key,
        capability=ModelExperimentCapability(blueprint["capability"]),
        comparison_mode=ModelExperimentComparisonMode(blueprint["comparison_mode"]),
        input_source_type=blueprint["input_source_type"], sanitized_input_snapshot=deepcopy(blueprint["input_snapshot"]),
        input_hash=blueprint["input_hash"], max_create_calls=blueprint["max_create_calls"], status=ModelExperimentStatus.READY,
    )
    db.add(row)
    db.flush()
    for item in blueprint["variants"]:
        profile_snapshot = _profile_snapshot(item["profile"], blueprint["slot"], item["resolution"])
        for repeat_index in range(1, blueprint["repeat"] + 1):
            db.add(ModelExperimentVariant(
                experiment_id=row.id, label=item["label"], model_profile_id=item["profile"].id,
                model_profile_version=item["profile"].version, prompt_template_version_id=item["prompt_version"].id,
                parameter_preset=item["resolution"]["selected_preset"], requested_overrides=deepcopy(item["requested_overrides"]),
                effective_parameters=deepcopy(item["resolution"]), model_profile_snapshot=profile_snapshot,
                prompt_snapshot=deepcopy(item["prompt"]), input_snapshot=deepcopy(blueprint["input_snapshot"]),
                repeat_index=repeat_index, status=RunStatus.PENDING,
            ))
    db.flush()
    _refresh_preflight(db, row)
    db.commit()
    db.refresh(row)
    return row


def _experiment_variants(db: Session, experiment_id: str) -> list[ModelExperimentVariant]:
    return list(db.scalars(select(ModelExperimentVariant).where(ModelExperimentVariant.experiment_id == experiment_id).order_by(ModelExperimentVariant.label, ModelExperimentVariant.repeat_index)).all())


def get_experiment(db: Session, experiment_id: str) -> ModelExperiment:
    row = db.get(ModelExperiment, experiment_id)
    if row is None:
        _error("模型实验不存在", status.HTTP_404_NOT_FOUND)
    return row


def _validate_frozen_experiment(db: Session, experiment: ModelExperiment, variants: list[ModelExperimentVariant]) -> None:
    """Reject start if the authoritative source behind a frozen snapshot changed.

    The worker later consumes only the frozen rows.  This check protects the
    much earlier preflight-to-start boundary: an edited Draft Profile, changed
    Prompt version, tampered Variant parameter set, or changed input digest
    cannot receive a fresh supplier-call authorization.
    """

    if not variants or len(variants) > MAX_VARIANTS * MAX_REPEAT:
        _error("实验 Variant 配置无效，无法开始", status.HTTP_409_CONFLICT)
    if experiment.input_hash != sha256(_stable_json(experiment.sanitized_input_snapshot).encode("utf-8")).hexdigest():
        _error("实验冻结输入已变化，请重新创建实验", status.HTTP_409_CONFLICT)
    _revalidate_frozen_input_assets(db, experiment)
    slot = _get_slot(db, experiment.model_slot_key, _capability(experiment.capability))
    for variant in variants:
        if variant.input_snapshot != experiment.sanitized_input_snapshot:
            _error("Variant 冻结输入与实验不一致，请重新预检", status.HTTP_409_CONFLICT)
        if sha256(_stable_json(variant.input_snapshot).encode("utf-8")).hexdigest() != experiment.input_hash:
            _error("输入资产哈希已变化，请重新创建实验", status.HTTP_409_CONFLICT)
        profile = db.get(ModelProfile, variant.model_profile_id)
        if profile is None or profile.version != variant.model_profile_version:
            _error("候选 ModelProfile 版本已变化，请重新创建实验", status.HTTP_409_CONFLICT)
        expected_profile = _profile_snapshot(profile, slot, variant.effective_parameters or {})
        if _stable_json(expected_profile) != _stable_json(variant.model_profile_snapshot or {}):
            _error("候选 ModelProfile 配置已变化，请重新创建实验", status.HTTP_409_CONFLICT)
        prompt_version = db.get(PromptTemplateVersion, variant.prompt_template_version_id)
        prompt_definition = db.get(PromptTemplateDefinition, prompt_version.prompt_template_id) if prompt_version else None
        frozen_prompt = variant.prompt_snapshot or {}
        if (
            prompt_version is None
            or prompt_definition is None
            or prompt_version.status != PromptTemplateVersionStatus.PUBLISHED
            or prompt_version.content_hash != frozen_prompt.get("content_hash")
            or prompt_version.version != frozen_prompt.get("prompt_version")
            or prompt_definition.prompt_key != frozen_prompt.get("prompt_key")
            or prompt_definition.operation_key != experiment.operation_key
            or prompt_definition.model_slot_key != experiment.model_slot_key
            or prompt_definition.capability != _capability(experiment.capability)
        ):
            _error("候选 Prompt 版本已变化或不再发布，请重新创建实验", status.HTTP_409_CONFLICT)


def _refresh_preflight(db: Session, experiment: ModelExperiment) -> dict[str, Any]:
    variants = _experiment_variants(db, experiment.id)
    _validate_frozen_experiment(db, experiment, variants)
    checked_at = _now()
    result = _preflight_payload(
        experiment_id=experiment.id,
        capability=_capability(experiment.capability),
        comparison_mode=_capability(experiment.comparison_mode),
        variants=variants,
        input_hash=experiment.input_hash,
        max_create_calls=experiment.max_create_calls,
        checked_at=checked_at,
    )
    experiment.preflight_hash = result["preflight_hash"]
    experiment.preflight_variant_hash = result["variant_config_hash"]
    experiment.preflight_expected_create_calls = result["expected_create_call_count"]
    experiment.preflight_checked_at = checked_at
    db.flush()
    return result


def preflight_existing_experiment(db: Session, experiment_id: str) -> dict[str, Any]:
    """Issue the start authorization for an existing immutable experiment.

    It is intentionally an explicit step.  It writes only preflight metadata,
    never workflows, steps, invocations, queues, results, profiles or prompts.
    """

    experiment = get_experiment(db, experiment_id)
    if experiment.status not in {ModelExperimentStatus.READY, ModelExperimentStatus.PAUSED}:
        _error("当前实验不能重新预检", status.HTTP_409_CONFLICT)
    result = _refresh_preflight(db, experiment)
    db.commit()
    return result


def list_experiments(db: Session, project_id: str | None = None) -> list[ModelExperiment]:
    statement = select(ModelExperiment).order_by(ModelExperiment.created_at.desc())
    if project_id:
        statement = statement.where(ModelExperiment.project_id == project_id)
    return list(db.scalars(statement).all())


def start_experiment(
    db: Session, *, experiment_id: str, confirmed_create_calls: int, preflight_hash: str
) -> ModelExperiment:
    # 同一实验只允许一个父 WorkflowRun。锁住实验行后再创建子步骤，避免双击或
    # 并发请求各自为同一 Variant 建立一套 Invocation。
    experiment = db.scalar(
        select(ModelExperiment).where(ModelExperiment.id == experiment_id).with_for_update()
    )
    if experiment is None:
        _error("模型实验不存在", status.HTTP_404_NOT_FOUND)
    if experiment.status not in {ModelExperimentStatus.READY, ModelExperimentStatus.PAUSED}:
        _error("当前实验不能开始", status.HTTP_409_CONFLICT)
    variants = _experiment_variants(db, experiment.id)
    _validate_frozen_experiment(db, experiment, variants)
    expected = len(variants)
    current_variant_hash = _variant_config_hash(variants)
    current_hash = _preflight_hash(
        experiment_id=experiment.id,
        input_hash=experiment.input_hash,
        variant_config_hash=current_variant_hash,
        expected_create_calls=expected,
        max_create_calls=experiment.max_create_calls,
    )
    if (
        not experiment.preflight_hash
        or not experiment.preflight_variant_hash
        or not experiment.preflight_expected_create_calls
        or preflight_hash != experiment.preflight_hash
        or current_variant_hash != experiment.preflight_variant_hash
        or current_hash != experiment.preflight_hash
    ):
        _error("预检结果已过期，请重新预检并确认调用数量", status.HTTP_409_CONFLICT)
    if confirmed_create_calls != expected or confirmed_create_calls != experiment.preflight_expected_create_calls or confirmed_create_calls > experiment.max_create_calls:
        _error(f"请明确确认预计 {expected} 次供应商创建调用", status.HTTP_409_CONFLICT)
    if experiment.workflow_run_id:
        return experiment
    definition = _definition(db)
    run = WorkflowRun(
        project_id=experiment.project_id, workflow_key=MODEL_LAB_WORKFLOW_KEY,
        workflow_definition_id=definition.id, workflow_version=definition.version,
        input_snapshot={"model_lab": {"experiment_id": experiment.id, "input_hash": experiment.input_hash, "max_create_calls": experiment.max_create_calls}},
        status=RunStatus.PENDING,
    )
    db.add(run)
    db.flush()
    slot = _get_slot(db, experiment.model_slot_key, _capability(experiment.capability))
    for position, variant in enumerate(variants, start=1):
        step = WorkflowStep(
            workflow_run_id=run.id, step_key="MODEL_LAB_VARIANT", position=position, attempt=variant.repeat_index,
            status=RunStatus.PENDING, progress=0,
            idempotency_key=f"model-lab:{variant.id}",
            input_payload={"experiment_id": experiment.id, "variant_id": variant.id, "input_hash": experiment.input_hash},
            model_profile_snapshot=deepcopy(variant.model_profile_snapshot),
        )
        db.add(step)
        db.flush()
        invocation = ModelInvocation(
            project_id=experiment.project_id, workflow_run_id=run.id, workflow_step_id=step.id,
            model_slot_id=slot.id, model_profile_id=variant.model_profile_id,
            prompt_template_version_id=variant.prompt_template_version_id,
            task_type=experiment.operation_key,
            model_profile_snapshot=deepcopy(variant.model_profile_snapshot), prompt_snapshot=deepcopy(variant.prompt_snapshot),
            input_snapshot={"model_lab": {"experiment_id": experiment.id, "variant_id": variant.id, "input": deepcopy(variant.input_snapshot), "effective_parameters": deepcopy(variant.effective_parameters)}},
            idempotency_key=f"model-lab-invocation:{variant.id}", status=RunStatus.PENDING,
        )
        db.add(invocation)
        db.flush()
        variant.workflow_step_id = step.id
        variant.model_invocation_id = invocation.id
    run.status = RunStatus.RUNNING
    run.started_at = _now()
    experiment.workflow_run_id = run.id
    experiment.status = ModelExperimentStatus.RUNNING
    db.commit()
    db.refresh(experiment)
    return experiment


_FAKE_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cfc0000004010100b51c0c020000000049454e44ae426082"
)
_FAKE_MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2avc1mp41"


def _fake_output(experiment: ModelExperiment, variant: ModelExperimentVariant) -> tuple[dict[str, Any], dict[str, Any]]:
    capability = _capability(experiment.capability)
    if capability == "text":
        result = {"kind": "fake_text", "structured_result": {"status": "FAKE_MODEL_LAB_OK", "variant_id": variant.id}, "character_count": 0, "mock": True}
        return result, {"mock": True, "character_count": 0}
    if capability == "image":
        media_url = local_asset_storage.save_generated_image_bytes(
            project_id=experiment.project_id, asset_kind="model-lab-fake-image", asset_id=variant.id,
            version=variant.repeat_index, content=_FAKE_PNG, content_type="image/png",
        )
        return {"kind": "fake_image", "local_media_url": media_url, "mime_type": "image/png", "width": 1, "height": 1, "byte_size": len(_FAKE_PNG), "sha256": sha256(_FAKE_PNG).hexdigest(), "reference_count": len(variant.input_snapshot.get("reference_assets", [])), "mock": True}, {"mock": True, "mime_type": "image/png"}
    media_url = local_asset_storage.save_generated_video_bytes(
        project_id=experiment.project_id, asset_kind="model-lab-fake-video", asset_id=variant.id,
        version=variant.repeat_index, content=_FAKE_MP4, content_type="video/mp4",
    )
    return {"kind": "fake_video", "local_media_url": media_url, "mime_type": "video/mp4", "byte_size": len(_FAKE_MP4), "sha256": sha256(_FAKE_MP4).hexdigest(), "provider_task_id": None, "mock": True}, {"mock": True, "mime_type": "video/mp4"}


def _finish_variant(db: Session, experiment: ModelExperiment, variant: ModelExperimentVariant) -> None:
    step = db.get(WorkflowStep, variant.workflow_step_id)
    invocation = db.get(ModelInvocation, variant.model_invocation_id)
    if step is None or invocation is None:
        raise RuntimeError("Model Lab Variant 缺少已冻结的工作流审计记录")
    if variant.status == RunStatus.SUCCEEDED:
        return
    started = perf_counter()
    variant.status = RunStatus.RUNNING
    step.status = RunStatus.RUNNING
    step.progress = 25
    step.started_at = step.started_at or _now()
    invocation.status = RunStatus.RUNNING
    profile_snapshot = variant.model_profile_snapshot.get("profile_snapshot") or {}
    try:
        if not is_mock_adapter(profile_snapshot):
            # 真实执行仍只允许经现有 Adapter；这里刻意阻止“未显式接入的试验协议”
            # 伪装成成功。下一轮在真实模型验收时会在此边界调用现有 Adapter，而不
            # 建立第二套供应商客户端。本轮没有任何真实调用入口。
            raise RuntimeError("MODEL_LAB_REAL_EXECUTION_REQUIRES_APPROVED_PROVIDER_RUN")
        output, metrics = _fake_output(experiment, variant)
        finished = _now()
        variant.status = RunStatus.SUCCEEDED
        variant.output_reference = redact_sensitive_data(output)
        variant.metrics = redact_sensitive_data(metrics)
        variant.completed_at = finished
        step.status = RunStatus.SUCCEEDED
        step.progress = 100
        step.output_payload = {"variant_id": variant.id, "output_reference": redact_sensitive_data(output), "mock": True}
        step.finished_at = finished
        invocation.status = RunStatus.SUCCEEDED
        invocation.output_reference = redact_sensitive_data(output)
        invocation.media_units = redact_sensitive_data(metrics)
        invocation.latency_ms = max(0, int((perf_counter() - started) * 1000))
        invocation.finished_at = finished
    except Exception as exc:
        failed = _now()
        summary = sanitize_error_summary(exc)
        variant.status = RunStatus.FAILED
        variant.error_code = "MODEL_LAB_EXECUTION_FAILED" if "MODEL_LAB_REAL" not in str(exc) else "MODEL_LAB_REAL_EXECUTION_REQUIRES_APPROVED_PROVIDER_RUN"
        variant.sanitized_error_summary = summary
        variant.completed_at = failed
        step.status = RunStatus.FAILED
        step.progress = 100
        step.error_message = summary
        step.finished_at = failed
        invocation.status = RunStatus.FAILED
        invocation.error_code = variant.error_code
        invocation.output_reference = {"error": summary}
        invocation.latency_ms = max(0, int((perf_counter() - started) * 1000))
        invocation.finished_at = failed
    db.flush()


def execute_model_lab_workflow(run_id: str) -> None:
    """Worker 入口；只消费创建时冻结的 Variant 快照，绝不回读活动模型或 Prompt。"""

    db: Session = SessionLocal()
    try:
        experiment = db.scalar(select(ModelExperiment).where(ModelExperiment.workflow_run_id == run_id))
        run = db.get(WorkflowRun, run_id)
        if experiment is None or run is None:
            raise RuntimeError("Model Lab 工作流不存在")
        if experiment.status == ModelExperimentStatus.PAUSED:
            return
        if experiment.status not in {ModelExperimentStatus.RUNNING, ModelExperimentStatus.READY}:
            return
        for variant in _experiment_variants(db, experiment.id):
            if variant.status == RunStatus.PENDING:
                _finish_variant(db, experiment, variant)
        variants = _experiment_variants(db, experiment.id)
        if any(item.status in {RunStatus.PENDING, RunStatus.RUNNING} for item in variants):
            run.status = RunStatus.RUNNING
        elif any(item.status == RunStatus.FAILED for item in variants):
            run.status = RunStatus.FAILED
            run.finished_at = _now()
            experiment.status = ModelExperimentStatus.FAILED
        else:
            run.status = RunStatus.SUCCEEDED
            run.finished_at = _now()
            experiment.status = ModelExperimentStatus.COMPLETED
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def pause_experiment(db: Session, experiment_id: str) -> ModelExperiment:
    row = get_experiment(db, experiment_id)
    if row.status != ModelExperimentStatus.RUNNING:
        _error("只有运行中的实验可以暂停", status.HTTP_409_CONFLICT)
    row.status = ModelExperimentStatus.PAUSED
    db.commit()
    return row


def resume_experiment(db: Session, experiment_id: str) -> ModelExperiment:
    row = get_experiment(db, experiment_id)
    if row.status != ModelExperimentStatus.PAUSED:
        _error("只有已暂停实验可以继续", status.HTTP_409_CONFLICT)
    row.status = ModelExperimentStatus.RUNNING
    db.commit()
    return row


def resume_provider_task_variant(db: Session, *, experiment_id: str, variant_id: str) -> ModelExperimentVariant:
    """把已有供应商任务恢复为仅查询/下载模式。

    该入口不接受调用方提交的任务号；只能复用当前失败 Variant 已持久化的
    ``provider_task_id``。因此恢复不会再走创建 POST，实际查询仍由既有视频/图片
    Adapter 的 poll/download 边界承担。
    """

    experiment = get_experiment(db, experiment_id)
    variant = db.get(ModelExperimentVariant, variant_id)
    if variant is None or variant.experiment_id != experiment.id:
        _error("Variant 不属于当前实验", status.HTTP_404_NOT_FOUND)
    if variant.status != RunStatus.FAILED or not variant.provider_task_id:
        _error("只有已保存供应商任务号的失败 Variant 可以恢复", status.HTTP_409_CONFLICT)
    step = db.get(WorkflowStep, variant.workflow_step_id)
    invocation = db.get(ModelInvocation, variant.model_invocation_id)
    if step is None or invocation is None:
        _error("Variant 缺少恢复所需审计步骤", status.HTTP_409_CONFLICT)
    recovery = {
        "execution_mode": "resume_provider_task",
        "recovered_provider_task_id": variant.provider_task_id,
        "provider_create_post_count": 0,
        "recovered_from_variant_id": variant.id,
    }
    variant.status = RunStatus.PENDING
    variant.error_code = None
    variant.sanitized_error_summary = None
    variant.recovered_from_variant_id = variant.id
    variant.provider_create_post_count = 0
    step.status = RunStatus.PENDING
    step.progress = 0
    step.error_message = None
    step.input_payload = {**(step.input_payload or {}), "provider_task_recovery": recovery}
    invocation.status = RunStatus.PENDING
    invocation.error_code = None
    invocation.output_reference = None
    invocation.input_snapshot = {
        **(invocation.input_snapshot or {}),
        "provider_task_recovery": recovery,
    }
    experiment.status = ModelExperimentStatus.RUNNING
    db.commit()
    return variant


def upsert_evaluation(
    db: Session, *, experiment_id: str, variant_id: str, scores: Mapping[str, Any], notes: str, is_winner: bool
) -> ModelExperimentEvaluation:
    experiment = get_experiment(db, experiment_id)
    variant = db.get(ModelExperimentVariant, variant_id)
    if variant is None or variant.experiment_id != experiment.id:
        _error("Variant 不属于当前实验", status.HTTP_404_NOT_FOUND)
    normalized_scores = _safe_mapping(scores, path="评分")
    expected = _SCORE_DIMENSIONS[_capability(experiment.capability)]
    if set(normalized_scores) != expected:
        _error("评分维度与实验能力不匹配")
    if any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5 for value in normalized_scores.values()):
        _error("每项评分必须是 1 至 5 的整数")
    if is_winner and variant.status != RunStatus.SUCCEEDED:
        _error("只有成功 Variant 可以设为优胜", status.HTTP_409_CONFLICT)
    evaluation = db.scalar(select(ModelExperimentEvaluation).where(ModelExperimentEvaluation.experiment_id == experiment.id, ModelExperimentEvaluation.variant_id == variant.id))
    if evaluation is None:
        evaluation = ModelExperimentEvaluation(experiment_id=experiment.id, variant_id=variant.id)
        db.add(evaluation)
    if is_winner:
        for other in db.scalars(select(ModelExperimentEvaluation).where(ModelExperimentEvaluation.experiment_id == experiment.id, ModelExperimentEvaluation.variant_id != variant.id, ModelExperimentEvaluation.is_winner.is_(True))).all():
            other.is_winner = False
        experiment.winner_variant_id = variant.id
    elif experiment.winner_variant_id == variant.id:
        experiment.winner_variant_id = None
    evaluation.scores = {key: int(value) for key, value in normalized_scores.items()}
    evaluation.notes = str(notes or "").strip()[:2000]
    evaluation.is_winner = is_winner
    db.commit()
    db.refresh(evaluation)
    return evaluation


def promote_winner_to_production(
    db: Session, *, experiment_id: str, variant_id: str, confirmed: bool, replace_profile_id: str | None
) -> ModelExperiment:
    if not confirmed:
        _error("必须明确确认后才能提升正式生产 Profile", status.HTTP_409_CONFLICT)
    experiment = get_experiment(db, experiment_id)
    if experiment.winner_variant_id != variant_id:
        _error("只有当前 Winner 可以提升生产 Profile", status.HTTP_409_CONFLICT)
    variant = db.get(ModelExperimentVariant, variant_id)
    if variant is None or variant.experiment_id != experiment.id or variant.status != RunStatus.SUCCEEDED:
        _error("优胜 Variant 无效或尚未成功", status.HTTP_409_CONFLICT)
    profile = db.get(ModelProfile, variant.model_profile_id)
    if profile is None:
        _error("优胜 Variant 的 ModelProfile 已不存在", status.HTTP_409_CONFLICT)
    if _is_non_production_profile(profile, variant.model_profile_snapshot):
        _error("Mock、Fake、测试或 Fixture Profile 不能提升到正式生产槽位", status.HTTP_409_CONFLICT)
    # 锁住同一槽位的提升事务；PostgreSQL 下可阻止两个 Winner 同时替换同一正式绑定。
    # SQLite 会在事务提交时串行写入，仍保持单次服务调用的原子提交。
    locked_slot = db.scalar(
        select(ModelSlot).where(ModelSlot.slot_key == experiment.model_slot_key).with_for_update()
    )
    if locked_slot is None:
        _error("模型槽位不存在", status.HTTP_404_NOT_FOUND)
    # bind_profile_to_slot 是已有模型中心的唯一版本化绑定入口；它会保留旧 Profile
    # 与历史调用审计。Model Lab 不接触 Profile 正文、Prompt 激活或工作流预设。
    binding = bind_profile_to_slot(
        db, slot_key=experiment.model_slot_key, model_profile_id=variant.model_profile_id,
        enabled=True, priority=100, weight=None, replace_existing=True, replace_profile_id=replace_profile_id,
        commit=False,
    )
    experiment.promotion_metadata = {
        "experiment_id": experiment.id, "variant_id": variant.id, "old_profile_id": replace_profile_id,
        "new_profile_id": variant.model_profile_id, "slot_key": experiment.model_slot_key,
        "binding_id": binding.id, "promoted_at": _now().isoformat(),
    }
    db.commit()
    db.refresh(experiment)
    return experiment


def _is_non_production_profile(profile: ModelProfile, snapshot: Mapping[str, Any] | None = None) -> bool:
    """Block test-only profiles at the service boundary, not merely in the UI."""

    profile_snapshot = (snapshot or {}).get("profile_snapshot") if isinstance(snapshot, Mapping) else {}
    adapter = str(
        (profile_snapshot or {}).get("adapter_key")
        or profile.adapter_key
        or profile.provider_key
        or ""
    ).casefold()
    if adapter == "mock_v1" or any(marker in adapter for marker in ("mock", "fake", "fixture", "test")):
        return True
    identity = " ".join(
        str(value or "")
        for value in (profile.provider_key, profile.model_key, profile.display_name)
    ).casefold()
    return bool(re.search(r"(?:^|[\s_\-./])(mock|fake|fixture|test)(?:$|[\s_\-./])", identity))


def response_payload(db: Session, experiment: ModelExperiment) -> dict[str, Any]:
    """为 API/UI 生成安全展示数据，不回传渠道 URL、Adapter 或密钥名称。"""

    variants = _experiment_variants(db, experiment.id)
    evaluations = {item.variant_id: item for item in db.scalars(select(ModelExperimentEvaluation).where(ModelExperimentEvaluation.experiment_id == experiment.id)).all()}
    slot = db.scalar(select(ModelSlot).where(ModelSlot.slot_key == experiment.model_slot_key))
    production_profiles = []
    if slot is not None:
        production_profiles = [
            {
                "id": profile.id,
                "name": profile.display_name or profile.model_key,
                "version": profile.version,
            }
            for profile in db.scalars(
                select(ModelProfile)
                .join(ModelSlotProfileBinding, ModelSlotProfileBinding.model_profile_id == ModelProfile.id)
                .where(
                    ModelSlotProfileBinding.slot_id == slot.id,
                    ModelSlotProfileBinding.is_enabled.is_(True),
                )
                .order_by(ModelSlotProfileBinding.priority, ModelSlotProfileBinding.created_at)
            ).all()
        ]
    preflight = None
    if experiment.preflight_hash and experiment.preflight_variant_hash and experiment.preflight_expected_create_calls:
        preflight = _preflight_payload(
            experiment_id=experiment.id,
            capability=_capability(experiment.capability),
            comparison_mode=_capability(experiment.comparison_mode),
            variants=variants,
            input_hash=experiment.input_hash,
            max_create_calls=experiment.max_create_calls,
            checked_at=experiment.preflight_checked_at,
        )
        # API responses report the authorization that was actually persisted;
        # they never mint a new one merely by being read.
        preflight["preflight_hash"] = experiment.preflight_hash
        preflight["variant_config_hash"] = experiment.preflight_variant_hash
        preflight["expected_create_call_count"] = experiment.preflight_expected_create_calls
        preflight["estimated_create_calls"] = experiment.preflight_expected_create_calls
    return {
        "id": experiment.id, "project_id": experiment.project_id, "name": experiment.name, "description": experiment.description,
        "operation_key": experiment.operation_key, "model_slot_key": experiment.model_slot_key,
        "capability": _capability(experiment.capability), "comparison_mode": _capability(experiment.comparison_mode),
        "input_source_type": experiment.input_source_type, "input_hash": experiment.input_hash, "max_create_calls": experiment.max_create_calls,
        "preflight": preflight,
        "status": _capability(experiment.status), "workflow_run_id": experiment.workflow_run_id, "winner_variant_id": experiment.winner_variant_id,
        "created_at": experiment.created_at, "updated_at": experiment.updated_at, "archived_at": experiment.archived_at,
        "slot_selection_mode": _capability(slot.selection_mode) if slot is not None else None,
        "production_profiles": production_profiles,
        "variants": [
            {
                "id": item.id, "label": item.label, "profile_id": item.model_profile_id, "profile_version": item.model_profile_version,
                "prompt_template_version_id": item.prompt_template_version_id, "prompt_version": (item.prompt_snapshot or {}).get("prompt_version"),
                "prompt_hash": str((item.prompt_snapshot or {}).get("content_hash") or "")[:12],
                "parameter_preset": item.parameter_preset, "effective_parameters": (item.effective_parameters or {}).get("effective_parameters", {}),
                "omitted_parameters": (item.effective_parameters or {}).get("omitted_parameters", []), "repeat_index": item.repeat_index,
                "status": _status(item.status), "workflow_step_id": item.workflow_step_id, "model_invocation_id": item.model_invocation_id,
                "provider_task_id_short": (item.provider_task_id[:12] + "…" + item.provider_task_id[-5:]) if item.provider_task_id and len(item.provider_task_id) > 20 else item.provider_task_id,
                "output_reference": redact_sensitive_data(item.output_reference), "metrics": redact_sensitive_data(item.metrics),
                "error_code": item.error_code, "sanitized_error_summary": sanitize_error_summary(item.sanitized_error_summary or "") or None,
                "provider_create_post_count": item.provider_create_post_count, "is_mock": (item.model_profile_snapshot.get("profile_snapshot") or {}).get("adapter_key") == "mock_v1",
                "evaluation": None if item.id not in evaluations else {"scores": evaluations[item.id].scores, "notes": evaluations[item.id].notes, "is_winner": evaluations[item.id].is_winner},
            }
            for item in variants
        ],
    }
