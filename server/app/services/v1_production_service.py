"""LemonFlow V1 主生产链路的状态机与人工审核闸门。

本模块只处理项目阶段、审核决定和锁定指针。它不调用 Gemini、Claude、Banana 或
Seedance；模型任务由后续 Adapter/Worker 写入生成结果后，再通过这里的标准状态
转换进入下一阶段。这样业务流程不会依赖任何具体供应商。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CharacterDefinition,
    CharacterReferenceImage,
    DesignStatus,
    DirectorPlan,
    DirectorPlanStatus,
    ProductionStage,
    ProjectProductionState,
    ReferenceAnalysis,
    ReviewDecision,
    ReviewStatus,
    RunStatus,
    SceneDefinition,
    SceneReferenceImage,
    ShotKeyframe,
    ShotPlan,
    StoryGenerationBatch,
    StoryProposal,
    StoryProposalStatus,
    VideoClip,
    VideoClipStatus,
    VideoReviewStatus,
)
from app.services.v1_configuration_service import get_or_create_project_state
from app.services.workflow_service import get_project_or_404


def utcnow() -> datetime:
    """统一保存审核与状态转换时间为 UTC。"""

    return datetime.now(timezone.utc)


def _conflict(message: str) -> None:
    """用统一的 409 表达“前置条件尚未满足”，避免接口层复制状态判断。"""

    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)


def _get_v1_state(db: Session, project_id: str) -> ProjectProductionState:
    """读取项目 V1 状态；历史只读项目不得意外进入新主流程。"""

    get_project_or_404(db, project_id)
    state = get_or_create_project_state(db, project_id)
    if state.active_stage == ProductionStage.LEGACY_READONLY:
        _conflict("该项目属于历史兼容流程，只读展示；请新建 V1 项目开始生产")
    return state


def get_project_production_state(db: Session, project_id: str) -> ProjectProductionState:
    """供生产台读取当前阶段及所有已经冻结的主指针。"""

    return _get_v1_state(db, project_id)


def _require_stage(state: ProjectProductionState, allowed: Iterable[ProductionStage], action: str) -> None:
    """限制人工操作只能发生在其所属审核阶段，杜绝跳过审核闸门。"""

    allowed_values = set(allowed)
    if state.active_stage not in allowed_values:
        allowed_text = "、".join(item.value for item in allowed_values)
        _conflict(f"当前阶段为 {state.active_stage.value}，不能{action}；应处于 {allowed_text}")


def _review(
    db: Session,
    *,
    project_id: str,
    target_type: str,
    target_id: str,
    decision: str,
    reviewer_label: Optional[str],
    note: Optional[str],
    quality_score: Optional[int],
) -> None:
    """记录审核与可选质量评分；评分只服务后续人工模型比较。"""

    db.add(
        ReviewDecision(
            project_id=project_id,
            target_type=target_type,
            target_id=target_id,
            decision=decision,
            reviewer_label=(reviewer_label or "人工审核").strip()[:120] or "人工审核",
            note=note.strip() if note else None,
            quality_score=quality_score,
        )
    )


def _require_generation_succeeded(generation_status: RunStatus, label: str) -> None:
    """未成功生成的内容不允许进入人工锁定，避免锁住空结果。"""

    if generation_status != RunStatus.SUCCEEDED:
        _conflict(f"{label}尚未生成成功，暂不能审核锁定")


def _get_or_404(db: Session, entity_type, entity_id: str, label: str):
    entity = db.get(entity_type, entity_id)
    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{label}不存在")
    return entity


# ---------------------------------------------------------------------------
# Worker 完成节点。当前 Phase 2 只提供状态写入边界，真实模型调用在 Phase 4 Adapter
# 接入后使用这些函数，不能由前端伪造“生成成功”。
# ---------------------------------------------------------------------------


def mark_reference_analysis_ready(db: Session, analysis_id: str) -> ReferenceAnalysis:
    """将已由 Worker 写好的分析结果送入人工审核阶段。"""

    analysis = _get_or_404(db, ReferenceAnalysis, analysis_id, "参考视频分析")
    state = _get_v1_state(db, analysis.project_id)
    _require_stage(state, [ProductionStage.REFERENCE_ANALYSIS], "提交分析审核")
    _require_generation_succeeded(analysis.generation_status, "参考视频分析")
    if analysis.review_status != ReviewStatus.PENDING_REVIEW:
        _conflict("该分析结果不是待审核状态")
    state.active_stage = ProductionStage.ANALYSIS_REVIEW
    db.commit()
    db.refresh(analysis)
    return analysis


def mark_story_batch_ready(db: Session, batch_id: str) -> StoryGenerationBatch:
    """当并行故事任务结束且已有候选后，允许人工选择故事。"""

    batch = _get_or_404(db, StoryGenerationBatch, batch_id, "故事生成批次")
    state = _get_v1_state(db, batch.project_id)
    _require_stage(state, [ProductionStage.STORY_GENERATION], "提交故事审核")
    if batch.reference_analysis_id != state.locked_reference_analysis_id:
        _conflict("故事生成批次必须使用本项目当前锁定的创作简报")
    _require_generation_succeeded(batch.status, "故事生成批次")
    has_candidate = db.scalar(select(StoryProposal.id).where(StoryProposal.batch_id == batch.id).limit(1))
    if has_candidate is None:
        _conflict("故事生成批次没有候选方案，不能进入人工选择")
    state.active_stage = ProductionStage.STORY_REVIEW
    db.commit()
    db.refresh(batch)
    return batch


def mark_director_plan_ready(db: Session, director_plan_id: str) -> DirectorPlan:
    """导演分镜完成后冻结项目指针，并转入关键帧生产。"""

    director_plan = _get_or_404(db, DirectorPlan, director_plan_id, "导演分镜方案")
    state = _get_v1_state(db, director_plan.project_id)
    _require_stage(state, [ProductionStage.DIRECTOR_PLANNING], "提交导演分镜")
    if director_plan.story_proposal_id != state.selected_story_proposal_id:
        _conflict("导演分镜必须基于本项目当前选中的故事")
    if director_plan.status != DirectorPlanStatus.READY:
        _conflict("导演分镜尚未生成完成")
    if state.director_plan_id and state.director_plan_id != director_plan.id:
        _conflict("本轮生产已冻结导演分镜；请创建新的生产版本，不可覆盖历史分镜")
    state.director_plan_id = director_plan.id
    state.active_stage = ProductionStage.SHOT_KEYFRAMES
    db.commit()
    db.refresh(director_plan)
    return director_plan


def mark_video_clip_ready(db: Session, clip_id: str) -> VideoClip:
    """视频 Worker 成功后进入人工审核，不会自动进入成片合成。"""

    clip = _get_or_404(db, VideoClip, clip_id, "视频片段")
    state = _get_v1_state(db, clip.project_id)
    _require_stage(state, [ProductionStage.VIDEO_GENERATION], "提交视频审核")
    if clip.shot_plan_id is None:
        _conflict("历史视频片段不能写入 V1 视频审核节点")
    shot = _get_or_404(db, ShotPlan, clip.shot_plan_id, "视频片段引用的分镜")
    if shot.director_plan_id != state.director_plan_id:
        _conflict("视频片段引用的分镜不属于当前导演方案")
    if clip.generation_status != RunStatus.SUCCEEDED.value and clip.status != VideoClipStatus.SUCCEEDED:
        _conflict("视频片段尚未生成成功")
    if clip.review_status not in {None, VideoReviewStatus.PENDING_REVIEW.value}:
        _conflict("视频片段不是待审核状态")
    clip.generation_status = RunStatus.SUCCEEDED.value
    clip.review_status = VideoReviewStatus.PENDING_REVIEW.value
    state.active_stage = ProductionStage.VIDEO_REVIEW
    db.commit()
    db.refresh(clip)
    return clip


# ---------------------------------------------------------------------------
# 人工审核节点。
# ---------------------------------------------------------------------------


def lock_reference_analysis(
    db: Session, analysis_id: str, *, reviewer_label: Optional[str], note: Optional[str], quality_score: Optional[int] = None
) -> ReferenceAnalysis:
    """锁定完整创作简报，后续故事生成只能消费该不可变快照。"""

    analysis = _get_or_404(db, ReferenceAnalysis, analysis_id, "参考视频分析")
    state = _get_v1_state(db, analysis.project_id)
    _require_stage(state, [ProductionStage.ANALYSIS_REVIEW], "锁定分析结果")
    _require_generation_succeeded(analysis.generation_status, "参考视频分析")
    if analysis.review_status != ReviewStatus.PENDING_REVIEW:
        _conflict("分析结果不是待审核状态，不能重复锁定或覆盖")

    analysis.review_status = ReviewStatus.LOCKED
    analysis.locked_at = utcnow()
    analysis.locked_snapshot = {
        "video_script_structure": analysis.video_script_structure,
        "opening_analysis": analysis.opening_analysis,
        "viral_elements": analysis.viral_elements,
        "scene_analysis": analysis.scene_analysis,
        "creative_brief": analysis.creative_brief,
        "version": analysis.version,
    }
    state.locked_reference_analysis_id = analysis.id
    state.active_stage = ProductionStage.STORY_GENERATION
    _review(
        db,
        project_id=analysis.project_id,
        target_type="REFERENCE_ANALYSIS",
        target_id=analysis.id,
        decision="LOCKED",
        reviewer_label=reviewer_label,
        note=note,
        quality_score=quality_score,
    )
    db.commit()
    db.refresh(analysis)
    return analysis


def reject_reference_analysis(
    db: Session, analysis_id: str, *, reviewer_label: Optional[str], note: Optional[str], quality_score: Optional[int] = None
) -> ReferenceAnalysis:
    """驳回分析版本并回到分析阶段；重跑必须创建新版本。"""

    analysis = _get_or_404(db, ReferenceAnalysis, analysis_id, "参考视频分析")
    state = _get_v1_state(db, analysis.project_id)
    _require_stage(state, [ProductionStage.ANALYSIS_REVIEW], "驳回分析结果")
    if analysis.review_status != ReviewStatus.PENDING_REVIEW:
        _conflict("分析结果不是待审核状态，不能重复驳回")
    analysis.review_status = ReviewStatus.REJECTED
    state.active_stage = ProductionStage.REFERENCE_ANALYSIS
    _review(
        db,
        project_id=analysis.project_id,
        target_type="REFERENCE_ANALYSIS",
        target_id=analysis.id,
        decision="REJECTED",
        reviewer_label=reviewer_label,
        note=note,
        quality_score=quality_score,
    )
    db.commit()
    db.refresh(analysis)
    return analysis


def select_story_proposal(
    db: Session, proposal_id: str, *, reviewer_label: Optional[str], note: Optional[str], quality_score: Optional[int] = None
) -> StoryProposal:
    """选择本批次原创故事，选中结果只能通过创建新批次迭代。"""

    proposal = _get_or_404(db, StoryProposal, proposal_id, "故事方案")
    state = _get_v1_state(db, proposal.project_id)
    _require_stage(state, [ProductionStage.STORY_REVIEW], "选择故事方案")
    batch = _get_or_404(db, StoryGenerationBatch, proposal.batch_id, "故事生成批次")
    if batch.reference_analysis_id != state.locked_reference_analysis_id:
        _conflict("故事方案不是基于本项目当前锁定的创作简报生成")
    if proposal.status != StoryProposalStatus.CANDIDATE:
        _conflict("故事方案不是候选状态，不能重复选择或覆盖")
    selected_in_batch = db.scalar(
        select(StoryProposal.id).where(
            StoryProposal.batch_id == proposal.batch_id,
            StoryProposal.status == StoryProposalStatus.SELECTED,
        )
    )
    if selected_in_batch is not None:
        _conflict("该故事批次已有人工选中的方案，不能覆盖")

    proposal.status = StoryProposalStatus.SELECTED
    state.selected_story_proposal_id = proposal.id
    state.active_stage = ProductionStage.CHARACTER_ASSETS
    _review(
        db,
        project_id=proposal.project_id,
        target_type="STORY_PROPOSAL",
        target_id=proposal.id,
        decision="SELECTED",
        reviewer_label=reviewer_label,
        note=note,
        quality_score=quality_score,
    )
    db.commit()
    db.refresh(proposal)
    return proposal


def _characters_are_locked(db: Session, state: ProjectProductionState) -> bool:
    if state.selected_story_proposal_id is None:
        return False
    characters = list(
        db.scalars(
            select(CharacterDefinition).where(
                CharacterDefinition.project_id == state.project_id,
                CharacterDefinition.story_proposal_id == state.selected_story_proposal_id,
            )
        ).all()
    )
    return bool(characters) and all(item.locked_reference_image_id for item in characters)


def _scenes_are_locked(db: Session, state: ProjectProductionState) -> bool:
    if state.selected_story_proposal_id is None:
        return False
    scenes = list(
        db.scalars(
            select(SceneDefinition).where(
                SceneDefinition.project_id == state.project_id,
                SceneDefinition.story_proposal_id == state.selected_story_proposal_id,
            )
        ).all()
    )
    return bool(scenes) and all(item.locked_reference_image_id for item in scenes)


def lock_character_reference_image(
    db: Session, image_id: str, *, reviewer_label: Optional[str], note: Optional[str], quality_score: Optional[int] = None
) -> CharacterReferenceImage:
    """锁定角色参考图版本，并在全部角色完成时自动进入场景资产阶段。"""

    image = _get_or_404(db, CharacterReferenceImage, image_id, "角色参考图")
    character = _get_or_404(db, CharacterDefinition, image.character_id, "角色设定")
    state = _get_v1_state(db, image.project_id)
    _require_stage(state, [ProductionStage.CHARACTER_ASSETS], "锁定角色参考图")
    if character.project_id != image.project_id or character.story_proposal_id != state.selected_story_proposal_id:
        _conflict("角色参考图不属于当前选中故事")
    _require_generation_succeeded(image.generation_status, "角色参考图")
    if not image.image_url:
        _conflict("角色参考图没有可用图片地址")
    if image.review_status != ReviewStatus.PENDING_REVIEW:
        _conflict("角色参考图不是待审核状态，不能重复锁定或覆盖")

    image.review_status = ReviewStatus.LOCKED
    character.locked_reference_image_id = image.id
    character.design_status = DesignStatus.LOCKED
    if _characters_are_locked(db, state):
        state.active_stage = ProductionStage.SCENE_ASSETS
    _review(
        db,
        project_id=image.project_id,
        target_type="CHARACTER_REFERENCE_IMAGE",
        target_id=image.id,
        decision="LOCKED",
        reviewer_label=reviewer_label,
        note=note,
        quality_score=quality_score,
    )
    db.commit()
    db.refresh(image)
    return image


def lock_scene_reference_image(
    db: Session, image_id: str, *, reviewer_label: Optional[str], note: Optional[str], quality_score: Optional[int] = None
) -> SceneReferenceImage:
    """锁定场景参考图版本，并在全部场景完成时进入导演分镜阶段。"""

    image = _get_or_404(db, SceneReferenceImage, image_id, "场景参考图")
    scene = _get_or_404(db, SceneDefinition, image.scene_id, "场景设定")
    state = _get_v1_state(db, image.project_id)
    _require_stage(state, [ProductionStage.SCENE_ASSETS], "锁定场景参考图")
    if scene.project_id != image.project_id or scene.story_proposal_id != state.selected_story_proposal_id:
        _conflict("场景参考图不属于当前选中故事")
    _require_generation_succeeded(image.generation_status, "场景参考图")
    if not image.image_url:
        _conflict("场景参考图没有可用图片地址")
    if image.review_status != ReviewStatus.PENDING_REVIEW:
        _conflict("场景参考图不是待审核状态，不能重复锁定或覆盖")

    image.review_status = ReviewStatus.LOCKED
    scene.locked_reference_image_id = image.id
    scene.design_status = DesignStatus.LOCKED
    if _scenes_are_locked(db, state):
        state.active_stage = ProductionStage.DIRECTOR_PLANNING
    _review(
        db,
        project_id=image.project_id,
        target_type="SCENE_REFERENCE_IMAGE",
        target_id=image.id,
        decision="LOCKED",
        reviewer_label=reviewer_label,
        note=note,
        quality_score=quality_score,
    )
    db.commit()
    db.refresh(image)
    return image


def _keyframes_are_locked(db: Session, state: ProjectProductionState) -> bool:
    if state.director_plan_id is None:
        return False
    shots = list(
        db.scalars(
            select(ShotPlan).where(
                ShotPlan.project_id == state.project_id,
                ShotPlan.director_plan_id == state.director_plan_id,
            )
        ).all()
    )
    return bool(shots) and all(item.locked_keyframe_id for item in shots)


def lock_shot_keyframe(
    db: Session, keyframe_id: str, *, reviewer_label: Optional[str], note: Optional[str], quality_score: Optional[int] = None
) -> ShotKeyframe:
    """锁定关键帧，并在全部分镜完成时开启 Seedance 视频生成阶段。"""

    keyframe = _get_or_404(db, ShotKeyframe, keyframe_id, "分镜关键帧")
    shot = _get_or_404(db, ShotPlan, keyframe.shot_id, "分镜")
    state = _get_v1_state(db, keyframe.project_id)
    _require_stage(state, [ProductionStage.SHOT_KEYFRAMES], "锁定分镜关键帧")
    if shot.project_id != keyframe.project_id or shot.director_plan_id != state.director_plan_id:
        _conflict("关键帧不属于当前导演分镜")
    _require_generation_succeeded(keyframe.generation_status, "分镜关键帧")
    if not keyframe.image_url:
        _conflict("分镜关键帧没有可用图片地址")
    if keyframe.review_status != ReviewStatus.PENDING_REVIEW:
        _conflict("分镜关键帧不是待审核状态，不能重复锁定或覆盖")

    keyframe.review_status = ReviewStatus.LOCKED
    shot.locked_keyframe_id = keyframe.id
    if _keyframes_are_locked(db, state):
        state.active_stage = ProductionStage.VIDEO_GENERATION
    _review(
        db,
        project_id=keyframe.project_id,
        target_type="SHOT_KEYFRAME",
        target_id=keyframe.id,
        decision="LOCKED",
        reviewer_label=reviewer_label,
        note=note,
        quality_score=quality_score,
    )
    db.commit()
    db.refresh(keyframe)
    return keyframe


def _current_v1_clips(db: Session, state: ProjectProductionState) -> list[VideoClip]:
    """仅统计当前导演方案的 V1 片段，历史按组视频不会混入审核闸门。"""

    if state.director_plan_id is None:
        return []
    return list(
        db.scalars(
            select(VideoClip)
            .join(ShotPlan, VideoClip.shot_plan_id == ShotPlan.id)
            .where(
                VideoClip.project_id == state.project_id,
                ShotPlan.director_plan_id == state.director_plan_id,
            )
        ).all()
    )


def approve_video_clip(
    db: Session, clip_id: str, *, reviewer_label: Optional[str], note: Optional[str], quality_score: Optional[int] = None
) -> VideoClip:
    """人工通过视频片段；仅所有当前片段都通过后才可进入合成。"""

    clip = _get_or_404(db, VideoClip, clip_id, "视频片段")
    state = _get_v1_state(db, clip.project_id)
    _require_stage(state, [ProductionStage.VIDEO_REVIEW], "通过视频片段")
    if clip.generation_status != RunStatus.SUCCEEDED.value or clip.review_status != VideoReviewStatus.PENDING_REVIEW.value:
        _conflict("视频片段不是待审核的成功结果")
    clips = _current_v1_clips(db, state)
    if clip.id not in {item.id for item in clips}:
        _conflict("视频片段不属于当前导演方案")

    clip.review_status = VideoReviewStatus.APPROVED.value
    clip.reviewed_at = utcnow()
    clip.review_note = note.strip() if note else None
    if clips and all(item.id == clip.id or item.review_status == VideoReviewStatus.APPROVED.value for item in clips):
        state.active_stage = ProductionStage.FINAL_EXPORT
    _review(
        db,
        project_id=clip.project_id,
        target_type="VIDEO_CLIP",
        target_id=clip.id,
        decision="APPROVED",
        reviewer_label=reviewer_label,
        note=note,
        quality_score=quality_score,
    )
    db.commit()
    db.refresh(clip)
    return clip


def reject_video_clip(
    db: Session, clip_id: str, *, reviewer_label: Optional[str], note: Optional[str], quality_score: Optional[int] = None
) -> VideoClip:
    """驳回片段后回到视频生成阶段；重做必须创建新的 VideoClip 版本。"""

    clip = _get_or_404(db, VideoClip, clip_id, "视频片段")
    state = _get_v1_state(db, clip.project_id)
    _require_stage(state, [ProductionStage.VIDEO_REVIEW], "驳回视频片段")
    if clip.generation_status != RunStatus.SUCCEEDED.value or clip.review_status != VideoReviewStatus.PENDING_REVIEW.value:
        _conflict("视频片段不是待审核的成功结果")
    if clip.id not in {item.id for item in _current_v1_clips(db, state)}:
        _conflict("视频片段不属于当前导演方案")

    clip.review_status = VideoReviewStatus.REJECTED.value
    clip.reviewed_at = utcnow()
    clip.review_note = note.strip() if note else None
    state.active_stage = ProductionStage.VIDEO_GENERATION
    _review(
        db,
        project_id=clip.project_id,
        target_type="VIDEO_CLIP",
        target_id=clip.id,
        decision="REJECTED",
        reviewer_label=reviewer_label,
        note=note,
        quality_score=quality_score,
    )
    db.commit()
    db.refresh(clip)
    return clip
