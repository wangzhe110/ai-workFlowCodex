"""Commerce StoryRun 的 Slice 2 导演、视觉、视频与成片执行服务。

这里不是第二条业务工作流。十创意和大纲仍由既有 ``commerce_story_run`` 父运行
推进；本模块只把该 StoryRun 已冻结的输入继续生产成角色、场景、导演分镜、图片、
视频片段和成片。每个耗时动作复用 ``WorkflowRun`` / ``WorkflowStep`` 以及
``ModelInvocation``，并把输入、模型、Prompt 固化到本次任务，Worker 不会重新读取
当前商品、当前 Prompt 或当前启用模型。

真实 Adapter 和 Mock 的唯一区别在 Adapter 边界。Mock 地址一律是 ``mock://``，
从不伪装为真实图片、MP4 或 FFmpeg 成片。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from subprocess import CalledProcessError, TimeoutExpired, run
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any, Iterable
from urllib.request import Request, urlopen

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    CommerceCharacterDesignVersion,
    CommerceCharacterReferenceImage,
    CommerceFinalVideo,
    CommerceSceneDesignVersion,
    CommerceSceneReferenceImage,
    CommerceShotKeyframeVersion,
    CommerceStoryboardVersion,
    CommerceVideoClipVersion,
    CommerceVideoPromptVersion,
    ModelInvocation,
    ModelProfile,
    ModelSlot,
    ProductAssetVersion,
    PromptTemplate,
    PromptTemplateStatus,
    ReviewDecision,
    RunStatus,
    StoryOutlineVersion,
    StoryRun,
    WorkflowRun,
    WorkflowStep,
)
from app.services.commerce_configuration_service import ensure_commerce_foundation
from app.services.final_video_service import _compose_real_video
from app.services.storage import LocalImageReference, local_asset_storage
from app.services.v1_configuration_service import enabled_profiles_for_slot
from app.services.v1_model_adapter_service import (
    adapter_key,
    assert_supported,
    create_video_request,
    generate_structured_text,
    is_mock_adapter,
    persist_v1_image,
    persist_v1_image_bytes,
    start_image_generation,
    video_provider,
    wait_for_image_result,
    wait_for_video_result,
)
from app.services.provider_config_security import redact_provider_config
from app.services.sensitive_data import sanitize_error_summary


WORKFLOW_PREFIX = "commerce_production_"

# key -> (ModelSlot, Prompt task type). FINAL_COMPOSE 不调用模型。
OPERATION_SPECS: dict[str, tuple[str | None, str | None]] = {
    "CHARACTER_DESIGN": ("CHARACTER_DESIGN", "CHARACTER_DESIGN"),
    "SCENE_DESIGN": ("SCENE_DESIGN", "SCENE_DESIGN"),
    "STORYBOARD": ("DIRECTOR_PLAN", "DIRECTOR_PLAN"),
    "CHARACTER_IMAGES": ("CHARACTER_IMAGE_GENERATE", "IMAGE_GENERATE"),
    "SCENE_IMAGES": ("SCENE_IMAGE_GENERATE", "IMAGE_GENERATE"),
    "SHOT_KEYFRAME": ("SHOT_KEYFRAME_GENERATE", "IMAGE_GENERATE"),
    # 视频 Prompt 是导演输出的结构化延续，使用同一能力槽位而不是把视频模型用于
    # 一段纯文本，避免不必要的付费视频调用。
    "VIDEO_PROMPT": ("DIRECTOR_PLAN", "DIRECTOR_PLAN"),
    "VIDEO_RENDER": ("VIDEO_GENERATE", "VIDEO_GENERATE"),
    "FINAL_COMPOSE": (None, None),
}

TERMINAL = {"LOCKED", "SUPERSEDED", "STALE", "REJECTED", "APPROVED", "FAILED"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _error(detail: str, code: int = status.HTTP_409_CONFLICT) -> None:
    raise HTTPException(status_code=code, detail=detail)


def _story_run(db: Session, story_run_id: str) -> StoryRun:
    row = db.get(StoryRun, story_run_id)
    if row is None:
        _error("StoryRun 不存在", status.HTTP_404_NOT_FOUND)
    if row.mainline_input is None:
        _error("该 StoryRun 不包含 Slice 1 冻结输入，不能进入带货导演生产", status.HTTP_409_CONFLICT)
    return row


def _enum(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _frozen_mainline(story_run: StoryRun) -> dict[str, Any]:
    snapshot = deepcopy(story_run.mainline_input.input_snapshot if story_run.mainline_input else {})
    required = ("reference_analysis", "script_analysis", "product_asset_version", "creative_idea")
    if not all(isinstance(snapshot.get(key), dict) for key in required):
        _error("StoryRun 的 Slice 1 冻结输入不完整，不能执行导演生产", status.HTTP_422_UNPROCESSABLE_CONTENT)
    return snapshot


def _current_locked_outline(db: Session, story_run: StoryRun) -> StoryOutlineVersion:
    row = db.scalars(
        select(StoryOutlineVersion)
        .where(StoryOutlineVersion.story_run_id == story_run.id, StoryOutlineVersion.status == "LOCKED")
        .order_by(StoryOutlineVersion.version.desc())
    ).first()
    if row is None:
        _error("请先确认并锁定故事大纲与商品融入方案")
    return row


def _current_locked_character(db: Session, story_run_id: str) -> CommerceCharacterDesignVersion:
    row = db.scalars(
        select(CommerceCharacterDesignVersion)
        .where(CommerceCharacterDesignVersion.story_run_id == story_run_id, CommerceCharacterDesignVersion.status == "LOCKED")
        .order_by(CommerceCharacterDesignVersion.version.desc())
    ).first()
    if row is None:
        _error("请先生成并锁定角色设定")
    return row


def _current_locked_scene(db: Session, story_run_id: str) -> CommerceSceneDesignVersion:
    row = db.scalars(
        select(CommerceSceneDesignVersion)
        .where(CommerceSceneDesignVersion.story_run_id == story_run_id, CommerceSceneDesignVersion.status == "LOCKED")
        .order_by(CommerceSceneDesignVersion.version.desc())
    ).first()
    if row is None:
        _error("请先生成并锁定场景设定")
    return row


def _current_storyboard(db: Session, story_run_id: str, *, require_locked: bool = True) -> CommerceStoryboardVersion:
    statement = select(CommerceStoryboardVersion).where(CommerceStoryboardVersion.story_run_id == story_run_id)
    if require_locked:
        statement = statement.where(CommerceStoryboardVersion.status == "LOCKED")
    row = db.scalars(statement.order_by(CommerceStoryboardVersion.version.desc())).first()
    if row is None:
        _error("请先生成并确认 AI 导演分镜")
    return row


def _latest_locked_image(
    db: Session, model, *, design_column: str, design_id: str, logical_id_column: str, logical_id: str
):
    return db.scalars(
        select(model)
        .where(
            getattr(model, design_column) == design_id,
            getattr(model, logical_id_column) == logical_id,
            model.status == "LOCKED",
        )
        .order_by(model.version.desc())
    ).first()


def _product_reference_urls(mainline: dict[str, Any]) -> list[str]:
    product = mainline.get("product_asset_version") or {}
    rows = product.get("reference_images") if isinstance(product, dict) else []
    urls: list[str] = []
    if isinstance(rows, list):
        for row in rows:
            candidate = row.get("url") if isinstance(row, dict) else row
            if isinstance(candidate, str) and candidate:
                urls.append(candidate)
    return list(dict.fromkeys(urls))


def _integration_nodes(outline: StoryOutlineVersion, mainline: dict[str, Any]) -> list[dict[str, Any]]:
    """规范化商品融入方案，以稳定 node ID 连接镜头、卖点和分析证据。"""

    strategy = deepcopy(outline.product_placement_strategy or {})
    candidates = strategy.get("nodes") if isinstance(strategy, dict) else None
    product = mainline.get("product_asset_version") or {}
    selling_points = product.get("selling_points") if isinstance(product, dict) else []
    valid_points = selling_points if isinstance(selling_points, list) else []
    nodes: list[dict[str, Any]] = []
    if isinstance(candidates, list):
        for index, row in enumerate(candidates, start=1):
            if not isinstance(row, dict):
                continue
            node_id = row.get("id") or f"outline-{outline.id}-placement-{index}"
            if not isinstance(node_id, str) or not node_id.strip():
                continue
            evidence_indexes = row.get("selling_point_indexes")
            if not isinstance(evidence_indexes, list):
                evidence_indexes = list(range(min(1, len(valid_points))))
            evidence_indexes = [item for item in evidence_indexes if isinstance(item, int) and 0 <= item < len(valid_points)]
            nodes.append({
                "id": node_id.strip(),
                "method": str(row.get("method") or strategy.get("method") or "SOFT_PROP"),
                "story_moment": str(row.get("story_moment") or "剧情转折中的自然体验"),
                "selling_point_indexes": evidence_indexes,
                "evidence": [deepcopy(valid_points[index]) for index in evidence_indexes],
            })
    if not nodes:
        indexes = list(range(min(1, len(valid_points))))
        nodes.append({
            "id": f"outline-{outline.id}-placement-1",
            "method": str(strategy.get("method") or "SOFT_PROP"),
            "story_moment": "在真实痛点被解决的剧情节点自然出现",
            "selling_point_indexes": indexes,
            "evidence": [deepcopy(valid_points[index]) for index in indexes],
        })
    return nodes


def _profile_snapshot(db: Session, binding, slot_key: str) -> dict[str, Any]:
    profile = db.get(ModelProfile, binding.model_profile_id)
    slot = db.get(ModelSlot, binding.slot_id)
    if profile is None or slot is None:
        _error("模型中心存在无效绑定", status.HTTP_503_SERVICE_UNAVAILABLE)
    return {
        "slot_id": slot.id,
        "slot_key": slot_key,
        "model_profile_id": profile.id,
        "profile_snapshot": {
            "profile_id": profile.id,
            "adapter_key": profile.adapter_key or profile.provider_key,
            "provider_key": profile.provider_key,
            "model_key": profile.model_key,
            "model_version": profile.model_version or profile.model_key,
            "version": profile.version,
            "provider_config": redact_provider_config(profile.provider_config),
        },
    }


def _freeze_model_and_prompt(db: Session, operation: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    slot_key, task_type = OPERATION_SPECS[operation]
    if slot_key is None:
        return None, None
    bindings = enabled_profiles_for_slot(db, slot_key)
    if not bindings:
        _error(f"模型槽位 {slot_key} 未启用模型配置", status.HTTP_503_SERVICE_UNAVAILABLE)
    binding = _profile_snapshot(db, bindings[0], slot_key)
    prompt = db.scalars(
        select(PromptTemplate)
        .where(PromptTemplate.task_type == task_type, PromptTemplate.status == PromptTemplateStatus.ACTIVE)
        .order_by(PromptTemplate.version.desc())
    ).first()
    if prompt is None:
        _error(f"任务 {task_type} 未配置活动 Prompt", status.HTTP_503_SERVICE_UNAVAILABLE)
    return binding, {
        "id": prompt.id,
        "task_type": prompt.task_type,
        "name": prompt.name,
        "version": prompt.version,
        "content": prompt.content,
        "variables_schema": deepcopy(prompt.variables_schema),
    }


def _operation_prerequisites(db: Session, story_run: StoryRun, operation: str, target_id: str | None) -> dict[str, Any]:
    mainline = _frozen_mainline(story_run)
    product_id = (mainline.get("product_asset_version") or {}).get("id")
    if product_id != story_run.product_asset_version_id:
        _error("StoryRun 冻结商品版本与上游快照不一致")
    product = db.get(ProductAssetVersion, story_run.product_asset_version_id)
    if product is None or _enum(product.status) != "CONFIRMED" or product.frozen_at is None:
        _error("冻结商品版本已失效或未确认")
    outline = _current_locked_outline(db, story_run)
    context: dict[str, Any] = {
        "story_run_id": story_run.id,
        "project_id": story_run.project_id,
        "operation": operation,
        "commerce_mainline": mainline,
        "outline": {
            "id": outline.id,
            "version": outline.version,
            "title": outline.title,
            "premise": outline.premise,
            "story_beats": deepcopy(outline.story_beats),
            "product_placement_strategy": deepcopy(outline.product_placement_strategy),
            "integration_nodes": _integration_nodes(outline, mainline),
        },
    }
    if operation in {"CHARACTER_DESIGN"}:
        return context
    character = _current_locked_character(db, story_run.id)
    context["character_design"] = {
        "id": character.id, "version": character.version, "content": deepcopy(character.content)
    }
    if operation in {"SCENE_DESIGN", "CHARACTER_IMAGES"}:
        return context
    scene = _current_locked_scene(db, story_run.id)
    context["scene_design"] = {"id": scene.id, "version": scene.version, "content": deepcopy(scene.content)}
    if operation in {"SCENE_IMAGES", "STORYBOARD"}:
        return context
    storyboard = _current_storyboard(db, story_run.id)
    context["storyboard"] = {"id": storyboard.id, "version": storyboard.version, "content": deepcopy(storyboard.content)}
    if target_id:
        context["target_id"] = target_id
    return context


def _fingerprint(context: dict[str, Any], *, retry: bool) -> str:
    raw = repr(context).encode("utf-8")
    value = sha256(raw).hexdigest()
    return f"{value}:{utcnow().isoformat()}" if retry else value


def _active_run_for_context(db: Session, *, project_id: str, workflow_key: str, context: dict[str, Any]) -> WorkflowRun | None:
    """在创建前按冻结上下文查找已有活动任务。

    0012 的 Commerce sidecar 只约束 ``commerce_story_run`` 父运行；Slice 2 是同一
    StoryRun 的独立媒体子任务，不能错误套用其“单阶段”约束。0018 另有按
    ``idempotency_key`` 限定的部分唯一索引；这里仅用于正常请求快速返回，而不是
    把并发正确性建立在一次非原子查询上。
    """

    candidates = db.scalars(
        select(WorkflowRun)
        .where(
            WorkflowRun.project_id == project_id,
            WorkflowRun.workflow_key == workflow_key,
            WorkflowRun.status.in_((RunStatus.PENDING, RunStatus.RUNNING)),
        )
        .order_by(WorkflowRun.created_at.desc())
    ).all()
    for candidate in candidates:
        frozen = ((candidate.input_snapshot or {}).get("commerce_production") or {})
        if frozen == context:
            return candidate
    return None


def create_production_run(
    db: Session, *, story_run_id: str, operation: str, target_id: str | None = None, retry: bool = False
) -> tuple[WorkflowRun, bool]:
    """创建一个冻结的 Slice 2 子任务，重复点击只返回同一个活动任务。"""

    operation = operation.upper()
    if operation not in OPERATION_SPECS:
        _error("未知的 Commerce 生产操作", status.HTTP_422_UNPROCESSABLE_CONTENT)
    story_run = _story_run(db, story_run_id)
    context = _operation_prerequisites(db, story_run, operation, target_id)
    binding, prompt = _freeze_model_and_prompt(db, operation)
    definition = ensure_commerce_foundation(db)
    semantic = _fingerprint(context, retry=retry)
    key = f"commerce-production:{story_run.id}:{operation}:{target_id or 'all'}:{semantic}"
    idempotency_key = f"run:{sha256(key.encode()).hexdigest()}"
    workflow_key = f"{WORKFLOW_PREFIX}{operation.lower()}"
    if not retry:
        existing = _active_run_for_context(
            db, project_id=story_run.project_id, workflow_key=workflow_key, context=context
        )
        if existing is not None:
            return existing, False
    snapshot = {
        "frozen_at": utcnow().isoformat(),
        "workflow_definition": {"id": definition.id, "workflow_code": definition.workflow_code, "version": definition.version},
        "commerce_production": context,
        "model_binding": deepcopy(binding),
        "prompt_template": deepcopy(prompt),
    }
    run_row = WorkflowRun(
        project_id=story_run.project_id,
        workflow_key=workflow_key,
        workflow_definition_id=definition.id,
        workflow_version=definition.version,
        idempotency_key=idempotency_key,
        input_snapshot=snapshot,
        status=RunStatus.PENDING,
    )
    step = WorkflowStep(
        workflow_run=run_row,
        step_key=f"COMMERCE_{operation}",
        position=1,
        attempt=1,
        input_payload=deepcopy(snapshot),
        model_profile_snapshot={"binding": deepcopy(binding), "prompt_template": deepcopy(prompt)},
        idempotency_key=f"step:{sha256((idempotency_key + ':step').encode()).hexdigest()}",
    )
    db.add_all([run_row, step])
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalars(
            select(WorkflowRun)
            .where(
                WorkflowRun.idempotency_key == idempotency_key,
                WorkflowRun.workflow_key == workflow_key,
                WorkflowRun.status.in_((RunStatus.PENDING, RunStatus.RUNNING)),
            )
            .order_by(WorkflowRun.created_at.desc())
        ).first()
        if existing is not None:
            return existing, False
        raise
    db.refresh(run_row)
    return run_row, True


def _start_invocation(
    db: Session,
    *,
    run_row: WorkflowRun,
    step: WorkflowStep,
    task_type: str,
    idempotency_suffix: str | None = None,
) -> ModelInvocation | None:
    binding = (run_row.input_snapshot or {}).get("model_binding")
    prompt = (run_row.input_snapshot or {}).get("prompt_template")
    if not isinstance(binding, dict) or not isinstance(prompt, dict):
        return None
    invocation_key = (
        step.idempotency_key
        if not idempotency_suffix
        else f"{step.idempotency_key}:{idempotency_suffix}"
    )
    # 视频供应商任务号已持久化后，Worker 可能在轮询中断。恢复时必须继续使用同一
    # 调用审计记录；重新插入会违反 ModelInvocation 的幂等唯一索引，也会掩盖一次
    # 已收费调用的真实生命周期。
    existing = db.scalar(select(ModelInvocation).where(ModelInvocation.idempotency_key == invocation_key))
    if existing is not None:
        return existing
    profile_snapshot = binding.get("profile_snapshot")
    if not isinstance(profile_snapshot, dict):
        raise RuntimeError("Commerce 生产任务冻结的模型快照无效")
    safe_profile_snapshot = deepcopy(profile_snapshot)
    safe_profile_snapshot["provider_config"] = redact_provider_config(
        profile_snapshot.get("provider_config")
    )
    invocation = ModelInvocation(
        project_id=run_row.project_id,
        workflow_run_id=run_row.id,
        workflow_step_id=step.id,
        model_slot_id=binding["slot_id"],
        model_profile_id=binding["model_profile_id"],
        prompt_template_id=prompt.get("id"),
        task_type=task_type,
        model_profile_snapshot=safe_profile_snapshot,
        prompt_snapshot=deepcopy(prompt),
        input_snapshot=deepcopy((run_row.input_snapshot or {}).get("commerce_production") or {}),
        # 一个图片批任务可能在同一 WorkflowStep 中生成多个角色或场景参考图。
        # 每个资产仍须有自己的可追溯、可去重调用键，不能共用 step 的唯一键。
        idempotency_key=invocation_key,
        status=RunStatus.RUNNING,
    )
    db.add(invocation)
    db.flush()
    return invocation


def _finish_invocation(
    invocation: ModelInvocation | None, *, output_reference: dict[str, Any], started: float, media_units: dict[str, Any] | None = None,
    provider_task_id: str | None = None,
) -> None:
    if invocation is None:
        return
    invocation.status = RunStatus.SUCCEEDED
    invocation.output_reference = deepcopy(output_reference)
    invocation.media_units = deepcopy(media_units or {})
    invocation.provider_task_id = provider_task_id
    invocation.latency_ms = max(0, int((perf_counter() - started) * 1000))
    invocation.finished_at = utcnow()


def _fail_invocation(invocation: ModelInvocation | None, message: str, *, started: float | None = None, provider_task_id: str | None = None) -> None:
    if invocation is None:
        return
    invocation.status = RunStatus.FAILED
    invocation.error_code = "COMMERCE_PRODUCTION_FAILED"
    invocation.provider_task_id = provider_task_id
    invocation.latency_ms = max(0, int((perf_counter() - started) * 1000)) if started is not None else None
    invocation.finished_at = utcnow()


def _next_version(db: Session, model, *, story_run_id: str, extra_column: str | None = None, extra_value: str | None = None) -> int:
    statement = select(func.max(model.version)).where(model.story_run_id == story_run_id)
    if extra_column and extra_value:
        statement = statement.where(getattr(model, extra_column) == extra_value)
    return int(db.scalar(statement) or 0) + 1


def _require_text(value: Any, label: str, maximum: int = 20_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label} 不能为空")
    return value.strip()[:maximum]


def _mock_roles() -> list[dict[str, Any]]:
    return [{
        "role_id": "role-1", "name": "林安", "age_range": "25-30岁", "gender": "女", "identity_and_occupation": "社区咖啡店店员",
        "personality": "细心、克制、愿意解决问题", "dramatic_function": "在生活困境中推动解决方案的人", "relationships": [],
        "appearance": "干净自然的东亚面孔", "hairstyle": "低马尾", "costume": "浅色衬衫与围裙",
        "fixed_visual_features": ["低马尾", "浅色围裙"], "immutable_features": ["发型", "服装主色"],
        "product_relationship": "在真实生活场景中体验已确认产品", "buyer": True, "user": True,
        "decision_influencer": False, "image_prompt": "电影感角色设定，低马尾，浅色围裙，正面半身，无文字"
    }]


def _validate_roles(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("roles") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("角色模型必须返回非空 roles")
    required = (
        "role_id", "name", "age_range", "gender", "identity_and_occupation", "personality", "dramatic_function",
        "appearance", "hairstyle", "costume", "product_relationship", "image_prompt",
    )
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("角色项目必须是对象")
        item = {key: _require_text(row.get(key), f"角色 {key}") for key in required}
        if item["role_id"] in seen:
            raise RuntimeError("角色 role_id 不可重复")
        seen.add(item["role_id"])
        item["relationships"] = deepcopy(row.get("relationships") if isinstance(row.get("relationships"), list) else [])
        for key in ("fixed_visual_features", "immutable_features"):
            values = row.get(key)
            item[key] = [str(value)[:240] for value in values] if isinstance(values, list) and values else []
            if not item[key]:
                raise RuntimeError(f"角色 {key} 不能为空")
        for key in ("buyer", "user", "decision_influencer"):
            item[key] = bool(row.get(key))
        normalized.append(item)
    return normalized


def _mock_scenes() -> list[dict[str, Any]]:
    return [{
        "scene_id": "scene-1", "name": "社区咖啡店后场", "purpose": "呈现主角真实痛点并自然引入商品体验", "time": "傍晚",
        "location": "社区咖啡店后场", "lighting": "暖色自然柔光", "color_tone": "浅棕与暖白", "spatial_layout": "操作台、储物架和窗口",
        "fixed_props": ["木质操作台", "玻璃窗"], "product_position": "操作台右侧", "product_usage_environment": "日常工作整理场景",
        "continuity_requirements": ["操作台位置不变", "窗外傍晚光线持续"], "immutable_features": ["木质操作台", "暖白色调"],
        "base_image_prompt": "电影感社区咖啡店后场，木质操作台，暖白柔光，无人物无文字"
    }]


def _validate_scenes(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("scenes") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("场景模型必须返回非空 scenes")
    required = (
        "scene_id", "name", "purpose", "time", "location", "lighting", "color_tone", "spatial_layout",
        "product_position", "product_usage_environment", "base_image_prompt",
    )
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("场景项目必须是对象")
        item = {key: _require_text(row.get(key), f"场景 {key}") for key in required}
        if item["scene_id"] in seen:
            raise RuntimeError("场景 scene_id 不可重复")
        seen.add(item["scene_id"])
        for key in ("fixed_props", "continuity_requirements", "immutable_features"):
            values = row.get(key)
            item[key] = [str(value)[:240] for value in values] if isinstance(values, list) and values else []
            if not item[key]:
                raise RuntimeError(f"场景 {key} 不能为空")
        normalized.append(item)
    return normalized


def _mock_shots(roles: list[dict[str, Any]], scenes: list[dict[str, Any]], nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """测试模式也遵守首支样片 20–40 秒、3–5 镜头的生产约束。"""

    role_id, scene_id = roles[0]["role_id"], scenes[0]["scene_id"]
    node = nodes[0]
    common = {
        "character_ids": [role_id],
        "scene_id": scene_id,
        "product_integration_node_id": node["id"],
    }
    return [
        {
            **common,
            "shot_id": "shot-1", "shot_number": 1, "segment_summary": "主角遭遇真实工作困扰，迅速建立行动目标。",
            "story_paragraph": "开头钩子", "duration_ms": 6000, "product_visible": False,
            "shot_scale": "中近景", "camera_position": "平视", "camera_move": "缓慢推进", "composition": "人物居中、背景保留工作台",
            "action": "主角整理物品时停下", "expression": "轻微焦虑", "dialogue": "今天又来不及了。", "narration": "",
            "product_position": "操作台右侧背景", "product_action": "暂不使用", "product_exposure_ms": 0,
            "previous_continuity_state": "傍晚工作开始", "ending_continuity_state": "主角注意到解决工具", "next_transition_requirement": "保持角色服装和操作台位置",
            "keyframe_prompt": "中近景，主角在暖白咖啡店后场整理物品，电影感", "video_prompt": "主角停下动作看向操作台，缓慢推镜，保持低马尾和暖白场景",
            "forbidden_content": ["文字水印", "未确认商品功效"],
        },
        {
            **common,
            "shot_id": "shot-2", "shot_number": 2, "segment_summary": "主角按已确认方式自然体验商品，问题开始缓解。",
            "story_paragraph": "商品融入", "duration_ms": 7000, "product_visible": True,
            "shot_scale": "特写转中景", "camera_position": "俯视后平视", "camera_move": "轻微横移", "composition": "产品与手部同框",
            "action": "按确认用法取用商品并继续整理", "expression": "专注后放松", "dialogue": "这样顺手多了。", "narration": "",
            "product_position": "操作台右侧前景", "product_action": "按冻结商品说明的日常使用方式体验", "product_exposure_ms": 2500,
            "previous_continuity_state": "主角注意到解决工具", "ending_continuity_state": "整理效率提升", "next_transition_requirement": "产品包装和操作台位置保持一致",
            "keyframe_prompt": "手部和确认商品同框，暖白柔光，电影感，无文字", "video_prompt": "角色按确认方式自然使用商品，手部细节后切中景，保持包装和角色一致",
            "forbidden_content": ["夸大功效", "变更包装", "文字水印"],
        },
        {
            **common,
            "shot_id": "shot-3", "shot_number": 3, "segment_summary": "主角回到人物关系和工作节奏，完成轻量情绪收束。",
            "story_paragraph": "结尾", "duration_ms": 7000, "product_visible": True,
            "shot_scale": "中景", "camera_position": "平视", "camera_move": "轻微拉远", "composition": "人物与整洁工作台同框",
            "action": "主角完成整理并对同事微笑", "expression": "释然", "dialogue": "终于能从容一点。", "narration": "",
            "product_position": "操作台右侧", "product_action": "自然静置", "product_exposure_ms": 1200,
            "previous_continuity_state": "整理效率提升", "ending_continuity_state": "日常恢复秩序", "next_transition_requirement": "无",
            "keyframe_prompt": "中景，整洁咖啡店后场，主角微笑，产品自然在操作台侧边，无文字", "video_prompt": "主角完成整理后微笑，镜头轻微拉远，保持同一场景与角色",
            "forbidden_content": ["医疗承诺", "文字水印"],
        },
    ]


def _validate_shots(payload: Any, *, role_ids: set[str], scene_ids: set[str], nodes: list[dict[str, Any]], product: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("shots") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not 3 <= len(rows) <= 5:
        raise RuntimeError("导演分镜必须返回 3 到 5 个镜头")
    node_ids = {item["id"] for item in nodes}
    evidence = product.get("selling_points") if isinstance(product.get("selling_points"), list) else []
    forbidden_claims = [str(item.get("claim") or item) for item in evidence if isinstance(item, dict) or isinstance(item, str)]
    required = (
        "shot_id", "segment_summary", "story_paragraph", "scene_id", "shot_scale", "camera_position", "camera_move", "composition",
        "action", "expression", "dialogue", "product_position", "product_action", "previous_continuity_state",
        "ending_continuity_state", "next_transition_requirement", "keyframe_prompt", "video_prompt",
    )
    normalized: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError("导演镜头必须是对象")
        item = {key: _require_text(row.get(key), f"镜头 {index} 的 {key}") for key in required}
        # 无旁白是正常创作选择；保留字段但不强迫模型为了通过校验编造旁白。
        narration = row.get("narration")
        item["narration"] = narration.strip()[:20_000] if isinstance(narration, str) else ""
        item["shot_number"] = int(row.get("shot_number") or index)
        item["duration_ms"] = int(row.get("duration_ms") or 0)
        if item["shot_number"] != index or not 4000 <= item["duration_ms"] <= 8000:
            raise RuntimeError("镜头编号必须连续，单镜时长必须为 4 到 8 秒")
        if item["shot_id"] in ids:
            raise RuntimeError("shot_id 不可重复")
        ids.add(item["shot_id"])
        characters = row.get("character_ids")
        if not isinstance(characters, list) or not characters or any(value not in role_ids for value in characters):
            raise RuntimeError("镜头必须引用当前锁定角色版本中的角色")
        item["character_ids"] = list(characters)
        if item["scene_id"] not in scene_ids:
            raise RuntimeError("镜头必须引用当前锁定场景版本中的场景")
        node_id = row.get("product_integration_node_id")
        if not isinstance(node_id, str) or node_id not in node_ids:
            raise RuntimeError("商品镜头必须引用冻结商品融入方案节点")
        item["product_integration_node_id"] = node_id
        item["product_visible"] = bool(row.get("product_visible"))
        exposure = int(row.get("product_exposure_ms") or 0)
        if exposure < 0 or exposure > item["duration_ms"]:
            raise RuntimeError("商品露出时长必须位于镜头时长内")
        item["product_exposure_ms"] = exposure
        forbidden = row.get("forbidden_content")
        item["forbidden_content"] = [str(value)[:240] for value in forbidden] if isinstance(forbidden, list) else []
        if not item["forbidden_content"]:
            raise RuntimeError("镜头必须声明禁止生成内容")
        # 当模型把未在商品事实中出现的“疗效/保证”写进商品动作时，阻止进入审核。
        forbidden_words = ("治愈", "保证", "立刻见效", "医疗", "药效")
        if item["product_visible"] and any(word in item["product_action"] for word in forbidden_words):
            raise RuntimeError("分镜包含未确认的商品功效或宣传结论")
        item["product_evidence"] = [{"index": i, "value": deepcopy(value)} for i, value in enumerate(evidence)]
        item["approved_claims"] = forbidden_claims
        normalized.append(item)
    if sum(item["duration_ms"] for item in normalized) < 20_000 or sum(item["duration_ms"] for item in normalized) > 40_000:
        raise RuntimeError("首个样片总时长必须位于 20 到 40 秒")
    return normalized


def _mark_stale(db: Session, story_run_id: str, *, source: str, shot_id: str | None = None) -> None:
    """只标记下游可替代版本，内容和旧文件永久保留。"""

    now = utcnow()
    models = {
        "CHARACTER": (CommerceSceneDesignVersion, CommerceStoryboardVersion, CommerceSceneReferenceImage, CommerceShotKeyframeVersion, CommerceVideoPromptVersion, CommerceVideoClipVersion, CommerceFinalVideo),
        "SCENE": (CommerceStoryboardVersion, CommerceSceneReferenceImage, CommerceShotKeyframeVersion, CommerceVideoPromptVersion, CommerceVideoClipVersion, CommerceFinalVideo),
        "STORYBOARD": (CommerceShotKeyframeVersion, CommerceVideoPromptVersion, CommerceVideoClipVersion, CommerceFinalVideo),
        "KEYFRAME": (CommerceVideoPromptVersion, CommerceVideoClipVersion, CommerceFinalVideo),
        "VIDEO": (CommerceFinalVideo,),
    }
    for model in models.get(source, ()):
        values: dict[str, Any] = {"status": "STALE"}
        if hasattr(model, "stale_at"):
            values["stale_at"] = now
        statement = update(model).where(
            model.story_run_id == story_run_id,
            model.status.in_(("READY", "LOCKED", "DRAFT", "APPROVED", "SUCCEEDED")),
        )
        # 单镜头重生关键帧只会让该镜头的 Prompt / 视频失效；其他已经审核通过的
        # 镜头仍可保留并参与之后的新成片。成片本身没有 shot_id，因此仍需整体失效。
        if source == "KEYFRAME" and shot_id and hasattr(model, "shot_id"):
            statement = statement.where(model.shot_id == shot_id)
        db.execute(statement.values(**values))


def _run_text_model(run_row: WorkflowRun, *, operation: str, system_suffix: str, output_contract: str, user_payload: dict[str, Any]) -> dict[str, Any]:
    binding = (run_row.input_snapshot or {}).get("model_binding") or {}
    prompt = (run_row.input_snapshot or {}).get("prompt_template") or {}
    profile = binding.get("profile_snapshot") if isinstance(binding, dict) else None
    if not isinstance(profile, dict) or not isinstance(prompt, dict):
        raise RuntimeError("冻结模型或 Prompt 快照无效")
    if is_mock_adapter(profile):
        return {"_mock": True}
    task_type = OPERATION_SPECS[operation][1]
    assert task_type is not None
    assert_supported(profile, task_type)
    return generate_structured_text(
        profile,
        task_type=task_type,
        system_instruction=f"{str(prompt.get('content') or '').strip()}\n\n{system_suffix}",
        user_payload=user_payload,
        output_contract=output_contract,
    )


def _execute_character_design(db: Session, run_row: WorkflowRun, step: WorkflowStep, context: dict[str, Any]) -> dict[str, Any]:
    started = perf_counter()
    invocation = _start_invocation(db, run_row=run_row, step=step, task_type="CHARACTER_DESIGN")
    raw = _run_text_model(
        run_row, operation="CHARACTER_DESIGN",
        system_suffix="根据冻结视频分析、脚本、商品、创意和已锁定大纲输出角色 JSON。禁止创造商品功效。",
        user_payload=deepcopy(context),
        output_contract='{"roles":[{"role_id":"string","name":"string","age_range":"string","gender":"string","identity_and_occupation":"string","personality":"string","dramatic_function":"string","relationships":[],"appearance":"string","hairstyle":"string","costume":"string","fixed_visual_features":["string"],"immutable_features":["string"],"product_relationship":"string","buyer":true,"user":true,"decision_influencer":false,"image_prompt":"string"}]}'
    )
    payload = {"roles": _mock_roles()} if raw.get("_mock") else raw
    roles = _validate_roles(payload)
    outline = context["outline"]
    row = CommerceCharacterDesignVersion(
        story_run_id=context["story_run_id"], source_outline_version_id=outline["id"],
        source_product_asset_version_id=(context["commerce_mainline"]["product_asset_version"] or {})["id"], workflow_run_id=run_row.id,
        model_invocation_id=invocation.id if invocation else None,
        version=_next_version(db, CommerceCharacterDesignVersion, story_run_id=context["story_run_id"]), status="READY",
        content={"roles": roles}, input_snapshot=deepcopy(context),
        prompt_snapshot=deepcopy((run_row.input_snapshot or {}).get("prompt_template") or {}), raw_response=deepcopy(payload),
    )
    _mark_stale(db, context["story_run_id"], source="CHARACTER")
    db.add(row); db.flush()
    _finish_invocation(invocation, output_reference={"character_design_version_id": row.id}, started=started)
    return {"character_design_version_id": row.id, "role_count": len(roles)}


def _execute_scene_design(db: Session, run_row: WorkflowRun, step: WorkflowStep, context: dict[str, Any]) -> dict[str, Any]:
    started = perf_counter()
    invocation = _start_invocation(db, run_row=run_row, step=step, task_type="SCENE_DESIGN")
    raw = _run_text_model(
        run_row, operation="SCENE_DESIGN",
        system_suffix="基于冻结大纲、商品融入方案和已锁定角色设定输出连续场景 JSON；禁止创造商品功效或包装。",
        user_payload=deepcopy(context),
        output_contract='{"scenes":[{"scene_id":"string","name":"string","purpose":"string","time":"string","location":"string","lighting":"string","color_tone":"string","spatial_layout":"string","fixed_props":["string"],"product_position":"string","product_usage_environment":"string","continuity_requirements":["string"],"immutable_features":["string"],"base_image_prompt":"string"}]}'
    )
    payload = {"scenes": _mock_scenes()} if raw.get("_mock") else raw
    scenes = _validate_scenes(payload)
    outline, character = context["outline"], context["character_design"]
    row = CommerceSceneDesignVersion(
        story_run_id=context["story_run_id"], source_outline_version_id=outline["id"], character_design_version_id=character["id"],
        source_product_asset_version_id=(context["commerce_mainline"]["product_asset_version"] or {})["id"], workflow_run_id=run_row.id,
        model_invocation_id=invocation.id if invocation else None,
        version=_next_version(db, CommerceSceneDesignVersion, story_run_id=context["story_run_id"]), status="READY",
        content={"scenes": scenes}, input_snapshot=deepcopy(context),
        prompt_snapshot=deepcopy((run_row.input_snapshot or {}).get("prompt_template") or {}), raw_response=deepcopy(payload),
    )
    _mark_stale(db, context["story_run_id"], source="SCENE")
    db.add(row); db.flush()
    _finish_invocation(invocation, output_reference={"scene_design_version_id": row.id}, started=started)
    return {"scene_design_version_id": row.id, "scene_count": len(scenes)}


def _execute_storyboard(db: Session, run_row: WorkflowRun, step: WorkflowStep, context: dict[str, Any]) -> dict[str, Any]:
    started = perf_counter()
    invocation = _start_invocation(db, run_row=run_row, step=step, task_type="DIRECTOR_PLAN")
    characters = (context["character_design"].get("content") or {}).get("roles") or []
    scenes = (context["scene_design"].get("content") or {}).get("scenes") or []
    nodes = context["outline"]["integration_nodes"]
    raw = _run_text_model(
        run_row, operation="STORYBOARD",
        system_suffix="输出 3 至 5 个独立视频镜头 JSON。每一镜必须有非空片段摘要，并且商品镜头只使用冻结融入节点和商品证据。",
        user_payload=deepcopy(context),
        output_contract='{"shots":[{"shot_id":"string","shot_number":1,"segment_summary":"string","story_paragraph":"string","duration_ms":6000,"character_ids":["string"],"scene_id":"string","product_integration_node_id":"string","product_visible":true,"shot_scale":"string","camera_position":"string","camera_move":"string","composition":"string","action":"string","expression":"string","dialogue":"string","narration":"string","product_position":"string","product_action":"string","product_exposure_ms":1000,"previous_continuity_state":"string","ending_continuity_state":"string","next_transition_requirement":"string","keyframe_prompt":"string","video_prompt":"string","forbidden_content":["string"]}]}'
    )
    payload = {"shots": _mock_shots(characters, scenes, nodes)} if raw.get("_mock") else raw
    shots = _validate_shots(
        payload, role_ids={item["role_id"] for item in characters}, scene_ids={item["scene_id"] for item in scenes}, nodes=nodes,
        product=context["commerce_mainline"]["product_asset_version"],
    )
    row = CommerceStoryboardVersion(
        story_run_id=context["story_run_id"], source_outline_version_id=context["outline"]["id"],
        character_design_version_id=context["character_design"]["id"], scene_design_version_id=context["scene_design"]["id"],
        source_product_asset_version_id=(context["commerce_mainline"]["product_asset_version"] or {})["id"], workflow_run_id=run_row.id,
        model_invocation_id=invocation.id if invocation else None,
        version=_next_version(db, CommerceStoryboardVersion, story_run_id=context["story_run_id"]), status="READY",
        content={"shots": shots, "product_integration_nodes": nodes}, input_snapshot=deepcopy(context),
        prompt_snapshot=deepcopy((run_row.input_snapshot or {}).get("prompt_template") or {}), raw_response=deepcopy(payload),
    )
    _mark_stale(db, context["story_run_id"], source="STORYBOARD")
    db.add(row); db.flush()
    _finish_invocation(invocation, output_reference={"storyboard_version_id": row.id, "shot_count": len(shots)}, started=started)
    return {"storyboard_version_id": row.id, "shot_count": len(shots)}


def _image_from_adapter(
    db: Session,
    *,
    run_row: WorkflowRun,
    step: WorkflowStep,
    prompt: str,
    references: list[str],
    reference_images: list[LocalImageReference] | None = None,
    asset_kind: str,
    asset_id: str,
    version: int,
) -> tuple[str, ModelInvocation | None]:
    started = perf_counter()
    invocation = _start_invocation(
        db,
        run_row=run_row,
        step=step,
        task_type="IMAGE_GENERATE",
        idempotency_suffix=f"{asset_kind}:{asset_id}:v{version}",
    )
    if invocation is not None and reference_images:
        # 仅追加可追溯摘要，绝不把 Data URL 或字节写入 ModelInvocation 输入快照。
        safe_input_snapshot = deepcopy(invocation.input_snapshot or {})
        safe_input_snapshot["reference_assets"] = [item.audit_metadata() for item in reference_images]
        invocation.input_snapshot = safe_input_snapshot
    binding = (run_row.input_snapshot or {}).get("model_binding") or {}
    profile = binding.get("profile_snapshot") if isinstance(binding, dict) else None
    if not isinstance(profile, dict):
        raise RuntimeError("图片任务缺少冻结模型")
    if is_mock_adapter(profile):
        url = f"mock://commerce-image/{asset_kind}/{asset_id}/v{version}"
        media_units: dict[str, Any] = {"images": 1, "reference_image_count": len(references)}
        provider_task_id: str | None = None
    else:
        assert_supported(profile, "IMAGE_GENERATE")
        provider, first_result = start_image_generation(
            profile,
            prompt=prompt,
            reference_image_urls=references,
            reference_images=reference_images,
            existing_provider_task_id=invocation.provider_task_id if invocation is not None else None,
        )
        provider_task_id = first_result.provider_task_id or (invocation.provider_task_id if invocation is not None else None)
        if invocation is not None and provider_task_id and invocation.provider_task_id != provider_task_id:
            invocation.provider_task_id = provider_task_id
            # Fal 先提交再轮询：任务号必须先提交，Worker 中断时才不会重提付费图片。
            db.commit()
        result = wait_for_image_result(provider, profile, first_result)
        provider_task_id = result.provider_task_id or provider_task_id
        if result.status != "SUCCEEDED" or (not result.image_url and not result.image_bytes):
            message = result.error_message or "图片供应商任务失败"
            _fail_invocation(invocation, message, started=started, provider_task_id=provider_task_id)
            db.commit()
            raise RuntimeError(message)
        effective_references = reference_images if reference_images else references
        media_units = {"images": 1, "reference_image_count": len(effective_references)}
        if reference_images:
            # Data URL 本身不进入 ModelInvocation；只保留可追溯的资产身份元数据。
            media_units["reference_assets"] = [item.audit_metadata() for item in reference_images]
        if result.content_type:
            media_units["content_type"] = result.content_type
        if result.byte_size is not None:
            media_units["byte_size"] = result.byte_size
        if result.sha256:
            media_units["sha256"] = result.sha256
        if result.width is not None:
            media_units["width"] = result.width
        if result.height is not None:
            media_units["height"] = result.height
        if isinstance(result.image_bytes, bytes):
            if not isinstance(result.content_type, str):
                raise RuntimeError("方舟图片结果缺少 MIME 类型")
            url = persist_v1_image_bytes(
                project_id=run_row.project_id,
                asset_kind=asset_kind,
                asset_id=asset_id,
                version=version,
                content=result.image_bytes,
                content_type=result.content_type,
            )
        else:
            # 历史 Fal/OpenAI 兼容 Profile 仍保留原有转存行为；官方方舟 Adapter
            # 永远走上方的本地字节存储分支，不会把临时签名 URL 写入数据库。
            if not isinstance(result.image_url, str) or not result.image_url:
                raise RuntimeError("图片供应商未返回可持久化结果")
            url = persist_v1_image(
                project_id=run_row.project_id,
                asset_kind=asset_kind,
                asset_id=asset_id,
                version=version,
                source_url=result.image_url,
            )
    _finish_invocation(
        invocation,
        output_reference={"asset_kind": asset_kind, "asset_id": asset_id, "version": version},
        started=started,
        media_units=media_units,
        provider_task_id=provider_task_id,
    )
    return url, invocation


def _execute_character_images(db: Session, run_row: WorkflowRun, step: WorkflowStep, context: dict[str, Any]) -> dict[str, Any]:
    design_id = context["character_design"]["id"]
    roles = (context["character_design"].get("content") or {}).get("roles") or []
    created: list[str] = []
    for role in roles:
        role_id = role["role_id"]
        version = _next_version(db, CommerceCharacterReferenceImage, story_run_id=context["story_run_id"], extra_column="role_id", extra_value=role_id)
        prompt = str(role["image_prompt"])
        url, invocation = _image_from_adapter(db, run_row=run_row, step=step, prompt=prompt, references=[], asset_kind="commerce-character", asset_id=role_id, version=version)
        row = CommerceCharacterReferenceImage(story_run_id=context["story_run_id"], character_design_version_id=design_id, role_id=role_id, workflow_run_id=run_row.id, model_invocation_id=invocation.id if invocation else None, version=version, image_url=url, prompt_snapshot=prompt, input_snapshot={"character_design_version_id": design_id, "role": deepcopy(role)}, status="READY")
        db.add(row); db.flush(); created.append(row.id)
    return {"character_reference_image_ids": created}


def _execute_scene_images(db: Session, run_row: WorkflowRun, step: WorkflowStep, context: dict[str, Any]) -> dict[str, Any]:
    design_id = context["scene_design"]["id"]
    scenes = (context["scene_design"].get("content") or {}).get("scenes") or []
    product_urls = _product_reference_urls(context["commerce_mainline"])
    binding = (run_row.input_snapshot or {}).get("model_binding") or {}
    profile = binding.get("profile_snapshot") if isinstance(binding, dict) else None
    # 本轮仅把“角色图 + 场景图”接入方舟关键帧参考图协议。冻结商品图可能是旧的
    # 公网地址或第三方地址，不能被当作本机参考图发送到方舟；场景生成仍使用其完整
    # 文本设计描述走已验证的纯文本单图路径。
    scene_reference_urls = (
        [] if isinstance(profile, dict) and adapter_key(profile) == "volcengine_ark_image" else product_urls
    )
    created: list[str] = []
    for scene in scenes:
        scene_id = scene["scene_id"]
        version = _next_version(db, CommerceSceneReferenceImage, story_run_id=context["story_run_id"], extra_column="scene_id", extra_value=scene_id)
        prompt = str(scene["base_image_prompt"])
        url, invocation = _image_from_adapter(db, run_row=run_row, step=step, prompt=prompt, references=scene_reference_urls, asset_kind="commerce-scene", asset_id=scene_id, version=version)
        row = CommerceSceneReferenceImage(story_run_id=context["story_run_id"], scene_design_version_id=design_id, scene_id=scene_id, workflow_run_id=run_row.id, model_invocation_id=invocation.id if invocation else None, version=version, image_url=url, prompt_snapshot=prompt, input_snapshot={"scene_design_version_id": design_id, "scene": deepcopy(scene), "product_reference_count": len(product_urls)}, status="READY")
        db.add(row); db.flush(); created.append(row.id)
    return {"scene_reference_image_ids": created}


def _shot_for_target(context: dict[str, Any]) -> dict[str, Any]:
    target_id = context.get("target_id")
    shots = ((context.get("storyboard") or {}).get("content") or {}).get("shots") or []
    for shot in shots:
        if isinstance(shot, dict) and shot.get("shot_id") == target_id:
            return shot
    _error("镜头不属于当前锁定导演分镜", status.HTTP_422_UNPROCESSABLE_CONTENT)
    raise AssertionError("unreachable")


def _keyframe_assets(
    db: Session, context: dict[str, Any], shot: dict[str, Any]
) -> tuple[list[str], list[dict[str, str]], dict[str, Any]]:
    character_design_id = context["character_design"]["id"]
    scene_design_id = context["scene_design"]["id"]
    character_rows = []
    local_reference_assets: list[dict[str, str]] = []
    urls: list[str] = []
    for role_id in shot["character_ids"]:
        image = _latest_locked_image(db, CommerceCharacterReferenceImage, design_column="character_design_version_id", design_id=character_design_id, logical_id_column="role_id", logical_id=role_id)
        if image is None or not image.image_url:
            _error("镜头所需角色参考图尚未锁定")
        character_rows.append({"id": image.id, "role_id": role_id, "url": image.image_url, "version": image.version})
        urls.append(image.image_url)
        # 图片文件历史上按逻辑 role_id 分目录保存；冻结图片记录 ID 则用于审计。
        # 两者都来自本次已锁定的同一行，不能由浏览器传入。
        local_reference_assets.append(
            {
                "asset_id": image.id,
                "storage_namespace_id": image.role_id,
                "role": "character",
                "image_url": image.image_url,
            }
        )
    scene = _latest_locked_image(db, CommerceSceneReferenceImage, design_column="scene_design_version_id", design_id=scene_design_id, logical_id_column="scene_id", logical_id=shot["scene_id"])
    if scene is None or not scene.image_url:
        _error("镜头所需场景基础图尚未锁定")
    urls.append(scene.image_url)
    local_reference_assets.append(
        {
            "asset_id": scene.id,
            "storage_namespace_id": scene.scene_id,
            "role": "scene",
            "image_url": scene.image_url,
        }
    )
    product_urls = _product_reference_urls(context["commerce_mainline"])
    if shot.get("product_visible") and not product_urls:
        _error("商品镜头需要在冻结商品版本中配置至少一张真实参考图")
    urls.extend(product_urls)
    return (
        list(dict.fromkeys(urls)),
        local_reference_assets,
        {
            "character_reference_image_ids": [item["id"] for item in character_rows],
            "scene_reference_image_id": scene.id,
            # 商品地址不进入方舟图生图请求；商品是否展示仍由冻结分镜与融入节点约束。
            "product_reference_count": len(product_urls),
        },
    )


def _execute_keyframe(db: Session, run_row: WorkflowRun, step: WorkflowStep, context: dict[str, Any]) -> dict[str, Any]:
    shot = _shot_for_target(context)
    existing = db.scalars(
        select(CommerceShotKeyframeVersion).where(
            CommerceShotKeyframeVersion.workflow_run_id == run_row.id,
            CommerceShotKeyframeVersion.shot_id == shot["shot_id"],
            CommerceShotKeyframeVersion.image_url.is_not(None),
            CommerceShotKeyframeVersion.status.in_(("READY", "LOCKED")),
        )
    ).first()
    if existing is not None:
        # 同一同步图片 Step 再次被 Worker 领取时直接复用已有关键帧，避免重复 POST。
        return {"shot_keyframe_version_id": existing.id, "shot_id": existing.shot_id}
    references, local_reference_assets, asset_snapshot = _keyframe_assets(db, context, shot)
    binding = (run_row.input_snapshot or {}).get("model_binding") or {}
    profile = binding.get("profile_snapshot") if isinstance(binding, dict) else None
    ark_reference_images: list[LocalImageReference] | None = None
    if isinstance(profile, dict) and adapter_key(profile) == "volcengine_ark_image":
        ark_reference_images = []
        for reference in local_reference_assets:
            ark_reference_images.append(
                local_asset_storage.load_generated_image_reference(
                    project_id=run_row.project_id,
                    asset_id=reference["asset_id"],
                    role=reference["role"],
                    image_url=reference["image_url"],
                    storage_namespace_id=reference["storage_namespace_id"],
                )
            )
        seen_sha256: set[str] = set()
        ark_reference_images = [
            item for item in ark_reference_images if not (item.sha256 in seen_sha256 or seen_sha256.add(item.sha256))
        ]
        if len(ark_reference_images) > 14:
            _error("分镜关键帧最多允许 14 张参考图", status.HTTP_422_UNPROCESSABLE_CONTENT)
        asset_snapshot["reference_assets"] = [item.audit_metadata() for item in ark_reference_images]
        # 方舟只收到经过 Storage 读取的 Data URL，绝不会收到旧流程中的本地 URL、商品
        # URL 或 Docker 地址。商品的剧情追溯仍保留在冻结分镜中。
        references = []
    storyboard = context["storyboard"]
    version = _next_version(db, CommerceShotKeyframeVersion, story_run_id=context["story_run_id"], extra_column="shot_id", extra_value=shot["shot_id"])
    url, invocation = _image_from_adapter(
        db,
        run_row=run_row,
        step=step,
        prompt=str(shot["keyframe_prompt"]),
        references=references,
        reference_images=ark_reference_images,
        asset_kind="commerce-keyframe",
        asset_id=shot["shot_id"],
        version=version,
    )
    _mark_stale(db, context["story_run_id"], source="KEYFRAME", shot_id=shot["shot_id"])
    row = CommerceShotKeyframeVersion(story_run_id=context["story_run_id"], storyboard_version_id=storyboard["id"], shot_id=shot["shot_id"], shot_number=shot["shot_number"], workflow_run_id=run_row.id, model_invocation_id=invocation.id if invocation else None, version=version, image_url=url, prompt_snapshot=str(shot["keyframe_prompt"]), input_asset_snapshot=asset_snapshot, status="READY")
    db.add(row); db.flush()
    return {"shot_keyframe_version_id": row.id, "shot_id": shot["shot_id"]}


def _execute_video_prompt(db: Session, run_row: WorkflowRun, step: WorkflowStep, context: dict[str, Any]) -> dict[str, Any]:
    shot = _shot_for_target(context)
    storyboard = context["storyboard"]
    keyframe = db.scalars(select(CommerceShotKeyframeVersion).where(CommerceShotKeyframeVersion.storyboard_version_id == storyboard["id"], CommerceShotKeyframeVersion.shot_id == shot["shot_id"], CommerceShotKeyframeVersion.status == "LOCKED").order_by(CommerceShotKeyframeVersion.version.desc())).first()
    if keyframe is None:
        _error("必须先锁定该镜头的关键帧才能生成视频 Prompt")
    started = perf_counter()
    invocation = _start_invocation(db, run_row=run_row, step=step, task_type="DIRECTOR_PLAN")
    raw = _run_text_model(run_row, operation="VIDEO_PROMPT", system_suffix="根据冻结导演镜头和锁定关键帧生成一个用于图生视频的动作 Prompt；禁止增加商品功效。", user_payload={"shot": deepcopy(shot), "keyframe_id": keyframe.id, "mainline": deepcopy(context["commerce_mainline"])}, output_contract='{"video_prompt":"string"}')
    prompt = str(shot["video_prompt"]) if raw.get("_mock") else _require_text(raw.get("video_prompt") if isinstance(raw, dict) else None, "video_prompt")
    version = _next_version(db, CommerceVideoPromptVersion, story_run_id=context["story_run_id"], extra_column="shot_id", extra_value=shot["shot_id"])
    row = CommerceVideoPromptVersion(story_run_id=context["story_run_id"], storyboard_version_id=storyboard["id"], shot_id=shot["shot_id"], shot_number=shot["shot_number"], keyframe_version_id=keyframe.id, workflow_run_id=run_row.id, model_invocation_id=invocation.id if invocation else None, version=version, prompt=prompt, trace={"shot_id": shot["shot_id"], "keyframe_version_id": keyframe.id, "forbidden_content": deepcopy(shot["forbidden_content"])}, status="LOCKED", locked_at=utcnow())
    db.add(row); db.flush()
    _finish_invocation(invocation, output_reference={"commerce_video_prompt_version_id": row.id}, started=started)
    return {"commerce_video_prompt_version_id": row.id, "shot_id": shot["shot_id"]}


def _probe_mp4(source: str | Path, *, require_https: bool = False) -> dict[str, Any]:
    """用 ffprobe 验证供应商片段或最终本地输出，而不是只相信任务状态。"""

    import shutil
    if shutil.which("ffprobe") is None:
        raise RuntimeError("真实视频验收需要安装系统依赖：ffprobe")
    raw_source = str(source)
    if require_https and not raw_source.startswith("https://"):
        raise RuntimeError("真实视频供应商必须返回 HTTPS MP4 地址")
    if not require_https and not Path(raw_source).is_file():
        raise RuntimeError("FFmpeg 输出文件不存在，无法执行 ffprobe 验收")
    try:
        result = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", raw_source], capture_output=True, text=True, timeout=90, check=True)
    except (CalledProcessError, TimeoutExpired) as exc:
        raise RuntimeError("ffprobe 无法读取供应商返回的视频 MP4") from exc
    import json
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("ffprobe 未返回可解析媒体元数据") from exc
    streams = payload.get("streams") if isinstance(payload, dict) else []
    video_stream = next((item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"), None) if isinstance(streams, list) else None
    if video_stream is None:
        raise RuntimeError("供应商结果不包含可播放的视频流")
    format_payload = payload.get("format") if isinstance(payload, dict) else {}
    duration = format_payload.get("duration") if isinstance(format_payload, dict) else None
    try:
        duration_ms = int(float(duration) * 1000)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("ffprobe 结果缺少视频时长") from exc
    def _fps(value: Any) -> float | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            numerator, denominator = value.split("/", 1)
            return round(float(numerator) / float(denominator), 3) if float(denominator) else None
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    audio_streams = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"]
    return {
        "duration_ms": duration_ms,
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "frame_rate": _fps(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")),
        "video_codec": video_stream.get("codec_name"),
        "audio_track_count": len(audio_streams),
    }


def _probe_remote_mp4(url: str) -> dict[str, Any]:
    return _probe_mp4(url, require_https=True)


def _execute_video_render(db: Session, run_row: WorkflowRun, step: WorkflowStep, context: dict[str, Any]) -> dict[str, Any]:
    shot = _shot_for_target(context)
    storyboard = context["storyboard"]
    prompt = db.scalars(select(CommerceVideoPromptVersion).where(CommerceVideoPromptVersion.storyboard_version_id == storyboard["id"], CommerceVideoPromptVersion.shot_id == shot["shot_id"], CommerceVideoPromptVersion.status == "LOCKED").order_by(CommerceVideoPromptVersion.version.desc())).first()
    if prompt is None:
        _error("必须使用当前已锁定的视频 Prompt 生成视频")
    keyframe = db.get(CommerceShotKeyframeVersion, prompt.keyframe_version_id)
    if keyframe is None or keyframe.status != "LOCKED" or not keyframe.image_url:
        _error("视频 Prompt 引用的关键帧未锁定或已失效")
    existing = db.scalars(select(CommerceVideoClipVersion).where(CommerceVideoClipVersion.workflow_run_id == run_row.id)).first()
    if existing is None:
        version = _next_version(db, CommerceVideoClipVersion, story_run_id=context["story_run_id"], extra_column="shot_id", extra_value=shot["shot_id"])
        existing = CommerceVideoClipVersion(story_run_id=context["story_run_id"], storyboard_version_id=storyboard["id"], shot_id=shot["shot_id"], shot_number=shot["shot_number"], keyframe_version_id=keyframe.id, video_prompt_version_id=prompt.id, workflow_run_id=run_row.id, version=version, idempotency_key=f"clip:{step.idempotency_key}", input_asset_snapshot=deepcopy(keyframe.input_asset_snapshot), status="RUNNING")
        db.add(existing); db.flush()
    started = perf_counter()
    invocation = _start_invocation(db, run_row=run_row, step=step, task_type="VIDEO_GENERATE")
    existing.model_invocation_id = invocation.id if invocation else None
    binding = (run_row.input_snapshot or {}).get("model_binding") or {}
    profile = binding.get("profile_snapshot") if isinstance(binding, dict) else None
    if not isinstance(profile, dict):
        raise RuntimeError("视频任务缺少冻结模型")
    if is_mock_adapter(profile):
        existing.status = "SUCCEEDED"; existing.provider_task_id = f"mock-commerce-video-{existing.id[:8]}"; existing.video_url = f"mock://commerce-video/{existing.id}"; existing.duration_ms = shot["duration_ms"]; existing.media_metadata = {"mode": "mock", "not_a_real_mp4": True}
        _finish_invocation(invocation, output_reference={"commerce_video_clip_version_id": existing.id}, started=started, media_units={"video_clips": 1, "mode": "mock"}, provider_task_id=existing.provider_task_id)
        existing.finished_at = utcnow()
        return {"commerce_video_clip_version_id": existing.id, "provider_task_id": existing.provider_task_id, "mode": "mock"}
    assert_supported(profile, "VIDEO_GENERATE")
    provider = video_provider(profile)
    if existing.provider_task_id:
        # 任务号一旦落库，此次 Worker 领取只负责恢复查询。即便本机原首帧文件
        # 暂时不可读取，也绝不能因为重新加载首帧而走到第二次付费 POST。
        first = provider.poll(existing.provider_task_id)
    else:
        first_frame = local_asset_storage.load_generated_image_reference(
            project_id=run_row.project_id,
            asset_id=keyframe.id,
            storage_namespace_id=keyframe.shot_id,
            role="first_frame",
            image_url=keyframe.image_url,
        )
        safe_input_assets = deepcopy(keyframe.input_asset_snapshot)
        safe_input_assets["keyframe_asset"] = first_frame.audit_metadata()
        existing.input_asset_snapshot = safe_input_assets
        if invocation is not None:
            safe_invocation_input = deepcopy(invocation.input_snapshot or {})
            safe_invocation_input["first_frame_asset"] = first_frame.audit_metadata()
            invocation.input_snapshot = safe_invocation_input
        try:
            submitted = provider.submit(
                create_video_request(
                    project_id=run_row.project_id,
                    shot_number=shot["shot_number"],
                    prompt=prompt.prompt,
                    image_urls=[],
                    reference_images=[first_frame],
                )
            )
        except Exception as exc:
            # 创建接口可能在供应商已经接到请求前直接拒绝（例如未开通模型）。此时
            # 仍必须保留失败片段和失败审计，不能让外层事务回滚后只剩一个无上下文
            # 的 WorkflowStep。这里没有 provider_task_id，人工重试才可显式新建一次。
            message = sanitize_error_summary(exc, max_length=2000)
            existing.status = "FAILED"
            existing.error_message = message
            existing.finished_at = utcnow()
            _fail_invocation(invocation, message, started=started)
            db.commit()
            raise
        existing.provider_task_id = submitted.provider_task_id
        step.provider_task_id = submitted.provider_task_id
        db.commit()  # 任务号先持久化，重启后只能 poll，绝不重复扣费提交。
        first = submitted
    result = wait_for_video_result(provider, profile, first)
    existing.provider_task_id = result.provider_task_id or existing.provider_task_id
    step.provider_task_id = existing.provider_task_id
    if result.status != "SUCCEEDED" or not result.video_url:
        raise RuntimeError(result.error_message or "视频供应商任务失败")
    content_type, content = local_asset_storage.download_generated_video(result.video_url)
    local_video_url = local_asset_storage.save_generated_video_bytes(
        project_id=run_row.project_id,
        asset_kind="commerce-video",
        asset_id=existing.id,
        version=existing.version,
        content=content,
        content_type=content_type,
    )
    media = _probe_mp4(local_asset_storage.generated_media_path(local_video_url))
    existing.status = "SUCCEEDED"; existing.video_url = local_video_url; existing.duration_ms = media["duration_ms"]; existing.media_metadata = media; existing.finished_at = utcnow()
    _finish_invocation(invocation, output_reference={"commerce_video_clip_version_id": existing.id}, started=started, media_units={"video_clips": 1, "duration_ms": existing.duration_ms}, provider_task_id=existing.provider_task_id)
    return {"commerce_video_clip_version_id": existing.id, "provider_task_id": existing.provider_task_id, "mode": "real"}


def _approved_current_clips(db: Session, story_run: StoryRun, storyboard: CommerceStoryboardVersion) -> list[CommerceVideoClipVersion]:
    shots = (storyboard.content or {}).get("shots") or []
    clips: list[CommerceVideoClipVersion] = []
    for shot in shots:
        # 必须检查该镜头“当前最新版本”，而不是从任意历史 APPROVED 版本中挑一个。
        # 否则制作人重生 v2 后，即使 v2 尚未审核，也会悄悄把旧 v1 编进新成片。
        row = db.scalars(
            select(CommerceVideoClipVersion)
            .where(
                CommerceVideoClipVersion.storyboard_version_id == storyboard.id,
                CommerceVideoClipVersion.shot_id == shot.get("shot_id"),
            )
            .order_by(CommerceVideoClipVersion.version.desc())
        ).first()
        if row is None or row.status != "APPROVED":
            _error(f"镜头 {shot.get('shot_number')} 当前视频版本尚未审核通过，不能合成成片")
        clips.append(row)
    return clips


def _execute_final_compose(db: Session, run_row: WorkflowRun, step: WorkflowStep, context: dict[str, Any]) -> dict[str, Any]:
    story = _story_run(db, context["story_run_id"])
    storyboard = _current_storyboard(db, story.id)
    clips = _approved_current_clips(db, story, storyboard)
    version = _next_version(db, CommerceFinalVideo, story_run_id=story.id)
    row = CommerceFinalVideo(story_run_id=story.id, storyboard_version_id=storyboard.id, workflow_run_id=run_row.id, version=version, clip_ids=[clip.id for clip in clips], input_snapshot={"storyboard_version_id": storyboard.id, "clip_ids": [clip.id for clip in clips]}, status="RUNNING")
    db.add(row); db.flush()
    if all((clip.video_url or "").startswith("mock://") for clip in clips):
        row.status = "SUCCEEDED"; row.output_url = f"mock://commerce-final/{row.id}"; row.media_metadata = {"mode": "mock", "not_a_real_mp4": True}; row.finished_at = utcnow()
        return {"commerce_final_video_id": row.id, "mode": "mock"}
    if any(not (clip.video_url or "").startswith("https://") for clip in clips):
        raise RuntimeError("真实成片只能使用已审核的 HTTPS 视频片段")
    delivery = _compose_real_video(project_id=story.project_id, final_video_id=row.id, clips=clips, snapshot={"provider_config": {}})
    # ``_compose_real_video`` 已复用安全下载、统一编码和原子交付。交付前再做一次
    # 最终 ffprobe；本地存储可直接读取，S3 则由公开 HTTPS 地址读取。
    if delivery.public_url:
        probe = _probe_mp4(delivery.public_url, require_https=True)
    else:
        probe = _probe_mp4(local_asset_storage.final_video_path(delivery.storage_key))
    row.storage_key = delivery.storage_key; row.output_url = delivery.public_url; row.status = "SUCCEEDED"; row.finished_at = utcnow()
    row.media_metadata = {"mode": "real", "ffmpeg": "reencoded", **probe}
    return {"commerce_final_video_id": row.id, "mode": "real"}


def _execute_operation(db: Session, run_row: WorkflowRun, step: WorkflowStep, operation: str, context: dict[str, Any]) -> dict[str, Any]:
    handlers = {
        "CHARACTER_DESIGN": _execute_character_design,
        "SCENE_DESIGN": _execute_scene_design,
        "STORYBOARD": _execute_storyboard,
        "CHARACTER_IMAGES": _execute_character_images,
        "SCENE_IMAGES": _execute_scene_images,
        "SHOT_KEYFRAME": _execute_keyframe,
        "VIDEO_PROMPT": _execute_video_prompt,
        "VIDEO_RENDER": _execute_video_render,
        "FINAL_COMPOSE": _execute_final_compose,
    }
    return handlers[operation](db, run_row, step, context)


def execute_commerce_production_workflow(run_id: str) -> None:
    """Worker 入口。领取步骤后才调用 Adapter，确保重复队列消息不会重复扣费。"""

    from app.core.database import SessionLocal
    db = SessionLocal()
    started: float | None = None
    try:
        run_row = db.get(WorkflowRun, run_id)
        if run_row is None or not run_row.workflow_key.startswith(WORKFLOW_PREFIX):
            return
        step = db.scalars(select(WorkflowStep).where(WorkflowStep.workflow_run_id == run_id).order_by(WorkflowStep.position)).first()
        if step is None:
            return
        operation = run_row.workflow_key.removeprefix(WORKFLOW_PREFIX).upper()
        if step.status == RunStatus.PENDING:
            claimed = db.execute(
                update(WorkflowStep)
                .where(WorkflowStep.id == step.id, WorkflowStep.status == RunStatus.PENDING)
                .values(status=RunStatus.RUNNING, started_at=utcnow())
            ).rowcount
            if claimed != 1:
                return
        elif step.status == RunStatus.RUNNING and operation == "VIDEO_RENDER":
            # 进程在提交后退出时，任务号已经与 WorkflowStep/Clip 一起落库。此处只
            # 恢复 poll；没有任务号的 RUNNING 步骤仍由人工明确重试，绝不猜测并二次
            # submit 一个可能已扣费的供应商任务。
            clip = db.scalar(
                select(CommerceVideoClipVersion).where(CommerceVideoClipVersion.workflow_run_id == run_id)
            )
            if not (step.provider_task_id or (clip.provider_task_id if clip is not None else None)):
                return
        else:
            return
        run_row.status = RunStatus.RUNNING; run_row.started_at = run_row.started_at or utcnow(); db.commit()
        context = ((run_row.input_snapshot or {}).get("commerce_production") or {})
        if operation not in OPERATION_SPECS or not isinstance(context, dict):
            raise RuntimeError("Commerce 生产任务冻结快照无效")
        started = perf_counter()
        output = _execute_operation(db, run_row, step, operation, context)
        step.output_payload = {"artifact_references": output}; step.status = RunStatus.SUCCEEDED; step.progress = 100; step.finished_at = utcnow(); run_row.status = RunStatus.SUCCEEDED; run_row.finished_at = step.finished_at
        db.commit()
    except Exception as exc:
        db.rollback()
        error_message = sanitize_error_summary(exc, max_length=2000)
        run_row = db.get(WorkflowRun, run_id)
        if run_row is not None:
            step = db.scalars(select(WorkflowStep).where(WorkflowStep.workflow_run_id == run_id).order_by(WorkflowStep.position)).first()
            if step is not None:
                invocation = db.scalars(select(ModelInvocation).where(ModelInvocation.workflow_step_id == step.id, ModelInvocation.status == RunStatus.RUNNING)).first()
                _fail_invocation(invocation, error_message, started=started, provider_task_id=step.provider_task_id)
                step.status = RunStatus.FAILED; step.error_message = error_message; step.finished_at = utcnow()
                # 已创建的片段保留 FAILED 版本，让前端可定位重试，而不是丢失供应商任务号。
                clip = db.scalars(select(CommerceVideoClipVersion).where(CommerceVideoClipVersion.workflow_run_id == run_id)).first()
                if clip is not None and clip.status not in {"SUCCEEDED", "APPROVED"}:
                    clip.status = "FAILED"; clip.error_message = error_message; clip.finished_at = utcnow(); clip.retry_count += 1
            run_row.status = RunStatus.FAILED; run_row.finished_at = utcnow(); db.commit()
    finally:
        db.close()


def _review(db: Session, *, story_run: StoryRun, target_type: str, target_id: str, decision: str, reviewer_label: str, note: str | None) -> None:
    db.add(ReviewDecision(project_id=story_run.project_id, target_type=target_type, target_id=target_id, decision=decision, reviewer_label=(reviewer_label or "制作人")[:120], note=(note or None)))


def lock_character_design(db: Session, *, story_run_id: str, version_id: str, reviewer_label: str, note: str | None) -> CommerceCharacterDesignVersion:
    story = _story_run(db, story_run_id); row = db.get(CommerceCharacterDesignVersion, version_id)
    if row is None or row.story_run_id != story.id or row.status != "READY": _error("角色设定不存在或当前不可锁定", status.HTTP_422_UNPROCESSABLE_CONTENT)
    db.execute(update(CommerceCharacterDesignVersion).where(CommerceCharacterDesignVersion.story_run_id == story.id, CommerceCharacterDesignVersion.status == "LOCKED").values(status="SUPERSEDED"))
    row.status = "LOCKED"; row.locked_at = utcnow(); _review(db, story_run=story, target_type="COMMERCE_CHARACTER_DESIGN", target_id=row.id, decision="LOCKED", reviewer_label=reviewer_label, note=note); db.commit(); db.refresh(row); return row


def lock_scene_design(db: Session, *, story_run_id: str, version_id: str, reviewer_label: str, note: str | None) -> CommerceSceneDesignVersion:
    story = _story_run(db, story_run_id); row = db.get(CommerceSceneDesignVersion, version_id)
    if row is None or row.story_run_id != story.id or row.status != "READY": _error("场景设定不存在或当前不可锁定", status.HTTP_422_UNPROCESSABLE_CONTENT)
    if db.get(CommerceCharacterDesignVersion, row.character_design_version_id).status != "LOCKED": _error("场景设定必须绑定当前锁定角色版本")
    db.execute(update(CommerceSceneDesignVersion).where(CommerceSceneDesignVersion.story_run_id == story.id, CommerceSceneDesignVersion.status == "LOCKED").values(status="SUPERSEDED"))
    row.status = "LOCKED"; row.locked_at = utcnow(); _review(db, story_run=story, target_type="COMMERCE_SCENE_DESIGN", target_id=row.id, decision="LOCKED", reviewer_label=reviewer_label, note=note); db.commit(); db.refresh(row); return row


def lock_storyboard(db: Session, *, story_run_id: str, version_id: str, reviewer_label: str, note: str | None) -> CommerceStoryboardVersion:
    story = _story_run(db, story_run_id); row = db.get(CommerceStoryboardVersion, version_id)
    if row is None or row.story_run_id != story.id or row.status != "READY": _error("导演分镜不存在或当前不可锁定", status.HTTP_422_UNPROCESSABLE_CONTENT)
    if db.get(CommerceCharacterDesignVersion, row.character_design_version_id).status != "LOCKED" or db.get(CommerceSceneDesignVersion, row.scene_design_version_id).status != "LOCKED": _error("导演分镜必须绑定锁定的角色与场景版本")
    db.execute(update(CommerceStoryboardVersion).where(CommerceStoryboardVersion.story_run_id == story.id, CommerceStoryboardVersion.status == "LOCKED").values(status="SUPERSEDED"))
    row.status = "LOCKED"; row.locked_at = utcnow(); _review(db, story_run=story, target_type="COMMERCE_STORYBOARD", target_id=row.id, decision="LOCKED", reviewer_label=reviewer_label, note=note); db.commit(); db.refresh(row); return row


def lock_image(db: Session, *, story_run_id: str, image_id: str, kind: str, reviewer_label: str, note: str | None):
    story = _story_run(db, story_run_id)
    model = {"CHARACTER": CommerceCharacterReferenceImage, "SCENE": CommerceSceneReferenceImage, "KEYFRAME": CommerceShotKeyframeVersion}.get(kind)
    if model is None: _error("未知图片类型", status.HTTP_422_UNPROCESSABLE_CONTENT)
    row = db.get(model, image_id)
    if row is None or row.story_run_id != story.id or row.status != "READY" or not row.image_url: _error("图片不存在、未生成或当前不可锁定", status.HTTP_422_UNPROCESSABLE_CONTENT)
    row.status = "LOCKED"; row.locked_at = utcnow(); _review(db, story_run=story, target_type=f"COMMERCE_{kind}_IMAGE", target_id=row.id, decision="LOCKED", reviewer_label=reviewer_label, note=note); db.commit(); db.refresh(row); return row


def review_video_clip(db: Session, *, story_run_id: str, clip_id: str, decision: str, reviewer_label: str, note: str | None) -> CommerceVideoClipVersion:
    story = _story_run(db, story_run_id); row = db.get(CommerceVideoClipVersion, clip_id)
    if row is None or row.story_run_id != story.id or row.status != "SUCCEEDED": _error("视频片段不存在、未成功或已经审核", status.HTTP_422_UNPROCESSABLE_CONTENT)
    accepted = decision.upper() == "APPROVED"
    if decision.upper() not in {"APPROVED", "REJECTED"}: _error("视频审核只支持 APPROVED 或 REJECTED", status.HTTP_422_UNPROCESSABLE_CONTENT)
    row.status = "APPROVED" if accepted else "REJECTED"; row.reviewed_at = utcnow(); row.review_note = note; _review(db, story_run=story, target_type="COMMERCE_VIDEO_CLIP", target_id=row.id, decision=row.status, reviewer_label=reviewer_label, note=note); db.commit(); db.refresh(row); return row


def list_story_run_assets(db: Session, story_run_id: str) -> dict[str, Any]:
    """给极简生产台提供版本清单；只读不做任何采用/状态推断。"""
    _story_run(db, story_run_id)
    return {
        "character_designs": list(db.scalars(select(CommerceCharacterDesignVersion).where(CommerceCharacterDesignVersion.story_run_id == story_run_id).order_by(CommerceCharacterDesignVersion.version.desc())).all()),
        "scene_designs": list(db.scalars(select(CommerceSceneDesignVersion).where(CommerceSceneDesignVersion.story_run_id == story_run_id).order_by(CommerceSceneDesignVersion.version.desc())).all()),
        "storyboards": list(db.scalars(select(CommerceStoryboardVersion).where(CommerceStoryboardVersion.story_run_id == story_run_id).order_by(CommerceStoryboardVersion.version.desc())).all()),
        "character_images": list(db.scalars(select(CommerceCharacterReferenceImage).where(CommerceCharacterReferenceImage.story_run_id == story_run_id).order_by(CommerceCharacterReferenceImage.created_at.desc())).all()),
        "scene_images": list(db.scalars(select(CommerceSceneReferenceImage).where(CommerceSceneReferenceImage.story_run_id == story_run_id).order_by(CommerceSceneReferenceImage.created_at.desc())).all()),
        "keyframes": list(db.scalars(select(CommerceShotKeyframeVersion).where(CommerceShotKeyframeVersion.story_run_id == story_run_id).order_by(CommerceShotKeyframeVersion.shot_number, CommerceShotKeyframeVersion.version.desc())).all()),
        "video_prompts": list(db.scalars(select(CommerceVideoPromptVersion).where(CommerceVideoPromptVersion.story_run_id == story_run_id).order_by(CommerceVideoPromptVersion.shot_number, CommerceVideoPromptVersion.version.desc())).all()),
        "clips": list(db.scalars(select(CommerceVideoClipVersion).where(CommerceVideoClipVersion.story_run_id == story_run_id).order_by(CommerceVideoClipVersion.shot_number, CommerceVideoClipVersion.version.desc())).all()),
        "finals": list(db.scalars(select(CommerceFinalVideo).where(CommerceFinalVideo.story_run_id == story_run_id).order_by(CommerceFinalVideo.version.desc())).all()),
    }
