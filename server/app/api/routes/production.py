"""LemonFlow V1 生产台与人工审核 HTTP 接口。

所有写操作均委托给 ``v1_production_service``，路由层只负责编码响应，避免页面
绕过主流程直接修改数据库状态。模型生成任务将在 Adapter Phase 接入；本模块只公开
真实生成结果的查看、审核和锁定入口。
"""

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import (
    CharacterDefinition,
    CharacterReferenceImage,
    DirectorPlan,
    ReferenceAnalysis,
    SceneDefinition,
    SceneReferenceImage,
    ShotKeyframe,
    ShotAssetBinding,
    ShotPlan,
    StoryProposal,
    VideoClip,
)
from app.schemas import (
    CharacterReferenceImageV1Response,
    DirectorPlanV1Response,
    DirectorShotResponse,
    ModelInvocationTraceResponse,
    ProductionStateResponse,
    ReferenceAnalysisResponse,
    ReviewActionRequest,
    SceneReferenceImageV1Response,
    ShotKeyframeV1Response,
    StoryProposalV1Response,
    VideoClipV1Response,
    V1GenerationRunRequest,
    WorkflowRunResponse,
)
from app.api.routes.projects import _run_response
from app.services.worker_runtime import dispatch_v1_video_children, dispatch_workflow
from app.services.v1_execution_service import RUN_SPECS, create_v1_run
from app.services.v1_production_service import (
    approve_video_clip,
    adopt_character_asset_version,
    adopt_scene_asset_version,
    get_project_production_state,
    lock_character_reference_image,
    lock_reference_analysis,
    lock_scene_reference_image,
    lock_shot_keyframe,
    reject_reference_analysis,
    reject_video_clip,
    select_story_proposal,
)
from app.services.v1_trace_service import list_project_invocation_traces


router = APIRouter(prefix="/api/v1/production", tags=["V1 生产台"])


def _enum_value(value) -> str:
    """兼容 SQLAlchemy 枚举和旧表字符串字段。"""

    return value.value if hasattr(value, "value") else str(value)


def _state_response(state) -> ProductionStateResponse:
    return ProductionStateResponse(
        project_id=state.project_id,
        active_stage=_enum_value(state.active_stage),
        workflow_definition_id=state.workflow_definition_id,
        locked_reference_analysis_id=state.locked_reference_analysis_id,
        selected_story_proposal_id=state.selected_story_proposal_id,
        director_plan_id=state.director_plan_id,
        created_at=state.created_at,
        updated_at=state.updated_at,
    )


