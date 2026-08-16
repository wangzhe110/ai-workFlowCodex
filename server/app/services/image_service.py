"""已确认分镜的图片生成与单镜版本管理。"""

from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import ImageStatus, RunStatus, StoryboardImage, StoryboardPackage, StoryboardStatus, WorkflowRun, WorkflowStep
from app.services.analysis_provider import OpenAICompatibleImageProvider
from app.services.model_profile_service import get_active_profile_snapshot
from app.services.sensitive_data import sanitize_error_summary
from app.services.storage import generated_image_delivery
from app.services.workflow_service import get_project_or_404, get_workflow_run_or_404


KEY = "image_generation"
STEP = "generate_storyboard_images"


def now() -> datetime:
    """统一由服务层记录 UTC 时间。"""

    return datetime.now(timezone.utc)


def selected_board(db: Session, project_id: str) -> StoryboardPackage:
    """图片和视频生成只能读取人工确认的分镜包。"""

    board = db.scalars(
        select(StoryboardPackage).where(
            StoryboardPackage.project_id == project_id,
            StoryboardPackage.status == StoryboardStatus.CONFIRMED,
        )
    ).first()
    if board is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="请先人工确认一个分镜包")
    return board


def run(
    db: Session,
    project_id: str,
    shot_numbers: Optional[list[int]] = None,
) -> WorkflowRun:
    """创建全量或单镜图片任务，并冻结确认分镜和活动模型配置。"""

    get_project_or_404(db, project_id)
    board = selected_board(db, project_id)
    requested_numbers = shot_numbers or [shot["number"] for shot in board.shots]
    shots = [shot for shot in board.shots if shot["number"] in requested_numbers]
    if not shots:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="未找到指定镜头")

    workflow_run = WorkflowRun(project_id=project_id, workflow_key=KEY)
    WorkflowStep(
        workflow_run=workflow_run,
        step_key=STEP,
        position=1,
        input_payload={"storyboard_package_id": board.id, "shots": shots},
        model_profile_snapshot=get_active_profile_snapshot(db, STEP),
    )
    db.add(workflow_run)
    db.commit()
    return get_workflow_run_or_404(db, workflow_run.id)


def mock_url(shot: dict) -> str:
    """本地联调 SVG 占位图；真实适配器返回第三方托管 URL 或 Base64 图片。"""

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="576" height="1024">'
        '<rect width="100%" height="100%" fill="#172554"/>'
        f'<text x="48" y="110" fill="white" font-size="36">第 {shot["number"]} 镜</text>'
        f'<text x="48" y="180" fill="#bfdbfe" font-size="22">{shot["scene"]}</text>'
        "</svg>"
    )
    return "data:image/svg+xml," + quote(svg)


def execute(run_id: str) -> None:
    """由 Worker 为每个镜头生成图片，成功镜头不受其他镜头版本影响。"""

    db = SessionLocal()
    try:
        workflow_run = get_workflow_run_or_404(db, run_id)
        if workflow_run.workflow_key != KEY or workflow_run.status != RunStatus.PENDING:
            return

        step = workflow_run.steps[0]
        workflow_run.status = RunStatus.RUNNING
        workflow_run.started_at = now()
        step.status = RunStatus.RUNNING
        step.started_at = now()
        step.attempt += 1
        db.commit()

        snapshot = step.model_profile_snapshot or {}
        external_provider = None
        if snapshot.get("provider_key") != "mock_provider":
            external_provider = OpenAICompatibleImageProvider(snapshot)

        image_ids: list[str] = []
        shots = step.input_payload["shots"]
        for index, shot in enumerate(shots, start=1):
            latest_version = db.scalar(
                select(func.max(StoryboardImage.version)).where(
                    StoryboardImage.storyboard_package_id == step.input_payload["storyboard_package_id"],
                    StoryboardImage.shot_number == shot["number"],
                )
            ) or 0
            version = latest_version + 1
            if external_provider is None:
                image_url = mock_url(shot)
            else:
                provider_image_url = external_provider.generate(shot["image_prompt"])
                # 生产可将中转站临时 URL 立即转存到自有 S3/MinIO；默认 direct 模式
                # 保持原 URL，避免本地演示引入对象存储依赖。
                image_url = generated_image_delivery.persist(
                    project_id=workflow_run.project_id,
                    storyboard_package_id=step.input_payload["storyboard_package_id"],
                    shot_number=shot["number"],
                    version=version,
                    source_url=provider_image_url,
                )
            image = StoryboardImage(
                project_id=workflow_run.project_id,
                storyboard_package_id=step.input_payload["storyboard_package_id"],
                generation_run_id=workflow_run.id,
                shot_number=shot["number"],
                version=version,
                prompt=shot["image_prompt"],
                image_url=image_url,
                status=ImageStatus.SUCCEEDED,
            )
            db.add(image)
            db.flush()
            image_ids.append(image.id)
            step.progress = int(index / len(shots) * 100)
            db.commit()

        step.output_payload = {"image_ids": image_ids}
        step.status = RunStatus.SUCCEEDED
        step.finished_at = now()
        workflow_run.status = RunStatus.SUCCEEDED
        workflow_run.finished_at = now()
        db.commit()
    except Exception as exc:
        db.rollback()
        error_message = sanitize_error_summary(exc, max_length=2000)
        workflow_run = db.get(WorkflowRun, run_id)
        if workflow_run is not None:
            workflow_run.status = RunStatus.FAILED
            workflow_run.finished_at = now()
            step = db.scalars(
                select(WorkflowStep).where(WorkflowStep.workflow_run_id == run_id)
            ).first()
            if step is not None:
                step.status = RunStatus.FAILED
                step.error_message = error_message
                step.finished_at = now()
            db.commit()
    finally:
        db.close()


def list_images(db: Session, project_id: str) -> list[StoryboardImage]:
    """按镜头号和版本倒序读取所有历史图片。"""

    get_project_or_404(db, project_id)
    statement = (
        select(StoryboardImage)
        .where(StoryboardImage.project_id == project_id)
        .order_by(StoryboardImage.shot_number, StoryboardImage.version.desc())
    )
    return list(db.scalars(statement).all())
