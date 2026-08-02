#!/usr/bin/env bash
# PostgreSQL 逻辑备份脚本：用于本机 Docker Compose 验收和同构部署。
# 用法：bash infra/scripts/backup_postgres.sh /绝对路径/备份目录
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "用法：bash infra/scripts/backup_postgres.sh /绝对路径/备份目录" >&2
  exit 2
fi

BACKUP_DIR="$1"
case "$BACKUP_DIR" in
  /*) ;;
  *)
    echo "为避免把备份写到不可预期位置，备份目录必须是绝对路径。" >&2
    exit 2
    ;;
esac

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
COMPOSE_ENV="$PROJECT_DIR/infra/compose.env"
if [ ! -f "$COMPOSE_ENV" ]; then
  echo "未找到 infra/compose.env；请先按 infra/README.md 创建本机部署配置。" >&2
  exit 2
fi

mkdir -p "$BACKUP_DIR"
umask 077
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FINAL_FILE="$BACKUP_DIR/ai-drama-postgres-$TIMESTAMP.dump"
TEMP_FILE="$BACKUP_DIR/.ai-drama-postgres-$TIMESTAMP.partial"

echo "正在导出 PostgreSQL 逻辑备份（不会停止 API 或 Worker）…"
docker compose --env-file "$COMPOSE_ENV" -f "$PROJECT_DIR/docker-compose.yml" \
  exec -T postgres pg_dump -U ai_drama -d ai_drama --format=custom --no-owner > "$TEMP_FILE"

if [ ! -s "$TEMP_FILE" ]; then
  echo "备份文件为空，未生成可恢复备份。" >&2
  exit 1
fi

mv "$TEMP_FILE" "$FINAL_FILE"
echo "备份完成：$FINAL_FILE"
echo "注意：此脚本只备份 PostgreSQL；源视频、图片和成片还需按对象存储策略单独备份。"
