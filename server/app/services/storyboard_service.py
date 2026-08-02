"""分镜细纲生成工作流：使用确认故事，产生可审阅的镜头快照。"""

from datetime import datetime, timezone
import time
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import RunStatus, StoryPackage, StoryStatus, StoryboardPackage, StoryboardStatus, WorkflowRun, WorkflowStep
from app.services.analysis_provider import (
    MockStoryboardGenerationProvider,
    OpenAICompatibleJsonProvider,
    StoryboardGenerationInput,
)
from app.services.model_profile_service import get_active_profile_snapshot
from app.services.workflow_service import get_project_or_404, get_workflow_run_or_404


KEY = "storyboard_generation"
STEP = "generate_storyboard"


def now() -> datetime:
    """统一由服务层生成 UTC 时间。"""

    return datetime.now(timezone.utc)


def selected_story(db: Session, project_id: str) -> StoryPackage:
    """视频、图片和分镜只能消费人工确认的故事包。"""

    story = db.scalars(
        select(StoryPackage).where(
            StoryPackage.project_id == project_id,
            StoryPackage.status == StoryStatus.CONFIRMED,
        )
    ).first()
    if story is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="请先人工确认一个故事包")
    return story


def create_run(db: Session, project_id: str, shot_count: int) -> WorkflowRun:
    """冻结完整故事包、镜头数与活动模型配置，防止生成中途被修改。"""

    get_project_or_404(db, project_id)
    story = selected_story(db, project_id)
    run = WorkflowRun(project_id=project_id, workflow_key=KEY)
    WorkflowStep(
        workflow_run=run,
        step_key=STEP,
        position=1,
        input_payload={
            "story": {
                "id": story.id,
                "title": story.title,
                "premise": story.premise,
                "outline": story.outline,
                "roles": story.roles,
                "scenes": story.scenes,
            },
            "shot_count": shot_count,
        },
        model_profile_snapshot=get_active_profile_snapshot(db, STEP),
    )
    db.add(run)
    db.commit()
    return get_workflow_run_or_404(db, run.id)


def _external_storyboard_shots(step: WorkflowStep) -> list[dict[str, Any]]:
    """调用真实文本模型生成统一镜头字段，并校验镜头数与顺序。"""

    response = OpenAICompatibleJsonProvider(step.model_profile_snapshot or {}).generate_json(
        system_instruction=(
            "你是短剧分镜导演。为已确认的原创故事生成可制作的连续分镜。不得"
            "复刻参考视频的具体画面、人物、台词、音乐或镜头，只可沿用抽象的"
            "开场冲突、节奏和悬念机制。图片与视频提示词必须保持原创并突出角色、"
            "场景和动作连续性。"
        ),
        user_payload=step.input_payload,
        output_contract=(
            '{"shots":[{"number":1,"duration_seconds":3,"scene":"string","visual":"string",'
            '"dialogue_or_voiceover":"string","camera":"string","image_prompt":"string",'
            '"video_prompt":"string"}]}；shots 必须恰好等于 shot_count，并按 number 从 1 连续编号。'
        ),
    )
    shots = response.get("shots") if isinstance(response, dict) else None
    expected_count = int(step.input_payload["shot_count"])
    if not isinstance(shots, list) or len(shots) != expected_count:
        raise RuntimeError("分镜模型返回的镜头数量与请求不一致")

    fields = (
        "scene",
        "visual",
        "dialogue_or_voiceover",
        "camera",
        "image_prompt",
        "video_prompt",
    )
    normalized: list[dict[str, Any]] = []
    for expected_number, shot in enumerate(shots, start=1):
        if not isinstance(shot, dict):
            raise RuntimeError("分镜模型返回的镜头格式无效")
        try:
            if int(shot["number"]) != expected_number:
                raise RuntimeError("分镜模型返回的镜头编号不连续")
            duration = int(shot["duration_seconds"])
            if not 1 <= duration <= 30:
                raise RuntimeError("分镜时长必须在 1 到 30 秒之间")
            normalized.append(
                {
                    "number": expected_number,
                    "duration_seconds": duration,
                    **{field: str(shot[field]).strip() for field in fields},
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("分镜模型返回缺少必要字段") from exc
    if any(not shot[field] for shot in normalized for field in fields):
        raise RuntimeError("分镜模型返回了空镜头字段")
    return normalized


def execute(run_id: str) -> None:
    """Worker 生成一版分镜；模型故障会变为前端可重试的任务失败。"""

    db = SessionLocal()
    try:
        run = get_workflow_run_or_404(db, run_id)
        if run.workflow_key != KEY or run.status != RunStatus.PENDING:
            return

        step = run.steps[0]
        run.status = RunStatus.RUNNING
        run.started_at = now()
        step.status = RunStatus.RUNNING
        step.started_at = now()
        step.attempt += 1
        db.commit()

        for progress in (25, 60, 85):
            time.sleep(settings.simulated_step_delay_seconds)
            step.progress = progress
            db.commit()

        if (step.model_profile_snapshot or {}).get("provider_key") == "mock_provider":
            shots = MockStoryboardGenerationProvider().generate(
                StoryboardGenerationInput(**step.input_payload)
            )
        else:
            shots = _external_storyboard_shots(step)

        package = StoryboardPackage(
            project_id=run.project_id,
            story_package_id=step.input_payload["story"]["id"],
            generation_run_id=run.id,
            target_shot_count=step.input_payload["shot_count"],
            shots=shots,
        )
        db.add(package)
        db.flush()
        step.output_payload = {"storyboard_package_id": package.id, "shot_count": len(shots)}
        step.progress = 100
        step.status = RunStatus.SUCCEEDED
        step.finished_at = now()
        run.status = RunStatus.SUCCEEDED
        run.finished_at = now()
        db.commit()
    except Exception as exc:
        db.rollback()
        run = db.get(WorkflowRun, run_id)
        if run is not None:
            run.status = RunStatus.FAILED
            run.finished_at = now()
            step = db.scalars(
                select(WorkflowStep).where(WorkflowStep.workflow_run_id == run_id)
            ).first()
            if step is not None:
                step.status = RunStatus.FAILED
                step.error_message = str(exc)[:2000]
                step.finished_at = now()
            db.commit()
    finally:
        db.close()


def list_packages(db: Session, project_id: str) -> list[StoryboardPackage]:
    """按创建时间倒序返回项目所有分镜版本。"""

    get_project_or_404(db, project_id)
    statement = (
        select(StoryboardPackage)
        .where(StoryboardPackage.project_id == project_id)
        .order_by(StoryboardPackage.created_at.desc())
    )
    return list(db.scalars(statement).all())


def confirm(db: Session, package_id: str) -> StoryboardPackage:
    """人工确认一个分镜版本，并撤销同项目旧的确认分镜。"""

    package = db.get(StoryboardPackage, package_id)
    if package is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分镜包不存在")
    for item in db.scalars(
        select(StoryboardPackage).where(
            StoryboardPackage.project_id == package.project_id,
            StoryboardPackage.status == StoryboardStatus.CONFIRMED,
        )
    ):
        item.status = StoryboardStatus.DRAFT
    package.status = StoryboardStatus.CONFIRMED
    db.commit()
    db.refresh(package)
    return package
