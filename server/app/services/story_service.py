"""故事生成工作流：从人工确认选题产出可审核、可追溯的故事包。"""

from datetime import datetime, timezone
import time
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import RunStatus, StoryPackage, StoryStatus, TopicCandidate, TopicStatus, WorkflowRun, WorkflowStep
from app.services.analysis_provider import (
    MockStoryGenerationProvider,
    OpenAICompatibleJsonProvider,
    StoryGenerationInput,
)
from app.services.model_profile_service import get_active_profile_snapshot
from app.services.sensitive_data import sanitize_error_summary
from app.services.workflow_service import get_project_or_404, get_workflow_run_or_404


STORY_WORKFLOW = "story_generation"
STORY_STEP = "generate_story_package"


def _now() -> datetime:
    """统一由服务层生成 UTC 时间，便于不同 Worker 的审计排序。"""

    return datetime.now(timezone.utc)


def _selected_topic(db: Session, project_id: str) -> TopicCandidate:
    """只允许人工确认过的唯一选题进入故事创作。"""

    topic = db.scalars(
        select(TopicCandidate).where(
            TopicCandidate.project_id == project_id,
            TopicCandidate.status == TopicStatus.SELECTED,
        )
    ).first()
    if topic is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="请先人工确认一个原创选题")
    return topic


def create_story_run(db: Session, project_id: str) -> WorkflowRun:
    """冻结已确认选题与活动模型配置，创建待执行的故事任务。"""

    get_project_or_404(db, project_id)
    topic = _selected_topic(db, project_id)
    run = WorkflowRun(project_id=project_id, workflow_key=STORY_WORKFLOW)
    WorkflowStep(
        workflow_run=run,
        step_key=STORY_STEP,
        position=1,
        input_payload={
            "topic": {
                "id": topic.id,
                "title": topic.title,
                "opening_hook": topic.opening_hook,
                "synopsis": topic.synopsis,
            }
        },
        model_profile_snapshot=get_active_profile_snapshot(db, STORY_STEP),
    )
    db.add(run)
    db.commit()
    return get_workflow_run_or_404(db, run.id)


def _external_story_package(step: WorkflowStep) -> dict[str, Any]:
    """调用真实文本模型，并将返回内容约束为故事包稳定契约。"""

    response = OpenAICompatibleJsonProvider(step.model_profile_snapshot or {}).generate_json(
        system_instruction=(
            "你是短剧编剧。根据确认的原创选题，写出新的故事包。不得复刻或引用"
            "参考视频的具体人物、台词、画面、音乐、镜头或剧情；保持开场冲突和"
            "段落悬念等抽象机制即可。"
        ),
        user_payload=step.input_payload,
        output_contract=(
            '{"title":"string","premise":"string","outline":[{"act":"string","content":"string"}],'
            '"roles":[{"name":"string","role":"string","goal":"string","conflict":"string"}],'
            '"scenes":[{"name":"string","purpose":"string"}]}'
        ),
    )
    if not isinstance(response, dict):
        raise RuntimeError("故事模型返回不是 JSON 对象")
    try:
        result = {
            "title": str(response["title"]).strip()[:180],
            "premise": str(response["premise"]).strip(),
            "outline": response["outline"],
            "roles": response["roles"],
            "scenes": response["scenes"],
        }
    except (KeyError, TypeError) as exc:
        raise RuntimeError("故事模型返回缺少必要字段") from exc
    if not result["title"] or not result["premise"] or not all(
        isinstance(result[key], list) and result[key] for key in ("outline", "roles", "scenes")
    ):
        raise RuntimeError("故事模型返回的故事包为空或格式无效")
    return result


def execute_story_generation(run_id: str) -> None:
    """Worker 生成一版故事包；任何模型错误都持久化为可重试状态。"""

    db = SessionLocal()
    try:
        run = get_workflow_run_or_404(db, run_id)
        if run.workflow_key != STORY_WORKFLOW or run.status != RunStatus.PENDING:
            return

        step = run.steps[0]
        run.status = RunStatus.RUNNING
        run.started_at = _now()
        step.status = RunStatus.RUNNING
        step.started_at = _now()
        step.attempt += 1
        db.commit()

        for progress in (25, 60, 85):
            time.sleep(settings.simulated_step_delay_seconds)
            step.progress = progress
            db.commit()

        if (step.model_profile_snapshot or {}).get("provider_key") == "mock_provider":
            result = MockStoryGenerationProvider().generate(
                StoryGenerationInput(topic=step.input_payload["topic"])
            )
        else:
            result = _external_story_package(step)

        package = StoryPackage(
            project_id=run.project_id,
            topic_candidate_id=step.input_payload["topic"]["id"],
            generation_run_id=run.id,
            **result,
        )
        db.add(package)
        db.flush()
        step.output_payload = {"story_package_id": package.id}
        step.progress = 100
        step.status = RunStatus.SUCCEEDED
        step.finished_at = _now()
        run.status = RunStatus.SUCCEEDED
        run.finished_at = _now()
        db.commit()
    except Exception as exc:
        db.rollback()
        error_message = sanitize_error_summary(exc, max_length=2000)
        run = db.get(WorkflowRun, run_id)
        if run is not None:
            run.status = RunStatus.FAILED
            run.finished_at = _now()
            step = db.scalars(
                select(WorkflowStep).where(WorkflowStep.workflow_run_id == run_id)
            ).first()
            if step is not None:
                step.status = RunStatus.FAILED
                step.error_message = error_message
                step.finished_at = _now()
            db.commit()
    finally:
        db.close()


def list_story_packages(db: Session, project_id: str) -> list[StoryPackage]:
    """按生成时间倒序读取当前项目的故事版本历史。"""

    get_project_or_404(db, project_id)
    statement = (
        select(StoryPackage)
        .where(StoryPackage.project_id == project_id)
        .order_by(StoryPackage.created_at.desc())
    )
    return list(db.scalars(statement).all())


def confirm_story_package(db: Session, package_id: str) -> StoryPackage:
    """人工确认一个故事版本，同时撤销项目内旧的确认版本。"""

    package = db.get(StoryPackage, package_id)
    if package is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="故事包不存在")
    for item in db.scalars(
        select(StoryPackage).where(
            StoryPackage.project_id == package.project_id,
            StoryPackage.status == StoryStatus.CONFIRMED,
        )
    ):
        item.status = StoryStatus.DRAFT
    package.status = StoryStatus.CONFIRMED
    db.commit()
    db.refresh(package)
    return package
