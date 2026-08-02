"""原创选题生成工作流：复用抽象分析，不复刻原始参考内容。"""

from datetime import datetime, timezone
import time
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import CreativeLibraryItem, Project, RunStatus, TopicCandidate, TopicStatus, WorkflowRun, WorkflowStep
from app.services.analysis_provider import (
    MockTopicGenerationProvider,
    OpenAICompatibleJsonProvider,
    TopicGenerationInput,
)
from app.services.model_profile_service import get_active_profile_snapshot
from app.services.workflow_service import get_project_or_404, get_workflow_run_or_404


TOPIC_GENERATION_WORKFLOW = "topic_generation"
TOPIC_GENERATION_STEP = "generate_original_topics"


def _external_topic_candidates(step: WorkflowStep) -> list[dict]:
    """调用真实文本模型生成原创候选，并过滤成数据库允许的稳定字段。"""

    response = OpenAICompatibleJsonProvider(step.model_profile_snapshot or {}).generate_json(
        system_instruction=(
            "你是短剧原创策划。只能提炼并运用抽象叙事机制，严禁复刻参考素材的"
            "具体人物、台词、画面、音乐、镜头或剧情。候选必须是新的原创表达。"
        ),
        user_payload=step.input_payload,
        output_contract=(
            '{"candidates":[{"title":"string","opening_hook":"string","synopsis":"string",'
            '"score":0-100,"scoring_notes":"string"}]}；返回 3 个候选。'
        ),
    )
    candidates = response.get("candidates") if isinstance(response, dict) else None
    if not isinstance(candidates, list) or not candidates:
        raise RuntimeError("选题模型返回缺少 candidates 数组")
    normalized: list[dict] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise RuntimeError("选题模型返回的候选格式无效")
        try:
            score = candidate.get("score")
            normalized.append(
                {
                    "title": str(candidate["title"]).strip()[:180],
                    "opening_hook": str(candidate["opening_hook"]).strip(),
                    "synopsis": str(candidate["synopsis"]).strip(),
                    "score": int(score) if score is not None else None,
                    "scoring_notes": str(candidate.get("scoring_notes") or "").strip() or None,
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("选题模型返回缺少必要字段") from exc
    return normalized


def utcnow() -> datetime:
    """保存统一 UTC 时间，保持任务日志可比较。"""

    return datetime.now(timezone.utc)


def _latest_analysis_step(db: Session, project_id: str) -> Optional[WorkflowStep]:
    """找到最近成功的视频分析结果，作为选题的可审计输入。"""

    statement = (
        select(WorkflowStep)
        .join(WorkflowRun, WorkflowStep.workflow_run_id == WorkflowRun.id)
        .where(
            WorkflowRun.project_id == project_id,
            WorkflowRun.workflow_key == "video_analysis",
            WorkflowRun.status == RunStatus.SUCCEEDED,
            WorkflowStep.step_key == "analyze_reference_mechanisms",
            WorkflowStep.status == RunStatus.SUCCEEDED,
        )
        .order_by(WorkflowRun.finished_at.desc())
    )
    return db.scalars(statement).first()


def create_topic_generation_run(db: Session, project_id: str) -> WorkflowRun:
    """创建选题运行，并把分析与资产库冻结为本次输入快照。"""

    project = get_project_or_404(db, project_id)
    analysis_step = _latest_analysis_step(db, project_id)
    if analysis_step is None or analysis_step.output_payload is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="请先完成视频分析，再生成原创选题")

    library_items = list(
        db.scalars(
            select(CreativeLibraryItem)
            .where(CreativeLibraryItem.is_active.is_(True))
            .order_by(CreativeLibraryItem.updated_at.desc())
        ).all()
    )
    library_snapshot = [
        {"id": item.id, "kind": item.kind.value, "title": item.title, "content": item.content, "tags": item.tags}
        for item in library_items
    ]
    run = WorkflowRun(project_id=project_id, workflow_key=TOPIC_GENERATION_WORKFLOW)
    WorkflowStep(
        workflow_run=run,
        step_key=TOPIC_GENERATION_STEP,
        position=1,
        input_payload={
            "analysis_snapshot": analysis_step.output_payload,
            "creative_direction": project.description or "",
            "library_snapshot": library_snapshot,
        },
        model_profile_snapshot=get_active_profile_snapshot(db, TOPIC_GENERATION_STEP),
    )
    db.add(run)
    db.commit()
    return get_workflow_run_or_404(db, run.id)


def execute_topic_generation(run_id: str) -> None:
    """Worker 执行选题生成并持久化候选卡片。"""

    db = SessionLocal()
    try:
        run = get_workflow_run_or_404(db, run_id)
        if run.workflow_key != TOPIC_GENERATION_WORKFLOW or run.status != RunStatus.PENDING:
            return
        step = run.steps[0]
        run.status = RunStatus.RUNNING
        run.started_at = utcnow()
        step.status = RunStatus.RUNNING
        step.started_at = utcnow()
        step.attempt += 1
        db.commit()
        for progress in (20, 55, 80):
            time.sleep(settings.simulated_step_delay_seconds)
            step.progress = progress
            db.commit()
        if (step.model_profile_snapshot or {}).get("provider_key") == "mock_provider":
            candidates = MockTopicGenerationProvider().generate(
                TopicGenerationInput(
                    creative_direction=step.input_payload["creative_direction"],
                    analysis_snapshot=step.input_payload["analysis_snapshot"],
                    library_snapshot=step.input_payload["library_snapshot"],
                )
            )
        else:
            candidates = _external_topic_candidates(step)
        records = [
            TopicCandidate(project_id=run.project_id, generation_run_id=run.id, position=index, **candidate)
            for index, candidate in enumerate(candidates, start=1)
        ]
        db.add_all(records)
        db.flush()
        step.output_payload = {"candidate_ids": [record.id for record in records], "count": len(records)}
        step.progress = 100
        step.status = RunStatus.SUCCEEDED
        step.finished_at = utcnow()
        run.status = RunStatus.SUCCEEDED
        run.finished_at = utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        run = db.get(WorkflowRun, run_id)
        if run is not None:
            run.status = RunStatus.FAILED
            run.finished_at = utcnow()
            step = db.scalars(select(WorkflowStep).where(WorkflowStep.workflow_run_id == run_id)).first()
            if step is not None:
                step.status = RunStatus.FAILED
                step.error_message = str(exc)[:2000]
                step.finished_at = utcnow()
            db.commit()
    finally:
        db.close()


def list_topic_candidates(db: Session, project_id: str) -> list[TopicCandidate]:
    """按最近生成时间和位置返回项目全部选题候选。"""

    get_project_or_404(db, project_id)
    return list(
        db.scalars(
            select(TopicCandidate)
            .where(TopicCandidate.project_id == project_id)
            .order_by(TopicCandidate.created_at.desc(), TopicCandidate.position.asc())
        ).all()
    )


def select_topic_candidate(db: Session, topic_id: str) -> TopicCandidate:
    """人工确认一个选题，并撤销该项目已有的唯一确认项。"""

    candidate = db.get(TopicCandidate, topic_id)
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="选题候选不存在")
    selected = db.scalars(
        select(TopicCandidate).where(TopicCandidate.project_id == candidate.project_id, TopicCandidate.status == TopicStatus.SELECTED)
    ).all()
    for item in selected:
        item.status = TopicStatus.DRAFT
    candidate.status = TopicStatus.SELECTED
    db.commit()
    db.refresh(candidate)
    return candidate
