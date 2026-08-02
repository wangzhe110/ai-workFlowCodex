"""FastAPI 服务入口。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.routes import creative_library, images, model_profiles, projects, stories, storyboards, topics, videos, workflows
from app.core.config import settings
from app.core.database import engine, init_database
from app.schemas import HealthResponse, ReadinessResponse


@asynccontextmanager
async def lifespan(_: FastAPI):
    """服务启动时初始化开发数据库；关闭时不保留进程级资源。"""

    init_database()
    yield


app = FastAPI(
    title="AI 短剧生产工作流平台 API",
    version="0.1.0",
    description="V1：项目、素材上传、创作资产库和异步工作流接口。",
    lifespan=lifespan,
)

# V1 暂无登录，但 CORS 仍显式限制前端来源，避免开发配置默认允许任意站点调用。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)

app.include_router(projects.router)
app.include_router(workflows.router)
app.include_router(creative_library.router)
app.include_router(topics.router)
app.include_router(stories.router)
app.include_router(storyboards.router)
app.include_router(images.router)
app.include_router(videos.router)
app.include_router(model_profiles.router)


@app.get("/health", response_model=HealthResponse, tags=["系统"])
def health_check() -> HealthResponse:
    """返回进程存活状态，不访问任何外部依赖，供容器重启策略使用。"""

    return HealthResponse(status="ok", service="ai-drama-workflow-api")


@app.get("/ready", response_model=ReadinessResponse, tags=["系统"])
def readiness_check() -> ReadinessResponse:
    """验证 API 已能访问运行所需的数据库，以及 RQ 模式下的 Redis。

    此接口适用于负载均衡器的就绪探针：进程虽然仍能响应 ``/health``，但数据库迁移
    未完成或 Redis 断连时会返回 503，避免新请求被分配到不可用实例。错误信息保持抽象，
    不把连接地址、账号或底层堆栈暴露给公网。
    """

    dependencies: dict[str, str] = {}
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        dependencies["database"] = "ok"
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="数据库暂不可用") from exc

    if settings.task_execution_mode == "rq":
        _assert_redis_ready()
        dependencies["redis"] = "ok"

    return ReadinessResponse(
        status="ok",
        service="ai-drama-workflow-api",
        dependencies=dependencies,
    )


def _assert_redis_ready() -> None:
    """以短超时检查 RQ 所依赖 Redis，失败时仅返回安全的统一错误。"""

    if not settings.redis_url:
        raise HTTPException(status_code=503, detail="任务队列暂不可用")
    client = None
    try:
        from redis import Redis

        client = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        if not client.ping():
            raise RuntimeError("Redis ping 未返回成功")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="任务队列暂不可用") from exc
    finally:
        if client is not None:
            client.close()
