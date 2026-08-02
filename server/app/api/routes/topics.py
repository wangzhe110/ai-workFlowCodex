"""原创选题生成、查看与人工确认接口。"""

from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy.orm import Session

from app.api.routes.projects import _run_response
from app.core.database import get_db
from app.schemas import TopicCandidateResponse, WorkflowRunResponse
from app.services.topic_service import create_topic_generation_run, list_topic_candidates, select_topic_candidate
from app.services.worker_runtime import dispatch_topic_generation


router = APIRouter(prefix="/api/v1", tags=["原创选题"])


def _topic_response(candidate) -> TopicCandidateResponse:
    """统一序列化选题卡片。"""

    return TopicCandidateResponse(
        id=candidate.id,
        project_id=candidate.project_id,
        generation_run_id=candidate.generation_run_id,
        position=candidate.position,
        title=candidate.title,
        opening_hook=candidate.opening_hook,
        synopsis=candidate.synopsis,
        score=candidate.score,
        scoring_notes=candidate.scoring_notes,
        status=candidate.status.value,
        created_at=candidate.created_at,
        updated_at=candidate.updated_at,
    )


@router.post("/projects/{project_id}/topic-generation-runs", response_model=WorkflowRunResponse, status_code=status.HTTP_202_ACCEPTED)
def start_topic_generation_endpoint(
    project_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> WorkflowRunResponse:
    """基于最近完成分析创建原创选题任务。"""

    run = create_topic_generation_run(db, project_id)
    dispatch_topic_generation(background_tasks, run.id)
    return _run_response(run)


@router.get("/projects/{project_id}/topic-candidates", response_model=list[TopicCandidateResponse])
def list_topic_candidates_endpoint(project_id: str, db: Session = Depends(get_db)) -> list[TopicCandidateResponse]:
    """列出项目的历史原创选题候选。"""

    return [_topic_response(candidate) for candidate in list_topic_candidates(db, project_id)]


@router.post("/topic-candidates/{topic_id}/select", response_model=TopicCandidateResponse)
def select_topic_candidate_endpoint(topic_id: str, db: Session = Depends(get_db)) -> TopicCandidateResponse:
    """由用户确认一个候选；后续故事工作流只消费已确认选题。"""

    return _topic_response(select_topic_candidate(db, topic_id))
