"""工作流任务投递与独立 Worker 执行边界。

开发环境使用 FastAPI ``BackgroundTasks``，无需 Redis 即可联调；生产可设置
``TASK_EXECUTION_MODE=rq``，由 Redis/RQ Worker 消费同一批执行函数。路由层只调用
本模块，不需要知道任务到底在 API 进程还是独立 Worker 中运行。
"""

from datetime import datetime, timezone
from typing import Callable

from fastapi import BackgroundTasks
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import CommerceWorkflowLink, CommerceWorkflowStep, RunStatus, StoryRun, StoryRunStatus, WorkflowRun, WorkflowStep
from app.services.image_service import execute as execute_images
from app.services.final_video_service import execute_final_video_export
from app.services.story_service import execute_story_generation
from app.services.storyboard_service import execute as execute_storyboard
from app.services.topic_service import execute_topic_generation
from app.services.video_service import execute_video_generation
from app.services.v1_execution_service import execute_v1_video_child, execute_v1_workflow
from app.services.commerce_workflow_service import execute_commerce_workflow
from app.services.workflow_service import execute_video_analysis


WorkflowExecutor = Callable[[str], None]

_WORKFLOW_EXECUTORS: dict[str, WorkflowExecutor] = {
    "video_analysis": execute_video_analysis,
    "topic_generation": execute_topic_generation,
    "story_generation": execute_story_generation,
    "storyboard_generation": execute_storyboard,
    "image_generation": execute_images,
    "video_generation": execute_video_generation,
    "final_video_export": execute_final_video_export,
    "v1_reference_analysis": execute_v1_workflow,
    "v1_story_generation": execute_v1_workflow,
    "v1_character_design": execute_v1_workflow,
    "v1_character_images": execute_v1_workflow,
    "v1_scene_design": execute_v1_workflow,
    "v1_scene_images": execute_v1_workflow,
    "v1_director_plan": execute_v1_workflow,
    "v1_shot_keyframes": execute_v1_workflow,
    "v1_video_generation": execute_v1_workflow,
    "v1_final_compose": execute_v1_workflow,
    # Commerce 使用一个 StoryRun 父运行；节点与 retry 是其 WorkflowStep attempt，
    # 因而队列只需要投递这一种工作流键。
    "commerce_story_run": execute_commerce_workflow,
}


def dispatch_video_analysis(background_tasks: BackgroundTasks, run_id: str) -> None:
    """兼容首个路由入口；实际投递统一交给通用分发函数。"""

    dispatch_workflow(background_tasks, "video_analysis", run_id)


def dispatch_topic_generation(background_tasks: BackgroundTasks, run_id: str) -> None:
    """投递原创选题生成任务。"""

    dispatch_workflow(background_tasks, "topic_generation", run_id)


def dispatch_story_generation(background_tasks: BackgroundTasks, run_id: str) -> None:
    """投递故事包生成任务。"""

    dispatch_workflow(background_tasks, "story_generation", run_id)


def dispatch_workflow(background_tasks: BackgroundTasks, workflow_key: str, run_id: str) -> None:
    """按运行模式投递工作流，未知模式或投递失败都保留可重试失败记录。"""

    _executor_for(workflow_key)
    if settings.task_execution_mode == "inline":
        background_tasks.add_task(execute_workflow_job, workflow_key, run_id)
        return
    if settings.task_execution_mode == "rq":
        try:
            _enqueue_rq_job(workflow_key, run_id)
        except Exception as exc:
            _mark_dispatch_failure(run_id, f"无法投递 Redis Worker：{exc}")
            raise RuntimeError("无法投递 Redis Worker；请检查 REDIS_URL、RQ Worker 与网络") from exc
        return
    _mark_dispatch_failure(run_id, f"未知 TASK_EXECUTION_MODE：{settings.task_execution_mode}")
    raise RuntimeError("TASK_EXECUTION_MODE 仅支持 inline 或 rq")


def dispatch_v1_video_children(background_tasks: BackgroundTasks, run_id: str) -> None:
    """按镜头投递独立视频 Job，父 WorkflowRun 仅聚合子任务终态。

    这里不投递一个可能等待数十分钟的串行父 Job。每个子任务有自己的
    ``provider_task_id``，Worker 重启后可安全恢复轮询已存在的供应商任务。
    """

    db: Session = SessionLocal()
    try:
        run = db.get(WorkflowRun, run_id)
        if run is None:
            raise RuntimeError("视频工作流不存在")
        steps = [step for step in run.steps if step.step_key == "VIDEO_SHOT" and step.status == RunStatus.PENDING]
        if settings.task_execution_mode == "inline":
            for step in steps:
                background_tasks.add_task(execute_v1_video_child, run.id, step.id)
            return
        if settings.task_execution_mode == "rq":
            for step in steps:
                _enqueue_v1_video_child(run.id, step.id)
            return
        raise RuntimeError("TASK_EXECUTION_MODE 仅支持 inline 或 rq")
    except Exception as exc:
        _mark_dispatch_failure(run_id, f"无法投递视频镜头子任务：{exc}")
        raise
    finally:
        db.close()