def _analysis_response(item: ReferenceAnalysis) -> ReferenceAnalysisResponse:
    return ReferenceAnalysisResponse(
        id=item.id,
        project_id=item.project_id,
        workflow_run_id=item.workflow_run_id,
        version=item.version,
        video_script_structure=item.video_script_structure,
        opening_analysis=item.opening_analysis,
        viral_elements=item.viral_elements,
        scene_analysis=item.scene_analysis,
        creative_brief=item.creative_brief,
        generation_status=_enum_value(item.generation_status),
        review_status=_enum_value(item.review_status),
        locked_snapshot=item.locked_snapshot,
        locked_at=item.locked_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _story_response(item: StoryProposal) -> StoryProposalV1Response:
    return StoryProposalV1Response(
        id=item.id,
        project_id=item.project_id,
        batch_id=item.batch_id,
        model_invocation_id=item.model_invocation_id,
        candidate_number=item.candidate_number,
        content=item.content,
        status=_enum_value(item.status),
        created_at=item.created_at,
    )


def _director_plan_response(db: Session, plan: DirectorPlan) -> DirectorPlanV1Response:
    """编码 Phase 4 结构化导演方案；镜头资产版本来自明确绑定，禁止按最新版本猜测。"""

    shots = list(
        db.scalars(
            select(ShotPlan)
            .where(ShotPlan.director_plan_id == plan.id)
            .order_by(ShotPlan.shot_number)
        ).all()
    )
    rows: list[DirectorShotResponse] = []
    for shot in shots:
        bindings = list(db.scalars(select(ShotAssetBinding).where(ShotAssetBinding.shot_id == shot.id)).all())
        scene_binding = next((item for item in bindings if item.scene_id), None)
        if scene_binding is None:
            # 历史不完整草稿仍可被读取，不对生产时的严格校验作任何放松。
            continue
        rows.append(
            DirectorShotResponse(
                id=shot.id,
                shot_number=shot.shot_number,
                duration=float(shot.duration_seconds),
                character_ids=[item.character_id for item in bindings if item.character_id],
                character_asset_version_ids=[
                    item.character_asset_version_id for item in bindings if item.character_asset_version_id
                ],
                scene_id=scene_binding.scene_id,
                scene_asset_version_id=scene_binding.scene_asset_version_id,
                action=shot.action_description,
                emotion=shot.emotion,
                camera_type=shot.camera_type,
                camera_move=shot.camera_move,
                lighting=shot.lighting,
                image_prompt=shot.image_prompt,
                video_prompt=shot.video_prompt,
                sound_prompt=shot.sound_prompt,
                locked_keyframe_id=shot.locked_keyframe_id,
                created_at=shot.created_at,
            )
        )
    return DirectorPlanV1Response(
        id=plan.id,
        project_id=plan.project_id,
        story_proposal_id=plan.story_proposal_id,
        workflow_run_id=plan.workflow_run_id,
        visual_bible=plan.visual_bible,
        status=_enum_value(plan.status),
        shots=rows,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )


@router.post(
    "/projects/{project_id}/generation-runs/{run_key}",
    response_model=WorkflowRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_v1_generation_run_endpoint(
    project_id: str,
    run_key: str,
    background_tasks: BackgroundTasks,
    payload: Optional[V1GenerationRunRequest] = None,
    db: Session = Depends(get_db),
) -> WorkflowRunResponse:
    """创建并投递一个 V1 生成节点。

    ``run_key`` 只能是服务端列出的正式节点，例如 ``reference_analysis``、
    ``story_generation`` 或 ``video_generation``。模型、Prompt 和 Workflow 版本会
    在创建时冻结；浏览器不能传供应商名或密钥覆盖它们。
    """

    if run_key not in RUN_SPECS:
        from fastapi import HTTPException

        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="未知的 V1 生成节点")
    requested_source_asset_id = payload.source_asset_id if payload and run_key == "reference_analysis" else None
    requested_shots = payload.shot_plan_ids if payload and run_key == "video_generation" else None
    run = create_v1_run(
        db,
        project_id=project_id,
        run_key=run_key,
        source_asset_id=requested_source_asset_id,
        shot_plan_ids=requested_shots,
    )
    if getattr(run, "_created", True):
        if run_key == "video_generation":
            dispatch_v1_video_children(background_tasks, run.id)
        else:
            dispatch_workflow(background_tasks, run.workflow_key, run.id)
    return _run_response(run)


@router.get("/projects/{project_id}/state", response_model=ProductionStateResponse)
def get_production_state_endpoint(project_id: str, db: Session = Depends(get_db)) -> ProductionStateResponse:
    """读取 V1 唯一主流程的当前阶段和冻结输入指针。"""

    return _state_response(get_project_production_state(db, project_id))


@router.get("/projects/{project_id}/director-plan", response_model=Optional[DirectorPlanV1Response])
def get_current_director_plan_endpoint(
    project_id: str, db: Session = Depends(get_db)
) -> Optional[DirectorPlanV1Response]:
    """读取当前项目已冻结的导演方案及可直接投产的结构化镜头数据。"""

    state = get_project_production_state(db, project_id)
    if not state.director_plan_id:
        return None
    plan = db.get(DirectorPlan, state.director_plan_id)
    if plan is None:
        return None
    return _director_plan_response(db, plan)


@router.get("/projects/{project_id}/model-invocations", response_model=list[ModelInvocationTraceResponse])
def list_project_model_invocations_endpoint(
    project_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[ModelInvocationTraceResponse]:
    """返回项目级版本追溯信息，不泄露原始素材、Prompt 正文或模型输出。"""

    get_project_production_state(db, project_id)
    return [ModelInvocationTraceResponse(**item) for item in list_project_invocation_traces(db, project_id=project_id, limit=limit)]


@router.get("/projects/{project_id}/reference-analyses", response_model=list[ReferenceAnalysisResponse])
def list_reference_analyses_endpoint(
    project_id: str, db: Session = Depends(get_db)
) -> list[ReferenceAnalysisResponse]:
    """列出分析历史版本，供人工比较后锁定其中一份。"""

    get_project_production_state(db, project_id)
    items = db.scalars(
        select(ReferenceAnalysis)
        .where(ReferenceAnalysis.project_id == project_id)
        .order_by(ReferenceAnalysis.version.desc(), ReferenceAnalysis.created_at.desc())
    ).all()
    return [_analysis_response(item) for item in items]


@router.post("/reference-analyses/{analysis_id}/lock", response_model=ReferenceAnalysisResponse)
def lock_reference_analysis_endpoint(
    analysis_id: str,
    payload: ReviewActionRequest,
    db: Session = Depends(get_db),
) -> ReferenceAnalysisResponse:
    """人工确认并冻结创作简报，之后故事生成只能读取该快照。"""

    return _analysis_response(
        lock_reference_analysis(
            db, analysis_id, reviewer_label=payload.reviewer_label, note=payload.note, quality_score=payload.quality_score
        )
    )


@router.post("/reference-analyses/{analysis_id}/reject", response_model=ReferenceAnalysisResponse)
def reject_reference_analysis_endpoint(
    analysis_id: str,
    payload: ReviewActionRequest,
    db: Session = Depends(get_db),
) -> ReferenceAnalysisResponse:
    """人工驳回分析版本；系统返回分析阶段，下一次生成必须形成新版本。"""

    return _analysis_response(
        reject_reference_analysis(
            db, analysis_id, reviewer_label=payload.reviewer_label, note=payload.note, quality_score=payload.quality_score
        )
    )


@router.get("/projects/{project_id}/story-proposals", response_model=list[StoryProposalV1Response])
def list_story_proposals_endpoint(
    project_id: str, db: Session = Depends(get_db)
) -> list[StoryProposalV1Response]:
    """列出多模型并行生成的原创故事候选，不混入旧故事包。"""

    get_project_production_state(db, project_id)
    items = db.scalars(
        select(StoryProposal)
        .where(StoryProposal.project_id == project_id)
        .order_by(StoryProposal.created_at.desc(), StoryProposal.candidate_number)
    ).all()
    return [_story_response(item) for item in items]


@router.post("/story-proposals/{proposal_id}/select", response_model=StoryProposalV1Response)
def select_story_proposal_endpoint(
    proposal_id: str,
    payload: ReviewActionRequest,
    db: Session = Depends(get_db),
) -> StoryProposalV1Response:
    """人工选择一份原创故事，放行角色资产设计阶段。"""

    return _story_response(
        select_story_proposal(
            db, proposal_id, reviewer_label=payload.reviewer_label, note=payload.note, quality_score=payload.quality_score
        )
    )


@router.get(
    "/projects/{project_id}/character-reference-images",
    response_model=list[CharacterReferenceImageV1Response],
)
def list_character_reference_images_endpoint(
    project_id: str, db: Session = Depends(get_db)
) -> list[CharacterReferenceImageV1Response]:
    """返回角色图的所有版本，让人工明确选择和锁定版本。"""

    get_project_production_state(db, project_id)
    rows = db.execute(
        select(CharacterReferenceImage, CharacterDefinition)
        .join(CharacterDefinition, CharacterReferenceImage.character_id == CharacterDefinition.id)
        .where(CharacterReferenceImage.project_id == project_id)
        .order_by(CharacterDefinition.character_code, CharacterReferenceImage.version.desc())
    ).all()
    return [
        CharacterReferenceImageV1Response(
            id=image.id,
            project_id=image.project_id,
            character_id=character.id,
            character_code=character.character_code,
            character_name=character.name,
            version=image.version,
            asset_version_id=image.asset_version_id,
            image_url=image.image_url,
            generation_status=_enum_value(image.generation_status),
            review_status=_enum_value(image.review_status),
            created_at=image.created_at,
        )
        for image, character in rows
    ]


@router.post("/character-reference-images/{image_id}/lock", response_model=CharacterReferenceImageV1Response)
def lock_character_reference_image_endpoint(
    image_id: str,
    payload: ReviewActionRequest,
    db: Session = Depends(get_db),
) -> CharacterReferenceImageV1Response:
    """锁定角色图；所有角色锁定后自动开放场景资产阶段。"""

    image = lock_character_reference_image(
        db, image_id, reviewer_label=payload.reviewer_label, note=payload.note, quality_score=payload.quality_score
    )
    character = db.get(CharacterDefinition, image.character_id)
    assert character is not None
    return CharacterReferenceImageV1Response(
        id=image.id,
        project_id=image.project_id,
        character_id=character.id,
        character_code=character.character_code,
        character_name=character.name,
        version=image.version,
        asset_version_id=image.asset_version_id,
        image_url=image.image_url,
        generation_status=_enum_value(image.generation_status),
        review_status=_enum_value(image.review_status),
        created_at=image.created_at,
    )


@router.post(
    "/projects/{project_id}/characters/{character_id}/asset-versions/{asset_version_id}/adopt",
    response_model=CharacterReferenceImageV1Response,
    status_code=status.HTTP_201_CREATED,
)
def adopt_character_asset_version_endpoint(
    project_id: str,
    character_id: str,
    asset_version_id: str,
    db: Session = Depends(get_db),
) -> CharacterReferenceImageV1Response:
    """把资产中心角色版本放入项目作为待审核锁图候选，不调用图片模型。"""

    image = adopt_character_asset_version(
        db,
        project_id=project_id,
        character_id=character_id,
        asset_version_id=asset_version_id,
    )
    character = db.get(CharacterDefinition, image.character_id)
    assert character is not None
    return CharacterReferenceImageV1Response(
        id=image.id,
        project_id=image.project_id,
        character_id=character.id,
        character_code=character.character_code,
        character_name=character.name,
        version=image.version,
        asset_version_id=image.asset_version_id,
        image_url=image.image_url,
        generation_status=_enum_value(image.generation_status),
        review_status=_enum_value(image.review_status),
        created_at=image.created_at,
    )


@router.get(
    "/projects/{project_id}/scene-reference-images",
    response_model=list[SceneReferenceImageV1Response],
)
def list_scene_reference_images_endpoint(
    project_id: str, db: Session = Depends(get_db)
) -> list[SceneReferenceImageV1Response]:
    """返回场景图的所有版本，让人工明确选择和锁定版本。"""

    get_project_production_state(db, project_id)
    rows = db.execute(
        select(SceneReferenceImage, SceneDefinition)
        .join(SceneDefinition, SceneReferenceImage.scene_id == SceneDefinition.id)
        .where(SceneReferenceImage.project_id == project_id)
        .order_by(SceneDefinition.scene_code, SceneReferenceImage.version.desc())
    ).all()
    return [
        SceneReferenceImageV1Response(
            id=image.id,
            project_id=image.project_id,
            scene_id=scene.id,
            scene_code=scene.scene_code,
            scene_name=scene.name,
            version=image.version,
            asset_version_id=image.asset_version_id,
            image_url=image.image_url,
            generation_status=_enum_value(image.generation_status),
            review_status=_enum_value(image.review_status),
            created_at=image.created_at,
        )
        for image, scene in rows
    ]


@router.post("/scene-reference-images/{image_id}/lock", response_model=SceneReferenceImageV1Response)
def lock_scene_reference_image_endpoint(
    image_id: str,
    payload: ReviewActionRequest,
    db: Session = Depends(get_db),
) -> SceneReferenceImageV1Response:
    """锁定场景图；所有场景锁定后自动开放导演分镜阶段。"""

    image = lock_scene_reference_image(
        db, image_id, reviewer_label=payload.reviewer_label, note=payload.note, quality_score=payload.quality_score
    )
    scene = db.get(SceneDefinition, image.scene_id)
    assert scene is not None
    return SceneReferenceImageV1Response(
        id=image.id,
        project_id=image.project_id,
        scene_id=scene.id,
        scene_code=scene.scene_code,
        scene_name=scene.name,
        version=image.version,
        asset_version_id=image.asset_version_id,
        image_url=image.image_url,
        generation_status=_enum_value(image.generation_status),
        review_status=_enum_value(image.review_status),
        created_at=image.created_at,
    )


@router.post(
    "/projects/{project_id}/scenes/{scene_id}/asset-versions/{asset_version_id}/adopt",
    response_model=SceneReferenceImageV1Response,
    status_code=status.HTTP_201_CREATED,
)
def adopt_scene_asset_version_endpoint(
    project_id: str,
    scene_id: str,
    asset_version_id: str,
    db: Session = Depends(get_db),
) -> SceneReferenceImageV1Response:
    """把资产中心场景版本放入项目作为待审核锁图候选，不调用图片模型。"""

    image = adopt_scene_asset_version(
        db,
        project_id=project_id,
        scene_id=scene_id,
        asset_version_id=asset_version_id,
    )
    scene = db.get(SceneDefinition, image.scene_id)
    assert scene is not None
    return SceneReferenceImageV1Response(
        id=image.id,
        project_id=image.project_id,
        scene_id=scene.id,
        scene_code=scene.scene_code,
        scene_name=scene.name,
        version=image.version,
        asset_version_id=image.asset_version_id,
        image_url=image.image_url,
        generation_status=_enum_value(image.generation_status),
        review_status=_enum_value(image.review_status),
        created_at=image.created_at,
    )


@router.get("/projects/{project_id}/shot-keyframes", response_model=list[ShotKeyframeV1Response])
def list_shot_keyframes_endpoint(
    project_id: str, db: Session = Depends(get_db)
) -> list[ShotKeyframeV1Response]:
    """返回当前项目的关键帧版本和冻结的基础资产输入快照。"""

    get_project_production_state(db, project_id)
    rows = db.execute(
        select(ShotKeyframe, ShotPlan)
        .join(ShotPlan, ShotKeyframe.shot_id == ShotPlan.id)
        .where(ShotKeyframe.project_id == project_id)
        .order_by(ShotPlan.shot_number, ShotKeyframe.version.desc())
    ).all()
    return [
        ShotKeyframeV1Response(
            id=item.id,
            project_id=item.project_id,
            shot_id=shot.id,
            shot_number=shot.shot_number,
            version=item.version,
            image_url=item.image_url,
            generation_status=_enum_value(item.generation_status),
            review_status=_enum_value(item.review_status),
            input_asset_snapshot=item.input_asset_snapshot,
            created_at=item.created_at,
        )
        for item, shot in rows
    ]


@router.post("/shot-keyframes/{keyframe_id}/lock", response_model=ShotKeyframeV1Response)
def lock_shot_keyframe_endpoint(
    keyframe_id: str,
    payload: ReviewActionRequest,
    db: Session = Depends(get_db),
) -> ShotKeyframeV1Response:
    """锁定关键帧；所有镜头锁定后自动开放 Seedance 视频生成阶段。"""

    item = lock_shot_keyframe(
        db, keyframe_id, reviewer_label=payload.reviewer_label, note=payload.note, quality_score=payload.quality_score
    )
    shot = db.get(ShotPlan, item.shot_id)
    assert shot is not None
    return ShotKeyframeV1Response(
        id=item.id,
        project_id=item.project_id,
        shot_id=shot.id,
        shot_number=shot.shot_number,
        version=item.version,
        image_url=item.image_url,
        generation_status=_enum_value(item.generation_status),
        review_status=_enum_value(item.review_status),
        input_asset_snapshot=item.input_asset_snapshot,
        created_at=item.created_at,
    )


@router.get("/projects/{project_id}/video-clips", response_model=list[VideoClipV1Response])
def list_v1_video_clips_endpoint(
    project_id: str, db: Session = Depends(get_db)
) -> list[VideoClipV1Response]:
    """返回 V1 分镜片段，保留输入资产快照用于审核和问题定位。"""

    get_project_production_state(db, project_id)
    rows = db.execute(
        select(VideoClip, ShotPlan)
        .join(ShotPlan, VideoClip.shot_plan_id == ShotPlan.id)
        .where(VideoClip.project_id == project_id)
        .order_by(ShotPlan.shot_number, VideoClip.version.desc())
    ).all()
    return [_video_clip_response(db, clip, shot) for clip, shot in rows]


@router.post("/video-clips/{clip_id}/approve", response_model=VideoClipV1Response)
def approve_video_clip_endpoint(
    clip_id: str,
    payload: ReviewActionRequest,
    db: Session = Depends(get_db),
) -> VideoClipV1Response:
    """人工通过片段；全部通过后生产台才出现合成入口。"""

    clip = approve_video_clip(
        db, clip_id, reviewer_label=payload.reviewer_label, note=payload.note, quality_score=payload.quality_score
    )
    shot = db.get(ShotPlan, clip.shot_plan_id)
    assert shot is not None
    return _video_clip_response(db, clip, shot)


@router.post("/video-clips/{clip_id}/reject", response_model=VideoClipV1Response)
def reject_video_clip_endpoint(
    clip_id: str,
    payload: ReviewActionRequest,
    db: Session = Depends(get_db),
) -> VideoClipV1Response:
    """人工驳回片段；回到生成阶段并要求创建新视频版本。"""

    clip = reject_video_clip(
        db, clip_id, reviewer_label=payload.reviewer_label, note=payload.note, quality_score=payload.quality_score
    )
    shot = db.get(ShotPlan, clip.shot_plan_id)
    assert shot is not None
    return _video_clip_response(db, clip, shot)


def _masked_provider_task_id(value: Optional[str]) -> Optional[str]:
    """任务号仅用于制作人排查，列表展示时不暴露完整第三方标识。"""

    if not value:
        return None
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-4:]}"


def _video_clip_response(db: Session, clip: VideoClip, shot: ShotPlan) -> VideoClipV1Response:
    """统一输出视频历史、当前采用标识及其子任务状态。"""

    from app.models import WorkflowStep

    task = db.scalars(select(WorkflowStep).where(WorkflowStep.video_clip_id == clip.id)).first()
    return VideoClipV1Response(
        id=clip.id,
        project_id=clip.project_id,
        shot_plan_id=shot.id,
        shot_number=shot.shot_number,
        version=clip.version,
        video_url=clip.video_url,
        provider_task_id=_masked_provider_task_id(clip.provider_task_id),
        task_status=_enum_value(task.status) if task else clip.generation_status,
        is_current=shot.selected_video_clip_id == clip.id,
        generation_status=clip.generation_status,
        review_status=clip.review_status,
        review_note=clip.review_note,
        input_asset_snapshot=clip.input_asset_snapshot,
        created_at=clip.created_at,
    )
