"""Commerce 内部静态技术验收入口。

这个模块只服务单机部署下的受控冒烟：它把已验证的本地机器人图和一张已锁定的
场景图复制到 *重跑* StoryRun，并建立无需文本模型的最小角色、场景、分镜和视频
Prompt。它不是用户创作入口，所有内容都固定在代码中，避免接口接收任意路径、URL
或 Base64 后绕过正常媒体边界。

关键帧、视频创建、供应商轮询和媒体持久化仍完全复用 ``commerce_production_service``。
本模块从不创建 ``ModelInvocation``、供应商任务号或伪造模型输出。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    CommerceCharacterDesignVersion,
    CommerceCharacterReferenceImage,
    CommerceSceneDesignVersion,
    CommerceSceneReferenceImage,
    CommerceShotKeyframeVersion,
    CommerceStoryboardVersion,
    CommerceVideoClipVersion,
    CommerceVideoPromptVersion,
    CommerceWorkflowLink,
    ModelInvocation,
    OutlineVersionStatus,
    ProductAssetVersion,
    RunStatus,
    StoryOutlineVersion,
    StoryRun,
    StoryRunStage,
    StoryRunStatus,
    WorkflowRun,
    WorkflowStep,
)
from app.services.storage import local_asset_storage


INTERNAL_CONFIRMATION = "INTERNAL_SMOKE_ONLY"
FIXTURE_VERSION = "nonhuman_robot_reference_chain_v1"
ROBOT_SOURCE_URL = (
    "/media/generated/projects/seedream-smoke-20260815/single-image-smoke/"
    "dc6b5865b1dbbc23/v1-56f06ca3b7724c8887a74eb080145617.jpg"
)
ROBOT_SOURCE_SHA256 = "920667d735ef2e9b15a2090b5e4299fef55ce0d90c6e7a7fd5b6333b8078a5a5"
ROOM_SOURCE_ASSET_ID = "7e6f8ace-cfb3-435c-a806-52743d0b4ae4"
ROOM_SOURCE_SHA256 = "3790a3052379c079407f7b4801010ec8c50381d9528888d121d571e05b85ec10"
ROLE_ID = "internal-smoke-lemon-robot"
SCENE_ID = "internal-smoke-living-room"
SHOT_ID = "internal-smoke-robot-wave"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _error(detail: str, code: int = status.HTTP_409_CONFLICT) -> None:
    raise HTTPException(status_code=code, detail=detail)


def _next_version(db: Session, model: Any, *, story_run_id: str, extra_column: str | None = None, extra_value: str | None = None) -> int:
    statement = select(func.max(model.version)).where(model.story_run_id == story_run_id)
    if extra_column is not None:
        statement = statement.where(getattr(model, extra_column) == extra_value)
    return int(db.scalar(statement) or 0) + 1


def _marker(*, operation: str, story_run: StoryRun, source_story_run_id: str) -> dict[str, Any]:
    """构造可落库的静态来源标识，刻意不包含模型或媒体内容。"""

    return {
        "source": "internal_smoke_fixture",
        "execution_mode": "manual_static",
        "paid_model_call": False,
        "fixture_version": FIXTURE_VERSION,
        "operation": operation,
        "story_run_id": story_run.id,
        "source_story_run_id": source_story_run_id,
    }


def _is_fixture_payload(value: Any, *, operation: str | None = None) -> bool:
    if not isinstance(value, dict):
        return False
    marker = value.get("internal_smoke") if isinstance(value.get("internal_smoke"), dict) else value
    if marker.get("fixture_version") != FIXTURE_VERSION:
        return False
    return operation is None or marker.get("operation") == operation


def _source_story_run_id(story_run: StoryRun) -> str:
    snapshot = story_run.mainline_input.input_snapshot if story_run.mainline_input else {}
    rerun = snapshot.get("rerun") if isinstance(snapshot, dict) else None
    source_id = rerun.get("source_story_run_id") if isinstance(rerun, dict) else None
    if not isinstance(source_id, str) or not source_id:
        _error("内部静态验收只允许从已选创意的重跑 StoryRun 发起")
    return source_id


def _assert_confirmation(confirm: str) -> None:
    if confirm != INTERNAL_CONFIRMATION:
        _error("内部静态验收必须显式确认 INTERNAL_SMOKE_ONLY", status.HTTP_422_UNPROCESSABLE_CONTENT)


def _production_runs_for_story_run(db: Session, story_run_id: str, project_id: str) -> list[WorkflowRun]:
    """JSON 快照是历史兼容字段，因此只读扫描，不依赖特定数据库 JSON 运算符。"""

    runs: list[WorkflowRun] = []
    for row in db.scalars(select(WorkflowRun).where(WorkflowRun.project_id == project_id)).all():
        snapshot = row.input_snapshot or {}
        context = snapshot.get("commerce_production") if isinstance(snapshot, dict) else None
        if isinstance(context, dict) and context.get("story_run_id") == story_run_id:
            runs.append(row)
    return runs


def _assert_eligible_empty_rerun(db: Session, story_run: StoryRun, *, source_story_run_id: str) -> None:
    if story_run.run_number <= 1:
        _error("内部静态验收只能用于 run_number 大于 1 的隔离重跑 StoryRun")
    if story_run.state.status in {StoryRunStatus.CANCELLED, StoryRunStatus.COMPLETED}:
        _error("已取消或已完成的 StoryRun 不能写入内部静态验收资产")
    source = db.get(StoryRun, source_story_run_id)
    if source is None or source.id == story_run.id or source.project_id != story_run.project_id:
        _error("内部静态验收来源 StoryRun 无效或不属于同一项目")

    artifact_models = (
        StoryOutlineVersion,
        CommerceCharacterDesignVersion,
        CommerceSceneDesignVersion,
        CommerceStoryboardVersion,
        CommerceCharacterReferenceImage,
        CommerceSceneReferenceImage,
        CommerceShotKeyframeVersion,
        CommerceVideoPromptVersion,
        CommerceVideoClipVersion,
    )
    for model in artifact_models:
        if db.scalar(select(func.count(model.id)).where(model.story_run_id == story_run.id)):
            _error("隔离 StoryRun 已存在生产资产，不能覆盖或混入内部静态验收结果")

    parent_link = db.scalar(select(CommerceWorkflowLink).where(CommerceWorkflowLink.story_run_id == story_run.id))
    if parent_link is not None and db.scalar(
        select(func.count(ModelInvocation.id)).where(ModelInvocation.workflow_run_id == parent_link.workflow_run_id)
    ):
        _error("隔离 StoryRun 的父工作流已产生模型调用，不能再写入内部静态验收资产")
    for run in _production_runs_for_story_run(db, story_run.id, story_run.project_id):
        if db.scalar(select(func.count(ModelInvocation.id)).where(ModelInvocation.workflow_run_id == run.id)):
            _error("隔离 StoryRun 已产生模型调用，不能再写入内部静态验收资产")
        _error("隔离 StoryRun 已存在生产任务，不能混入内部静态验收资产")


def _existing_bootstrap(db: Session, story_run: StoryRun) -> dict[str, Any] | None:
    outline = db.scalars(
        select(StoryOutlineVersion)
        .where(StoryOutlineVersion.story_run_id == story_run.id)
        .order_by(StoryOutlineVersion.version.desc())
    ).first()
    if outline is None or not _is_fixture_payload(outline.product_placement_strategy, operation="bootstrap"):
        return None
    character = db.scalars(
        select(CommerceCharacterDesignVersion)
        .where(CommerceCharacterDesignVersion.story_run_id == story_run.id, CommerceCharacterDesignVersion.status == "LOCKED")
        .order_by(CommerceCharacterDesignVersion.version.desc())
    ).first()
    scene = db.scalars(
        select(CommerceSceneDesignVersion)
        .where(CommerceSceneDesignVersion.story_run_id == story_run.id, CommerceSceneDesignVersion.status == "LOCKED")
        .order_by(CommerceSceneDesignVersion.version.desc())
    ).first()
    storyboard = db.scalars(
        select(CommerceStoryboardVersion)
        .where(CommerceStoryboardVersion.story_run_id == story_run.id, CommerceStoryboardVersion.status == "LOCKED")
        .order_by(CommerceStoryboardVersion.version.desc())
    ).first()
    if not all((character, scene, storyboard)):
        _error("内部静态验收发现不完整的 bootstrap 记录，拒绝覆盖", status.HTTP_409_CONFLICT)
    character_image = db.scalars(
        select(CommerceCharacterReferenceImage).where(
            CommerceCharacterReferenceImage.story_run_id == story_run.id,
            CommerceCharacterReferenceImage.character_design_version_id == character.id,
            CommerceCharacterReferenceImage.role_id == ROLE_ID,
            CommerceCharacterReferenceImage.status == "LOCKED",
        )
    ).first()
    scene_image = db.scalars(
        select(CommerceSceneReferenceImage).where(
            CommerceSceneReferenceImage.story_run_id == story_run.id,
            CommerceSceneReferenceImage.scene_design_version_id == scene.id,
            CommerceSceneReferenceImage.scene_id == SCENE_ID,
            CommerceSceneReferenceImage.status == "LOCKED",
        )
    ).first()
    if character_image is None or scene_image is None:
        _error("内部静态验收发现不完整的受控参考图记录，拒绝覆盖", status.HTTP_409_CONFLICT)
    audit = next(
        (
            row
            for row in db.scalars(
                select(WorkflowRun)
                .where(WorkflowRun.project_id == story_run.project_id, WorkflowRun.workflow_key == "commerce_internal_smoke_bootstrap")
                .order_by(WorkflowRun.created_at.desc())
            ).all()
            if _is_fixture_payload((row.input_snapshot or {}).get("internal_smoke"), operation="bootstrap")
            and ((row.input_snapshot or {}).get("internal_smoke") or {}).get("story_run_id") == story_run.id
        ),
        None,
    )
    return {
        "workflow_run_id": audit.id if audit else None,
        "workflow_step_id": audit.steps[0].id if audit and audit.steps else None,
        "outline_id": outline.id,
        "character_design_id": character.id,
        "scene_design_id": scene.id,
        "storyboard_id": storyboard.id,
        "character_image_id": character_image.id,
        "scene_image_id": scene_image.id,
        "shot_id": SHOT_ID,
        "current_stage": story_run.state.current_stage.value,
        "current_status": story_run.state.status.value,
        "idempotent": True,
    }


def _create_static_audit(
    db: Session,
    *,
    story_run: StoryRun,
    source_story_run_id: str,
    operation: str,
    output: dict[str, Any],
) -> tuple[WorkflowRun, WorkflowStep]:
    marker = _marker(operation=operation, story_run=story_run, source_story_run_id=source_story_run_id)
    parent = db.scalar(select(CommerceWorkflowLink).where(CommerceWorkflowLink.story_run_id == story_run.id))
    parent_run = db.get(WorkflowRun, parent.workflow_run_id) if parent else None
    run = WorkflowRun(
        project_id=story_run.project_id,
        workflow_key=f"commerce_internal_smoke_{operation}",
        workflow_definition_id=parent_run.workflow_definition_id if parent_run else None,
        workflow_version=parent_run.workflow_version if parent_run else None,
        idempotency_key=f"internal-smoke:{story_run.id}:{operation}:{FIXTURE_VERSION}",
        input_snapshot={"internal_smoke": marker},
        status=RunStatus.SUCCEEDED,
        started_at=_utcnow(),
        finished_at=_utcnow(),
    )
    step = WorkflowStep(
        workflow_run=run,
        step_key=f"COMMERCE_INTERNAL_SMOKE_{operation.upper()}",
        position=1,
        attempt=1,
        status=RunStatus.SUCCEEDED,
        progress=100,
        input_payload={"internal_smoke": deepcopy(marker)},
        output_payload={"internal_smoke": deepcopy(marker), "artifact_references": deepcopy(output)},
        idempotency_key=f"internal-smoke-step:{story_run.id}:{operation}:{FIXTURE_VERSION}",
        started_at=run.started_at,
        finished_at=run.finished_at,
    )
    db.add_all((run, step))
    db.flush()
    return run, step


def _unlink_generated_media(media_url: str) -> None:
    try:
        local_asset_storage.generated_media_path(media_url).unlink(missing_ok=True)
    except RuntimeError:
        return


def _static_video_prompt_response(
    db: Session, *, story_run: StoryRun, prompt: CommerceVideoPromptVersion, keyframe_id: str, idempotent: bool
) -> dict[str, Any]:
    """以相同格式返回新建或已存在的静态视频 Prompt。

    审计步骤是内部业务操作记录，而非模型执行记录。重复请求仍应能追溯到同一条
    审计步骤，不能因为幂等返回而丢失这个关联。
    """

    audit = db.get(WorkflowRun, prompt.workflow_run_id) if prompt.workflow_run_id else None
    audit_step = audit.steps[0] if audit and audit.steps else None
    return {
        "workflow_run_id": audit.id if audit else prompt.workflow_run_id,
        "workflow_step_id": audit_step.id if audit_step else None,
        "video_prompt_id": prompt.id,
        "keyframe_id": keyframe_id,
        "shot_id": SHOT_ID,
        "current_stage": story_run.state.current_stage.value,
        "current_status": story_run.state.status.value,
        "idempotent": idempotent,
    }


def bootstrap_internal_smoke(db: Session, *, story_run_id: str, confirm: str) -> dict[str, Any]:
    """原子建立固定的非真人角色、客厅和单镜头关键帧输入。"""

    _assert_confirmation(confirm)
    story_run = db.get(StoryRun, story_run_id)
    if story_run is None or story_run.mainline_input is None:
        _error("StoryRun 不存在或缺少冻结主线输入", status.HTTP_404_NOT_FOUND)
    source_story_run_id = _source_story_run_id(story_run)
    if story_run.run_number <= 1 or story_run.state.status in {StoryRunStatus.CANCELLED, StoryRunStatus.COMPLETED}:
        _error("当前 StoryRun 不允许写入内部静态验收资产")
    existing = _existing_bootstrap(db, story_run)
    if existing is not None:
        return existing
    _assert_eligible_empty_rerun(db, story_run, source_story_run_id=source_story_run_id)

    product = db.get(ProductAssetVersion, story_run.product_asset_version_id)
    if product is None or product.status.value != "CONFIRMED" or product.frozen_at is None:
        _error("隔离 StoryRun 的冻结商品版本无效", status.HTTP_422_UNPROCESSABLE_CONTENT)
    source_scene = db.get(CommerceSceneReferenceImage, ROOM_SOURCE_ASSET_ID)
    if source_scene is None or source_scene.story_run_id != source_story_run_id or source_scene.status != "LOCKED" or not source_scene.image_url:
        _error("指定客厅场景来源不存在、未锁定或不属于来源 StoryRun", status.HTTP_422_UNPROCESSABLE_CONTENT)

    robot_url: str | None = None
    scene_url: str | None = None
    try:
        robot_url, robot_meta = local_asset_storage.clone_generated_image(
            source_media_url=ROBOT_SOURCE_URL,
            target_project_id=story_run.project_id,
            asset_kind="commerce-character",
            asset_id=ROLE_ID,
            version=1,
        )
        if robot_meta["sha256"] != ROBOT_SOURCE_SHA256 or robot_meta["byte_size"] != 330372 or (robot_meta["width"], robot_meta["height"]) != (2048, 2048):
            _error("受控机器人源图指纹不符合内部验收固定输入", status.HTTP_422_UNPROCESSABLE_CONTENT)
        scene_url, scene_meta = local_asset_storage.clone_generated_image(
            source_media_url=source_scene.image_url,
            target_project_id=story_run.project_id,
            asset_kind="commerce-scene",
            asset_id=SCENE_ID,
            version=1,
        )
        if scene_meta["sha256"] != ROOM_SOURCE_SHA256:
            _error("受控客厅场景源图指纹不符合内部验收固定输入", status.HTTP_422_UNPROCESSABLE_CONTENT)

        marker = _marker(operation="bootstrap", story_run=story_run, source_story_run_id=source_story_run_id)
        outline = StoryOutlineVersion(
            story_run_id=story_run.id,
            version=_next_version(db, StoryOutlineVersion, story_run_id=story_run.id),
            title="内部技术验收：柠檬机器人客厅挥手",
            premise="明确非人类的柠檬机器人在已锁定现代客厅中挥手，仅用于验证图生视频技术链。",
            story_beats=[{"position": 1, "summary": "机器人在客厅中央面对镜头抬起机械手臂。"}],
            product_placement_strategy={"internal_smoke": marker, "nodes": []},
            status=OutlineVersionStatus.LOCKED,
        )
        db.add(outline)
        db.flush()
        character = CommerceCharacterDesignVersion(
            story_run_id=story_run.id,
            source_outline_version_id=outline.id,
            source_product_asset_version_id=story_run.product_asset_version_id,
            version=_next_version(db, CommerceCharacterDesignVersion, story_run_id=story_run.id),
            status="LOCKED",
            content={"roles": [{
                "role_id": ROLE_ID,
                "name": "柠檬机器人",
                "age_range": "非人类",
                "gender": "非人类",
                "identity_and_occupation": "小型机械角色",
                "personality": "友好、好奇",
                "dramatic_function": "内部技术验收主体",
                "relationships": [],
                "appearance": "柠檬黄色外壳、机械手臂和机械腿、电子显示屏表情",
                "hairstyle": "无",
                "costume": "固定机械外壳",
                "fixed_visual_features": ["柠檬黄色", "机械结构", "电子屏幕表情"],
                "immutable_features": ["非真人", "不得有人类皮肤", "不得出现真人五官"],
                "product_relationship": "无商品关系，仅内部技术验收",
                "buyer": False,
                "user": False,
                "decision_influencer": False,
                "image_prompt": "使用已验证的非真人机器人参考图，不重新生成人物。",
            }]},
            input_snapshot={"internal_smoke": marker, "source_outline_version_id": outline.id},
            prompt_snapshot={"internal_smoke": marker},
            raw_response={},
            locked_at=_utcnow(),
        )
        db.add(character)
        db.flush()
        scene = CommerceSceneDesignVersion(
            story_run_id=story_run.id,
            source_outline_version_id=outline.id,
            character_design_version_id=character.id,
            source_product_asset_version_id=story_run.product_asset_version_id,
            version=_next_version(db, CommerceSceneDesignVersion, story_run_id=story_run.id),
            status="LOCKED",
            content={"scenes": [{
                "scene_id": SCENE_ID,
                "name": "现代客厅",
                "purpose": "内部技术验收背景",
                "time": "傍晚",
                "location": "现代城市公寓客厅",
                "lighting": "右侧暖色自然光",
                "color_tone": "暖色、自然",
                "spatial_layout": "浅色沙发、木茶几和落地窗保持固定位置",
                "fixed_props": ["浅色沙发", "木茶几", "落地窗"],
                "product_position": "无",
                "product_usage_environment": "无",
                "continuity_requirements": ["保持沙发、茶几、窗户和傍晚暖光一致"],
                "immutable_features": ["不增加真人或其他角色"],
                "base_image_prompt": "使用已锁定客厅参考图，不重新生成场景。",
            }]},
            input_snapshot={"internal_smoke": marker, "character_design_version_id": character.id},
            prompt_snapshot={"internal_smoke": marker},
            raw_response={},
            locked_at=_utcnow(),
        )
        db.add(scene)
        db.flush()
        character_image = CommerceCharacterReferenceImage(
            story_run_id=story_run.id,
            character_design_version_id=character.id,
            role_id=ROLE_ID,
            version=1,
            image_url=robot_url,
            prompt_snapshot="内部静态验收采用既有非真人柠檬机器人受控参考图。",
            input_snapshot={"internal_smoke": marker, "asset": robot_meta},
            status="LOCKED",
            locked_at=_utcnow(),
        )
        scene_image = CommerceSceneReferenceImage(
            story_run_id=story_run.id,
            scene_design_version_id=scene.id,
            scene_id=SCENE_ID,
            version=1,
            image_url=scene_url,
            prompt_snapshot="内部静态验收采用既有已锁定客厅受控参考图。",
            input_snapshot={"internal_smoke": marker, "asset": scene_meta, "source_asset_id": ROOM_SOURCE_ASSET_ID},
            status="LOCKED",
            locked_at=_utcnow(),
        )
        db.add_all((character_image, scene_image))
        db.flush()
        shot = {
            "shot_id": SHOT_ID,
            "shot_number": 1,
            "segment_summary": "柠檬机器人在客厅中央抬起机械手臂挥手。",
            "story_paragraph": "内部技术验收单镜头。",
            "duration_ms": 5000,
            "character_ids": [ROLE_ID],
            "scene_id": SCENE_ID,
            "product_integration_node_id": None,
            "product_visible": False,
            "shot_scale": "中景",
            "camera_position": "平视",
            "camera_move": "极轻微向前推进",
            "composition": "机器人居中，客厅结构可见",
            "action": "机器人抬起一只机械手臂准备挥手",
            "expression": "电子屏幕表情友好",
            "dialogue": "",
            "narration": "",
            "product_position": "无",
            "product_action": "无",
            "product_exposure_ms": 0,
            "previous_continuity_state": "单镜头起始",
            "ending_continuity_state": "机器人挥手动作开始",
            "next_transition_requirement": "无",
            "keyframe_prompt": "一个明确非人类的柠檬造型小机器人站在现代客厅中央，保持参考图中的柠檬黄色外壳、机械结构、电子屏幕表情和整体比例。机器人面对镜头，抬起一只机械手臂准备挥手。保留场景参考图中的浅色沙发、木茶几、落地窗和傍晚暖光，电影感构图，适合作为图生视频首帧。\n\n不要真人，不要人类面孔，不要人类皮肤，不要真人五官，不要照片级人物，不要把机器人拟人化成真实儿童或成年人，不要增加其他人物。",
            "video_prompt": "柠檬造型小机器人站在客厅中央，先看向镜头，然后缓慢抬起机械手臂友好挥手，电子屏幕表情轻微变化。窗外傍晚暖光缓慢移动，镜头做非常轻微的向前推进。保持机器人机械结构、客厅陈设和整体画面一致，不出现任何真人或新增角色。",
            "forbidden_content": ["真人", "人类面孔", "人类皮肤", "真实儿童", "真实成年人", "其他人物"],
        }
        storyboard = CommerceStoryboardVersion(
            story_run_id=story_run.id,
            source_outline_version_id=outline.id,
            character_design_version_id=character.id,
            scene_design_version_id=scene.id,
            source_product_asset_version_id=story_run.product_asset_version_id,
            version=_next_version(db, CommerceStoryboardVersion, story_run_id=story_run.id),
            status="LOCKED",
            content={"shots": [shot], "product_integration_nodes": [], "internal_smoke": marker},
            input_snapshot={"internal_smoke": marker, "outline_id": outline.id, "character_design_id": character.id, "scene_design_id": scene.id},
            prompt_snapshot={"internal_smoke": marker},
            raw_response={},
            locked_at=_utcnow(),
        )
        db.add(storyboard)
        db.flush()
        audit_output = {
            "outline_id": outline.id,
            "character_design_id": character.id,
            "scene_design_id": scene.id,
            "character_image_id": character_image.id,
            "scene_image_id": scene_image.id,
            "storyboard_id": storyboard.id,
            "shot_id": SHOT_ID,
        }
        audit_run, audit_step = _create_static_audit(
            db, story_run=story_run, source_story_run_id=source_story_run_id, operation="bootstrap", output=audit_output
        )
        # 商业主工作流不替代这个静态审计；这里只把当前隔离 Run 安全推进到已具备
        # 锁定角色、场景、分镜和双参考图的关键帧阶段。
        story_run.state.current_stage = StoryRunStage.VISUAL_ASSETS
        story_run.state.status = StoryRunStatus.PENDING
        story_run.state.stage_data = {"internal_smoke": marker, "bootstrap_workflow_run_id": audit_run.id}
        db.commit()
        return {
            "workflow_run_id": audit_run.id,
            "workflow_step_id": audit_step.id,
            **audit_output,
            "current_stage": story_run.state.current_stage.value,
            "current_status": story_run.state.status.value,
            "idempotent": False,
        }
    except RuntimeError as exc:
        db.rollback()
        if robot_url:
            _unlink_generated_media(robot_url)
        if scene_url:
            _unlink_generated_media(scene_url)
        _error(f"内部静态验收受控媒体校验失败：{exc}", status.HTTP_422_UNPROCESSABLE_CONTENT)
    except IntegrityError:
        # PostgreSQL 会用唯一约束裁决两个同时 bootstrap 的请求。第二个请求回滚
        # 自己的媒体副本后，只要第一笔事务已完整落库，就返回同一静态结果；绝不
        # 通过第二次写入覆盖资产或制造第二个审计步骤。
        db.rollback()
        if robot_url:
            _unlink_generated_media(robot_url)
        if scene_url:
            _unlink_generated_media(scene_url)
        current = db.get(StoryRun, story_run_id)
        if current is not None:
            existing = _existing_bootstrap(db, current)
            if existing is not None:
                return existing
        _error("内部静态验收 bootstrap 发生并发冲突，未覆盖已有记录")
    except Exception:
        db.rollback()
        if robot_url:
            _unlink_generated_media(robot_url)
        if scene_url:
            _unlink_generated_media(scene_url)
        raise


def create_internal_smoke_video_prompt(
    db: Session, *, story_run_id: str, keyframe_id: str, confirm: str
) -> dict[str, Any]:
    """在审核锁定后的内部关键帧上追加静态视频 Prompt，不调用导演模型。"""

    _assert_confirmation(confirm)
    story_run = db.get(StoryRun, story_run_id)
    if story_run is None or story_run.mainline_input is None:
        _error("StoryRun 不存在或缺少冻结主线输入", status.HTTP_404_NOT_FOUND)
    source_story_run_id = _source_story_run_id(story_run)
    if story_run.run_number <= 1 or story_run.state.status in {StoryRunStatus.CANCELLED, StoryRunStatus.COMPLETED}:
        _error("当前 StoryRun 不允许创建内部静态视频 Prompt")
    source = db.get(StoryRun, source_story_run_id)
    if source is None or source.id == story_run.id or source.project_id != story_run.project_id:
        _error("内部静态验收来源 StoryRun 无效或不属于同一项目")
    storyboard = db.scalars(
        select(CommerceStoryboardVersion).where(
            CommerceStoryboardVersion.story_run_id == story_run.id,
            CommerceStoryboardVersion.status == "LOCKED",
        ).order_by(CommerceStoryboardVersion.version.desc())
    ).first()
    if storyboard is None or not _is_fixture_payload(storyboard.content, operation="bootstrap"):
        _error("当前 StoryRun 没有已锁定的内部静态验收分镜")
    keyframe = db.get(CommerceShotKeyframeVersion, keyframe_id)
    if (
        keyframe is None
        or keyframe.story_run_id != story_run.id
        or keyframe.storyboard_version_id != storyboard.id
        or keyframe.shot_id != SHOT_ID
        or keyframe.status != "LOCKED"
        or not keyframe.image_url
    ):
        _error("必须先锁定当前内部技术镜头的关键帧", status.HTTP_422_UNPROCESSABLE_CONTENT)
    existing = db.scalars(
        select(CommerceVideoPromptVersion).where(
            CommerceVideoPromptVersion.story_run_id == story_run.id,
            CommerceVideoPromptVersion.storyboard_version_id == storyboard.id,
            CommerceVideoPromptVersion.shot_id == SHOT_ID,
            CommerceVideoPromptVersion.keyframe_version_id == keyframe.id,
        ).order_by(CommerceVideoPromptVersion.version.desc())
    ).first()
    if existing is not None:
        if _is_fixture_payload(existing.trace, operation="video_prompt") and existing.status == "LOCKED":
            return _static_video_prompt_response(
                db, story_run=story_run, prompt=existing, keyframe_id=keyframe.id, idempotent=True
            )
        _error("该内部镜头已有冲突的视频 Prompt，不能覆盖", status.HTTP_409_CONFLICT)
    try:
        marker = _marker(operation="video_prompt", story_run=story_run, source_story_run_id=source_story_run_id)
        prompt = CommerceVideoPromptVersion(
            story_run_id=story_run.id,
            storyboard_version_id=storyboard.id,
            shot_id=SHOT_ID,
            shot_number=1,
            keyframe_version_id=keyframe.id,
            version=_next_version(db, CommerceVideoPromptVersion, story_run_id=story_run.id, extra_column="shot_id", extra_value=SHOT_ID),
            prompt="柠檬造型小机器人站在客厅中央，先看向镜头，然后缓慢抬起机械手臂友好挥手，电子屏幕表情轻微变化。窗外傍晚暖光缓慢移动，镜头做非常轻微的向前推进。保持机器人机械结构、客厅陈设和整体画面一致，不出现任何真人或新增角色。",
            trace={"internal_smoke": marker, "keyframe_id": keyframe.id, "keyframe_assets": deepcopy(keyframe.input_asset_snapshot)},
            status="LOCKED",
            locked_at=_utcnow(),
        )
        db.add(prompt)
        db.flush()
        audit_run, audit_step = _create_static_audit(
            db,
            story_run=story_run,
            source_story_run_id=source_story_run_id,
            operation="video_prompt",
            output={"video_prompt_id": prompt.id, "keyframe_id": keyframe.id, "shot_id": SHOT_ID},
        )
        prompt.workflow_run_id = audit_run.id
        story_run.state.current_stage = StoryRunStage.SEGMENT_RENDER
        story_run.state.status = StoryRunStatus.PENDING
        story_run.state.stage_data = {"internal_smoke": marker, "video_prompt_workflow_run_id": audit_run.id}
        db.commit()
        return _static_video_prompt_response(
            db, story_run=story_run, prompt=prompt, keyframe_id=keyframe.id, idempotent=False
        )
    except IntegrityError:
        # 同一已锁定关键帧的重复请求只允许得到同一个 Prompt/审计记录；不创建
        # 第二个版本，更不能以自动重试形式触发后续付费视频任务。
        db.rollback()
        current = db.get(StoryRun, story_run_id)
        if current is not None:
            existing = db.scalars(
                select(CommerceVideoPromptVersion)
                .where(
                    CommerceVideoPromptVersion.story_run_id == current.id,
                    CommerceVideoPromptVersion.shot_id == SHOT_ID,
                    CommerceVideoPromptVersion.keyframe_version_id == keyframe_id,
                )
                .order_by(CommerceVideoPromptVersion.version.desc())
            ).first()
            if existing is not None and _is_fixture_payload(existing.trace, operation="video_prompt") and existing.status == "LOCKED":
                return _static_video_prompt_response(
                    db, story_run=current, prompt=existing, keyframe_id=keyframe_id, idempotent=True
                )
        _error("内部静态视频 Prompt 发生并发冲突，未覆盖已有记录")
    except Exception:
        db.rollback()
        raise