def execute_workflow_job(workflow_key: str, run_id: str) -> None:
    """独立 RQ Worker 和本地 BackgroundTask 共用的实际任务入口。

    所有领域执行函数都自行创建数据库会话并处理模型异常，因此队列层只负责路由，
    不应重复开启事务或尝试解析模型错误。RQ 会记录进程级异常；业务级失败仍保存
    到 ``workflow_runs`` 和 ``workflow_steps``，供前端统一轮询。
    """

    _executor_for(workflow_key)(run_id)


def _executor_for(workflow_key: str) -> WorkflowExecutor:
    """解析工作流键到明确执行函数，防止队列任务被误投递到错误流程。"""

    executor = _WORKFLOW_EXECUTORS.get(workflow_key)
    if executor is None:
        raise RuntimeError(f"暂不支持投递的工作流：{workflow_key}")
    return executor


def _enqueue_rq_job(workflow_key: str, run_id: str) -> None:
    """按需导入 RQ/Redis 并入队，未启用生产模式时不增加本地开发依赖。"""

    if not settings.redis_url:
        raise RuntimeError("TASK_EXECUTION_MODE=rq 需要 REDIS_URL")
    try:
        from redis import Redis
        from rq import Queue
    except ImportError as exc:
        raise RuntimeError("RQ Worker 依赖未安装；请按 server/requirements.txt 安装") from exc
    queue = Queue(settings.rq_queue_name, connection=Redis.from_url(settings.redis_url))
    queue.enqueue(
        execute_workflow_job,
        workflow_key,
        run_id,
        job_timeout=settings.worker_job_timeout_seconds,
        result_ttl=0,
        failure_ttl=7 * 24 * 60 * 60,
    )


def _enqueue_v1_video_child(run_id: str, step_id: str) -> None:
    """视频单镜头任务使用独立 RQ Job，避免一个 Job 超过 Worker 超时。"""

    if not settings.redis_url:
        raise RuntimeError("TASK_EXECUTION_MODE=rq 需要 REDIS_URL")
    try:
        from redis import Redis
        from rq import Queue
    except ImportError as exc:
        raise RuntimeError("RQ Worker 依赖未安装；请按 server/requirements.txt 安装") from exc
    queue = Queue(settings.rq_queue_name, connection=Redis.from_url(settings.redis_url))
    queue.enqueue(
        execute_v1_video_child,
        run_id,
        step_id,
        job_timeout=settings.worker_job_timeout_seconds,
        result_ttl=0,
        failure_ttl=7 * 24 * 60 * 60,
    )


def _mark_dispatch_failure(run_id: str, message: str) -> None:
    """投递阶段失败时将待执行运行改为失败，使前端能够恢复并执行重试。"""

    db: Session = SessionLocal()
    try:
        run = db.get(WorkflowRun, run_id)
        if run is None or run.status != RunStatus.PENDING:
            return
        now = datetime.now(timezone.utc)
        run.status = RunStatus.FAILED
        run.finished_at = now
        for step in run.steps:
            if step.status == RunStatus.PENDING:
                step.status = RunStatus.FAILED
                # 先刷新真实 WorkflowStep，再同步 Commerce sidecar。0012 的数据库
                # 触发器和 Base.metadata 测试库都因此观察同一个事务内的同一状态。
                db.flush()
                db.execute(
                    update(CommerceWorkflowStep)
                    .where(CommerceWorkflowStep.workflow_step_id == step.id)
                    .values(status=RunStatus.FAILED.value)
                )
                step.error_message = message[:2000]
                step.finished_at = now
        # Commerce 运行拥有独立状态机；队列投递失败不能只让 WorkflowRun 失败而把
        # StoryRun 留在 RUNNING，从而失去明确的 retry 入口。
        link = db.get(CommerceWorkflowLink, run.id)
        if link is not None:
            story_run = db.get(StoryRun, link.story_run_id)
            if story_run is not None and story_run.state.status != StoryRunStatus.CANCELLED:
                story_run.state.status = StoryRunStatus.FAILED
                story_run.state.stage_data = {
                    "blocked_reason": "dispatch_failed",
                    "failed_workflow_run_id": run.id,
                }
        db.commit()
    finally:
        db.close()
