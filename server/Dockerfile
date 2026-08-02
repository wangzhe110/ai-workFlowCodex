# API 与 RQ Worker 共用这个镜像，确保模型适配、FFmpeg 和迁移脚本版本完全一致。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app/server

# 视频分析、转写和完整成片导出都会通过命令行调用 FFmpeg/FFprobe。
RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && apt-get clean

COPY server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 仅复制服务运行必需文件，前端依赖、测试素材与本机数据库不会进入镜像。
COPY server/alembic.ini ./alembic.ini
COPY server/pyproject.toml ./pyproject.toml
COPY server/migrations ./migrations
COPY server/app ./app

# 本机 compose 使用命名卷挂载到此目录；生产多机请改为 S3 并配置受限凭据。
RUN mkdir -p /var/lib/ai-drama/assets

EXPOSE 8000
