"""运行配置。

配置只允许由部署环境注入，避免把 API Key、数据库密码等生产密钥写入
Python 文件、前端包或数据库普通字段。这里的默认值仅服务于本地演示。
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


# 服务端配置文件位于工程根目录；部署环境变量优先级更高，.env 仅用于本地开发。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _csv(value: str) -> list[str]:
    """把逗号分隔的跨域白名单转为干净列表。"""

    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """不可变运行配置，防止请求过程中被业务代码意外修改。"""

    database_url: str
    database_schema_mode: str
    local_storage_path: Path
    source_video_storage_mode: str
    task_execution_mode: str
    cors_origins: list[str]
    simulated_step_delay_seconds: float
    max_upload_bytes: int
    generated_image_delivery_mode: str
    generated_image_max_bytes: int
    generated_image_download_timeout_seconds: float
    final_video_delivery_mode: str
    s3_endpoint_url: Optional[str]
    s3_bucket: Optional[str]
    s3_public_base_url: Optional[str]
    redis_url: Optional[str]
    rq_queue_name: str
    worker_job_timeout_seconds: int
    workflow_stale_after_seconds: int


def load_settings() -> Settings:
    """读取环境变量，并提供无需基础设施即可启动的开发默认值。"""

    worker_job_timeout_seconds = int(os.getenv("WORKER_JOB_TIMEOUT_SECONDS", "1800"))
    # RQ 硬超时之后再保留五分钟缓冲，避免网络抖动或数据库提交稍慢时把仍在运行的
    # Worker 误判失败。它只负责解除“永久执行中”，从不自动重复调用模型扣费。
    workflow_stale_after_seconds = int(
        os.getenv("WORKFLOW_STALE_AFTER_SECONDS", str(worker_job_timeout_seconds + 300))
    )
    if worker_job_timeout_seconds < 30:
        raise RuntimeError("WORKER_JOB_TIMEOUT_SECONDS 不能小于 30 秒")
    if workflow_stale_after_seconds < worker_job_timeout_seconds + 60:
        raise RuntimeError("WORKFLOW_STALE_AFTER_SECONDS 至少应比 WORKER_JOB_TIMEOUT_SECONDS 多 60 秒")

    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///./data/ai_drama.db"),
        # auto 仅用于零配置本地开发；生产应设为 migrate，并在发布阶段执行 Alembic。
        database_schema_mode=os.getenv("DATABASE_SCHEMA_MODE", "auto"),
        local_storage_path=Path(os.getenv("LOCAL_STORAGE_PATH", "./data/assets")),
        source_video_storage_mode=os.getenv("SOURCE_VIDEO_STORAGE_MODE", "local"),
        task_execution_mode=os.getenv("TASK_EXECUTION_MODE", "inline"),
        cors_origins=_csv(os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")),
        simulated_step_delay_seconds=float(os.getenv("SIMULATED_STEP_DELAY_SECONDS", "0.35")),
        max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(500 * 1024 * 1024))),
        # 直连仅适合开发或供应商保证长期公网 URL 的场景；生产建议将模型结果
        # 立即转存到自己控制的 S3/MinIO，再把稳定 HTTPS URL 交给图生视频服务。
        generated_image_delivery_mode=os.getenv("GENERATED_IMAGE_DELIVERY_MODE", "direct"),
        generated_image_max_bytes=int(os.getenv("GENERATED_IMAGE_MAX_BYTES", str(25 * 1024 * 1024))),
        generated_image_download_timeout_seconds=float(
            os.getenv("GENERATED_IMAGE_DOWNLOAD_TIMEOUT_SECONDS", "90")
        ),
        # 完整 MP4 默认保存到本机媒体目录，适合单机开发；生产 Worker 应将其上传
        # 至同一套 S3/MinIO 与 HTTPS CDN，避免 API 本机磁盘成为单点。
        final_video_delivery_mode=os.getenv("FINAL_VIDEO_DELIVERY_MODE", "local"),
        s3_endpoint_url=os.getenv("S3_ENDPOINT_URL") or None,
        s3_bucket=os.getenv("S3_BUCKET") or None,
        s3_public_base_url=os.getenv("S3_PUBLIC_BASE_URL") or None,
        redis_url=os.getenv("REDIS_URL") or None,
        rq_queue_name=os.getenv("RQ_QUEUE_NAME", "ai_drama"),
        worker_job_timeout_seconds=worker_job_timeout_seconds,
        workflow_stale_after_seconds=workflow_stale_after_seconds,
    )


settings = load_settings()
