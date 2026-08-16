"""Commerce Slice 1：V1 参考分析到带货 StoryRun 的最小正式主链。

本服务不创建新的调度体系：视频分析和十创意生成仍是 V1 ``WorkflowRun``，故事
大纲仍进入既有 Commerce ``StoryRun → WorkflowRun → WorkflowStep``。这里仅把每一
段输入转换为可冻结、可审核、可追溯的版本资产，避免执行 Worker 时读取“最新商品”。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CommerceCreativeBatch,
    CommerceCreativeBatchStatus,
    CommerceCreativeIdea,
    CommerceCreativeIdeaStatus,
    CommerceReferenceIntake,
    CommerceStoryRunInput,
    ModelInvocation,
    ProductAnalysisStatus,
    ProductAnalysisVersion,
    ProductAsset,
    ProductAssetVersion,
    ProductAssetVersionStatus,
    ProjectProductSelection,
    ProjectProductionState,
    ReferenceAnalysis,
    ReviewDecision,
    ReviewStatus,
    RunStatus,
    ScriptAnalysisStatus,
    ScriptAnalysisVersion,
    ScriptAsset,
    StoryRun,
    StoryRunMode,
    StoryRunStage,
    TopicCandidate,
    TopicStatus,
    WorkflowRun,
)
from app.services.commerce_domain_service import (
    create_next_product_asset_version,
    create_next_script_analysis_version,
    create_project_product_selection,
    create_story_run,
    freeze_product_asset_version,
    transition_product_asset_version_status,
)
from app.services.v1_model_adapter_service import assert_supported, generate_structured_text, is_mock_adapter


IDEA_COUNT = 10


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _error(detail: str, code: int = status.HTTP_409_CONFLICT) -> None:
    raise HTTPException(status_code=code, detail=detail)


def _enum(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _analysis_snapshot(analysis: ReferenceAnalysis) -> dict[str, Any]:
    """只复制已保存的分析结果；不从项目指针或最新素材重新推导。"""

    return {
        "id": analysis.id,
        "version": analysis.version,
        "video_script_structure": deepcopy(analysis.video_script_structure),
        "opening_analysis": deepcopy(analysis.opening_analysis),
        "viral_elements": deepcopy(analysis.viral_elements),
        "scene_analysis": deepcopy(analysis.scene_analysis),
        "creative_brief": deepcopy(analysis.creative_brief),
        "locked_snapshot": deepcopy(analysis.locked_snapshot),
    }


def _script_snapshot(script: ScriptAnalysisVersion) -> dict[str, Any]:
    return {
        "id": script.id,
        "script_asset_id": script.script_asset_id,
        "version": script.version,
        "timeline_transcript": deepcopy(script.timeline_transcript),
        "story_beats": deepcopy(script.story_beats),
        "role_archetypes": deepcopy(script.role_archetypes),
        "conflicts": deepcopy(script.conflicts),
        "turning_points": deepcopy(script.turning_points),
        "emotional_curve": deepcopy(script.emotional_curve),
        "chapter_candidates": deepcopy(script.chapter_candidates),
        "product_slot_candidates": deepcopy(script.product_slot_candidates),
        "narrative_function_sequence": deepcopy(script.narrative_function_sequence),
        "raw_analysis": deepcopy(script.raw_analysis),
    }


def _product_snapshot(product: ProductAssetVersion) -> dict[str, Any]:
    return {
        "id": product.id,
        "product_asset_id": product.product_asset_id,
        "source_analysis_version_id": product.source_analysis_version_id,
        "version": product.version,
        "product_name": product.product_name,
        "appearance_description": product.appearance_description,
        "selling_points": deepcopy(product.selling_points),
        "user_pain_points": deepcopy(product.user_pain_points),
        "usage_scenarios": deepcopy(product.usage_scenarios),
        "package_ocr": deepcopy(product.package_ocr),
        "reference_images": deepcopy(product.reference_images),
        "status": _enum(product.status),
        "frozen_at": product.frozen_at.isoformat() if product.frozen_at else None,
    }


def _source_asset_id(db: Session, analysis: ReferenceAnalysis) -> str:
    run = db.get(WorkflowRun, analysis.workflow_run_id)
    source_id = ((run.input_snapshot or {}).get("context") or {}).get("source_asset_id") if run else None
    if not isinstance(source_id, str) or not source_id:
        raise RuntimeError("参考分析缺少创建时冻结的来源视频")
    return source_id


def ensure_reference_intake_from_analysis(db: Session, analysis: ReferenceAnalysis) -> CommerceReferenceIntake:
    """把一次成功 V1 分析落为 ScriptAnalysis + 商品草稿，且可安全重复执行。

    Worker 只使用 ``analysis.workflow_run_id`` 内已经冻结的 source id，绝不按上传
    时间读取当前素材。商品草稿不猜测功效，明确要求制作人确认后才能被创意任务采用。
    """

    existing = db.scalars(
        select(CommerceReferenceIntake).where(CommerceReferenceIntake.reference_analysis_id == analysis.id)
    ).first()
    if existing is not None:
        return existing
    if analysis.generation_status != RunStatus.SUCCEEDED:
        raise RuntimeError("只有成功的参考分析可以生成 Commerce 输入资产")
    source_asset_id = _source_asset_id(db, analysis)
    script_asset = db.scalars(select(ScriptAsset).where(ScriptAsset.media_asset_id == source_asset_id)).first()
    if script_asset is None:
        script_asset = ScriptAsset(
            project_id=analysis.project_id,
            media_asset_id=source_asset_id,
            name=f"参考视频脚本资产 · 分析 v{analysis.version}",
        )
        db.add(script_asset)
        db.flush()

    structure = deepcopy(analysis.video_script_structure or {})
    opening = deepcopy(analysis.opening_analysis or {})
    viral_elements = deepcopy(analysis.viral_elements or [])
    scene_analysis = deepcopy(analysis.scene_analysis or [])
    creative_brief = deepcopy(analysis.creative_brief or {})
    script = create_next_script_analysis_version(
        db,
        script_asset_id=script_asset.id,
        timeline_transcript=[
            {"time_range": opening.get("time_window", "前 3-10 秒"), "summary": opening.get("mechanism", "待人工核对")}
        ],
        story_beats=[{"source": "video_script_structure", "value": structure}],
        role_archetypes=[],
        conflicts=[item for item in viral_elements if isinstance(item, dict) and item.get("type") == "conflict"],
        turning_points=[{"source": "structure", "value": item} for item in (structure.get("structure") or [])],
        emotional_curve=[item for item in viral_elements if isinstance(item, dict) and item.get("type") == "emotion"],
        chapter_candidates=[{"source": "scene_analysis", "value": item} for item in scene_analysis if isinstance(item, dict)],
        product_slot_candidates=[],
        narrative_function_sequence=[{"source": "viral_element", "value": item} for item in viral_elements if isinstance(item, dict)],
        raw_analysis={"reference_analysis": _analysis_snapshot(analysis), "creative_brief": creative_brief},
        analysis_status=ScriptAnalysisStatus.SUCCEEDED,
    )

    product_asset = ProductAsset(
        name=f"待确认商品 · 参考分析 v{analysis.version}",
        description="由参考视频分析自动创建的商品草稿；必须人工核对和冻结后才能用于带货创作。",
    )
    db.add(product_asset)
    db.flush()
    product_analysis = ProductAnalysisVersion(
        product_asset_id=product_asset.id,
        source_media_asset_id=source_asset_id,
        version=1,
        product_identification={"name": "待确认商品", "source": "reference_analysis"},
        package_ocr={},
        candidate_reference_images=[],
        appearance_description_candidates=[],
        selling_point_candidates=[],
        user_pain_point_candidates=[],
        usage_scenario_candidates=[],
        raw_analysis={"reference_analysis_id": analysis.id, "analysis": _analysis_snapshot(analysis)},
        analysis_status=ProductAnalysisStatus.SUCCEEDED,
    )
    db.add(product_analysis)
    db.flush()
    product_version = create_next_product_asset_version(
        db,
        product_asset_id=product_asset.id,
        source_analysis_version_id=product_analysis.id,
        product_name="待确认商品",
        appearance_description="请根据参考视频中的真实商品外观人工确认；系统不会自动补造包装、功效或使用方法。",
        selling_points=[],
        user_pain_points=[],
        usage_scenarios=[],
        package_ocr={},
        reference_images=[],
    )
    intake = CommerceReferenceIntake(
        project_id=analysis.project_id,
        reference_analysis_id=analysis.id,
        script_asset_id=script_asset.id,
        script_analysis_version_id=script.id,
        product_asset_id=product_asset.id,
        product_analysis_version_id=product_analysis.id,
        product_asset_version_id=product_version.id,
        input_snapshot={
            "source_asset_id": source_asset_id,
            "reference_analysis": _analysis_snapshot(analysis),
            "script_analysis": _script_snapshot(script),
            "product_draft": _product_snapshot(product_version),
        },
    )
    db.add(intake)
    db.flush()
    return intake


def list_reference_intakes(db: Session, project_id: str) -> list[CommerceReferenceIntake]:
    return list(
        db.scalars(
            select(CommerceReferenceIntake)
            .where(CommerceReferenceIntake.project_id == project_id)
            .order_by(CommerceReferenceIntake.created_at.desc())
        ).all()
    )


def confirm_and_freeze_product_draft(
    db: Session,
    *,
    intake_id: str,
    reviewer_label: str | None,
    note: str | None,
    changes: dict[str, Any] | None = None,
) -> CommerceReferenceIntake:
    """确认草稿并冻结具体产品版本，之后仅该版本可进入创意快照。"""

    intake = db.get(CommerceReferenceIntake, intake_id)
    if intake is None:
        _error("商品草稿不存在", status.HTTP_404_NOT_FOUND)
    analysis = db.get(ReferenceAnalysis, intake.reference_analysis_id)
    state = db.get(ProjectProductionState, intake.project_id)
    if analysis is None or state is None or analysis.review_status != ReviewStatus.LOCKED or state.locked_reference_analysis_id != analysis.id:
        _error("请先锁定与该商品草稿对应的参考视频分析")
    product = db.get(ProductAssetVersion, intake.product_asset_version_id)
    if product is None:
        raise RuntimeError("商品草稿缺少产品版本")
    if product.status == ProductAssetVersionStatus.DRAFT:
        permitted = {
            "product_name", "appearance_description", "selling_points", "user_pain_points",
            "usage_scenarios", "package_ocr", "reference_images",
        }
        for key, value in (changes or {}).items():
            if key not in permitted:
                _error(f"商品确认不支持字段：{key}", status.HTTP_422_UNPROCESSABLE_CONTENT)
            setattr(product, key, deepcopy(value))
        transition_product_asset_version_status(
            db, product_asset_version_id=product.id, next_status=ProductAssetVersionStatus.CONFIRMED
        )
        freeze_product_asset_version(db, product_asset_version_id=product.id)
        db.add(
            ReviewDecision(
                project_id=intake.project_id,
                target_type="COMMERCE_PRODUCT_VERSION",
                target_id=product.id,
                decision="LOCKED",
                reviewer_label=(reviewer_label or "制作人").strip() or "制作人",
                note=note.strip() if note else None,
            )
        )
        snapshot = deepcopy(intake.input_snapshot or {})
        snapshot["product_version"] = _product_snapshot(product)
        intake.input_snapshot = snapshot
    elif product.status != ProductAssetVersionStatus.CONFIRMED or product.frozen_at is None:
        _error("该商品版本不能被确认并冻结")
    db.commit()
    db.refresh(intake)
    return intake


def frozen_creative_input(db: Session, state: ProjectProductionState) -> dict[str, Any]:
    """创建十创意任务前冻结唯一可用的脚本、商品、模型输入证据。"""

    analysis_id = state.locked_reference_analysis_id
    if not analysis_id:
        _error("请先人工锁定创作简报")
    intake = db.scalars(
        select(CommerceReferenceIntake).where(
            CommerceReferenceIntake.project_id == state.project_id,
            CommerceReferenceIntake.reference_analysis_id == analysis_id,
        )
    ).first()
    if intake is None:
        _error("锁定分析尚未生成 Commerce 脚本和商品草稿，请重新执行分析任务")
    product = db.get(ProductAssetVersion, intake.product_asset_version_id)
    script = db.get(ScriptAnalysisVersion, intake.script_analysis_version_id)
    analysis = db.get(ReferenceAnalysis, intake.reference_analysis_id)
    if product is None or script is None or analysis is None:
        raise RuntimeError("Commerce 创意输入引用不存在")
    if product.status != ProductAssetVersionStatus.CONFIRMED or product.frozen_at is None:
        _error("请先人工确认并冻结商品版本")
    if script.analysis_status != ScriptAnalysisStatus.SUCCEEDED:
        _error("脚本分析尚未成功，不能生成创意")
    return {
        "reference_intake_id": intake.id,
        "reference_analysis_id": analysis.id,
        "script_analysis_version_id": script.id,
        "product_asset_version_id": product.id,
        "reference_analysis": _analysis_snapshot(analysis),
        "script_analysis": _script_snapshot(script),
        "product_asset_version": _product_snapshot(product),
    }


def _mock_ideas() -> list[dict[str, Any]]:
    ideas: list[dict[str, Any]] = []
    premises = [
        "误会让主角必须在今晚做出选择，产品成为自然解决方案。",
        "家庭关系中的小难题被放大，真实体验带来可验证的转机。",
        "倒计时任务迫使两位角色协作，产品被作为可信道具使用。",
        "主角隐藏的困扰在公共场景暴露，体验过程推动关系变化。",
        "看似普通的承诺遇到阻碍，产品使用场景成为情节转折。",
    ]
    for number in range(1, IDEA_COUNT + 1):
        ideas.append(
            {
                "title": f"原创带货短剧创意 {number}",
                "opening_hook": f"第 {number} 个开场：异常信息出现后必须立刻做决定。",
                "synopsis": premises[(number - 1) % len(premises)],
                "product_integration": {
                    "method": "SOFT_PROP",
                    "evidence_rule": "仅使用冻结商品版本中已确认的卖点、外观和使用场景。",
                },
            }
        )
    return ideas


IDEA_OUTPUT_CONTRACT = (
    '{"ideas":[{"title":"string","opening_hook":"string","synopsis":"string",'
    '"product_integration":{"method":"string","evidence_rule":"string"}}]}'
)


def _require_ideas(payload: Any) -> list[dict[str, Any]]:
    ideas = payload.get("ideas") if isinstance(payload, dict) else None
    if not isinstance(ideas, list) or len(ideas) != IDEA_COUNT:
        raise RuntimeError("故事创意模型必须一次返回恰好 10 个方案")
    normalized: list[dict[str, Any]] = []
    for index, idea in enumerate(ideas, start=1):
        if not isinstance(idea, dict):
            raise RuntimeError(f"故事创意 #{index} 不是对象")
        values = {name: idea.get(name) for name in ("title", "opening_hook", "synopsis")}
        if any(not isinstance(value, str) or not value.strip() for value in values.values()):
            raise RuntimeError(f"故事创意 #{index} 缺少标题、开头或简介")
        integration = idea.get("product_integration")
        if not isinstance(integration, dict):
            raise RuntimeError(f"故事创意 #{index} 缺少结构化商品融入方案")
        normalized.append(
            {
                "title": values["title"].strip()[:180],
                "opening_hook": values["opening_hook"].strip()[:4000],
                "synopsis": values["synopsis"].strip()[:8000],
                "product_integration": deepcopy(integration),
            }
        )
    return normalized


def execute_creative_generation(db: Session, run: WorkflowRun, *, binding: dict[str, Any], prompt: dict[str, Any]) -> CommerceCreativeBatch:
    """执行一个已冻结的十创意 V1 任务；不会读取模型中心/ACTIVE Prompt。"""

    context = ((run.input_snapshot or {}).get("context") or {}).get("commerce_mainline")
    if not isinstance(context, dict):
        raise RuntimeError("创意任务缺少冻结的 Commerce 输入")
    intake_id = context.get("reference_intake_id")
    if not isinstance(intake_id, str):
        raise RuntimeError("创意任务缺少冻结的商品/脚本 intake")
    existing = db.scalars(select(CommerceCreativeBatch).where(CommerceCreativeBatch.workflow_run_id == run.id)).first()
    if existing is not None:
        return existing
    profile = binding.get("profile_snapshot") if isinstance(binding, dict) else None
    if not isinstance(profile, dict):
        raise RuntimeError("创意任务缺少冻结模型配置")
    model_snapshot = deepcopy(profile)
    started_at = perf_counter()
    batch_number = int(
        db.scalar(select(func.max(CommerceCreativeBatch.batch_number)).where(CommerceCreativeBatch.project_id == run.project_id)) or 0
    ) + 1
    batch = CommerceCreativeBatch(
        project_id=run.project_id,
        reference_intake_id=intake_id,
        workflow_run_id=run.id,
        batch_number=batch_number,
        status=CommerceCreativeBatchStatus.RUNNING,
        input_snapshot=deepcopy(context),
        model_snapshot=model_snapshot,
        prompt_snapshot=deepcopy(prompt),
        started_at=utcnow(),
    )
    db.add(batch)
    db.flush()
    invocation = ModelInvocation(
        project_id=run.project_id,
        workflow_run_id=run.id,
        model_slot_id=binding.get("slot_id"),
        model_profile_id=binding.get("model_profile_id"),
        prompt_template_id=prompt.get("id"),
        task_type="STORY_GENERATE",
        model_profile_snapshot=deepcopy(model_snapshot),
        prompt_snapshot=deepcopy(prompt),
        input_snapshot=deepcopy(context),
        idempotency_key=f"{run.idempotency_key}:commerce-ideas",
        status=RunStatus.RUNNING,
    )
    db.add(invocation)
    db.flush()
    if is_mock_adapter(model_snapshot):
        raw = {"ideas": _mock_ideas(), "adapter": "mock_v1"}
    else:
        assert_supported(model_snapshot, "STORY_GENERATE")
        raw = generate_structured_text(
            model_snapshot,
            task_type="STORY_GENERATE",
            system_instruction=(
                f"{prompt.get('content', '').strip()}\n\n"
                "生成恰好十个原创带货短剧创意。只能使用冻结商品版本中的确认事实，"
                "不得创造功效、包装、使用方法或宣传结论。"
            ),
            user_payload={"frozen_input": deepcopy(context), "required_idea_count": IDEA_COUNT},
            output_contract=IDEA_OUTPUT_CONTRACT,
        )
    ideas = _require_ideas(raw)
    for number, content in enumerate(ideas, start=1):
        db.add(
            CommerceCreativeIdea(
                batch_id=batch.id,
                project_id=run.project_id,
                model_invocation_id=invocation.id,
                candidate_number=number,
                content=content,
            )
        )
    invocation.status = RunStatus.SUCCEEDED
    invocation.finished_at = utcnow()
    invocation.latency_ms = max(0, int((perf_counter() - started_at) * 1000))
    invocation.output_reference = {"commerce_creative_batch_id": batch.id, "idea_count": IDEA_COUNT}
    batch.status = CommerceCreativeBatchStatus.SUCCEEDED
    batch.raw_response = deepcopy(raw)
    batch.structured_response = {"ideas": deepcopy(ideas)}
    batch.finished_at = utcnow()
    return batch


def mark_creative_batch_ready(db: Session, batch: CommerceCreativeBatch) -> None:
    """把十创意交给人工选择；不触碰旧 StoryProposal 指针。"""

    state = db.get(ProjectProductionState, batch.project_id)
    if state is None or state.active_stage.value not in {"STORY_GENERATION", "STORY_REVIEW"}:
        raise RuntimeError("创意批次不能在当前 V1 阶段提交审核")
    if batch.status != CommerceCreativeBatchStatus.SUCCEEDED or len(batch.ideas) != IDEA_COUNT:
        raise RuntimeError("创意批次未成功生成 10 个方案")
    state.active_stage = state.active_stage.__class__.STORY_REVIEW
    db.commit()


def list_creative_batches(db: Session, project_id: str) -> list[CommerceCreativeBatch]:
    return list(
        db.scalars(
            select(CommerceCreativeBatch)
            .where(CommerceCreativeBatch.project_id == project_id)
            .order_by(CommerceCreativeBatch.batch_number.desc())
        ).all()
    )


def select_creative_idea(
    db: Session,
    *,
    idea_id: str,
    reviewer_label: str | None,
    note: str | None,
    mode: StoryRunMode = StoryRunMode.STEPWISE,
) -> StoryRun:
    """选择创意后创建唯一关联的 Commerce StoryRun 与冻结输入快照。

    TopicCandidate 只充当既有 StoryRun 的兼容入口；它由这个被选创意生成，不能拿
    其他项目或其他商品的旧 TopicCandidate 拼装新的带货运行。
    """

    idea = db.get(CommerceCreativeIdea, idea_id)
    if idea is None:
        _error("故事创意不存在", status.HTTP_404_NOT_FOUND)
    batch = db.get(CommerceCreativeBatch, idea.batch_id)
    if batch is None or batch.status != CommerceCreativeBatchStatus.SUCCEEDED:
        _error("故事创意批次尚未成功完成")
    if idea.status != CommerceCreativeIdeaStatus.CANDIDATE:
        _error("该故事创意已经被处理，不能覆盖或重复选择")
    if db.scalar(select(CommerceCreativeIdea.id).where(CommerceCreativeIdea.batch_id == batch.id, CommerceCreativeIdea.status == CommerceCreativeIdeaStatus.SELECTED)):
        _error("当前创意批次已经选择过一个方案")
    frozen = deepcopy(batch.input_snapshot or {})
    product_snapshot = frozen.get("product_asset_version")
    script_snapshot = frozen.get("script_analysis")
    analysis_snapshot = frozen.get("reference_analysis")
    if not all(isinstance(item, dict) for item in (product_snapshot, script_snapshot, analysis_snapshot)):
        raise RuntimeError("创意批次缺少冻结的输入证据链")
    product_id = product_snapshot.get("id")
    script_id = script_snapshot.get("id")
    analysis_id = analysis_snapshot.get("id")
    if not all(isinstance(item, str) and item for item in (product_id, script_id, analysis_id)):
        raise RuntimeError("创意批次冻结输入 ID 无效")
    # ``reference_intake_id`` 是批次建立时的真实冻结归属；快照中任何被人工或故障
    # 修改的跨项目版本 ID 都不能绕过这条证据链。
    frozen_intake_id = frozen.get("reference_intake_id")
    if not isinstance(frozen_intake_id, str) or frozen_intake_id != batch.reference_intake_id:
        _error("创意批次的冻结 intake 引用无效，不能创建 StoryRun")
    product = db.get(ProductAssetVersion, product_id)
    intake = db.get(CommerceReferenceIntake, batch.reference_intake_id)
    if (
        product is None
        or intake is None
        or product.id != intake.product_asset_version_id
        or product.product_asset_id != intake.product_asset_id
        or batch.project_id != idea.project_id
    ):
        _error("创意批次与已确认商品草稿引用不一致，不能跨产品创建 StoryRun")
    if product.status != ProductAssetVersionStatus.CONFIRMED or product.frozen_at is None:
        _error("创意只能绑定已确认且冻结的商品版本")
    selection = db.scalars(
        select(ProjectProductSelection).where(
            ProjectProductSelection.project_id == idea.project_id,
            ProjectProductSelection.product_asset_version_id == product.id,
        )
    ).first()
    if selection is None:
        selection = create_project_product_selection(
            db,
            project_id=idea.project_id,
            product_asset_id=product.product_asset_id,
            product_asset_version_id=product.id,
        )
    content = idea.content or {}
    topic = TopicCandidate(
        project_id=idea.project_id,
        generation_run_id=batch.workflow_run_id,
        position=idea.candidate_number,
        title=str(content.get("title") or "未命名创意")[:180],
        opening_hook=str(content.get("opening_hook") or "")[:4000],
        synopsis=str(content.get("synopsis") or "")[:8000],
        status=TopicStatus.SELECTED,
    )
    db.add(topic)
    db.flush()
    run_number = int(
        db.scalar(
            select(func.max(StoryRun.run_number)).where(
                StoryRun.project_id == idea.project_id, StoryRun.topic_candidate_id == topic.id
            )
        )
        or 0
    ) + 1
    story_run = create_story_run(
        db,
        project_id=idea.project_id,
        topic_candidate_id=topic.id,
        project_product_selection_id=selection.id,
        product_asset_version_id=product.id,
        run_number=run_number,
        mode=mode,
    )
    db.add(
        CommerceStoryRunInput(
            story_run_id=story_run.id,
            creative_batch_id=batch.id,
            creative_idea_id=idea.id,
            reference_analysis_id=analysis_id,
            script_analysis_version_id=script_id,
            product_asset_version_id=product.id,
            input_snapshot={
                "reference_analysis": deepcopy(analysis_snapshot),
                "script_analysis": deepcopy(script_snapshot),
                "product_asset_version": deepcopy(product_snapshot),
                "creative_batch": {
                    "id": batch.id,
                    "batch_number": batch.batch_number,
                    "model_snapshot": deepcopy(batch.model_snapshot),
                    "prompt_snapshot": deepcopy(batch.prompt_snapshot),
                },
                "creative_idea": {"id": idea.id, "candidate_number": idea.candidate_number, "content": deepcopy(content)},
            },
        )
    )
    idea.status = CommerceCreativeIdeaStatus.SELECTED
    idea.selected_at = utcnow()
    idea.topic_candidate_id = topic.id
    db.add(
        ReviewDecision(
            project_id=idea.project_id,
            target_type="COMMERCE_CREATIVE_IDEA",
            target_id=idea.id,
            decision="SELECTED",
            reviewer_label=(reviewer_label or "制作人").strip() or "制作人",
            note=note.strip() if note else None,
        )
    )
    db.flush()
    # ``start_story_run`` 需要先在独立事务中领取 TOPIC / OUTLINE 的状态；本服务
    # 只负责建立可审计输入，确保路由可在已提交的冻结事实之上投递既有工作流。
    db.commit()
    db.refresh(story_run)
    return story_run
