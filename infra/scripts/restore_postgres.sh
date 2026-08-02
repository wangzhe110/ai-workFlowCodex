#!/usr/bin/env bash
# PostgreSQL 恢复脚本。此操作会替换当前数据库，必须显式输入确认参数。
# 用法：bash infra/scripts/restore_postgres.sh /绝对路径/backup.dump --confirm-restore
set -euo pipefail

if [ "$#" -ne 2 ] || [ "$2" != "--confirm-restore" ]; then
  echo "用法：bash infra/scripts/restore_postgres.sh /绝对路径/backup.dump --confirm-restore" >&2
  echo "警告：恢复会替换当前 PostgreSQL 数据；脚本会先自动创建一份恢复前备份。" >&2
  exit 2
fi

BACKUP_FILE="$1"
case "$BACKUP_FILE" in
  /*) ;;
  *)
    echo "为避免读取不可预期文件，备份文件必须使用绝对路径。" >&2
    exit 2
    ;;
esac
if [ ! -s "$BACKUP_FILE" ]; then
  echo "指定备份文件不存在或为空：$BACKUP_FILE" >&2
  exit 2
fi

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
COMPOSE_ENV="$PROJECT_DIR/infra/compose.env"
if [ ! -f "$COMPOSE_ENV" ]; then
  echo "未找到 infra/compose.env；请先按 infra/README.md 创建本机部署配置。" >&2
  exit 2
fi

# 恢复前先保留当前状态，防止选错备份文件后无法回退。
PRE_RESTORE_DIR="$(dirname "$BACKUP_FILE")/pre-restore"
bash "$PROJECT_DIR/infra/scripts/backup_postgres.sh" "$PRE_RESTORE_DIR"

SERVICES_STOPPED=false
restart_application_services() {
  if [ "$SERVICES_STOPPED" = true ]; then
    echo "正在重新启动 API 与 Worker…"
    docker compose --env-file "$COMPOSE_ENV" -f "$PROJECT_DIR/docker-compose.yml" up -d api worker
  fi
}
trap restart_application_services EXIT

echo "正在停止 API 与 Worker，避免恢复期间产生新写入…"
docker compose --env-file "$COMPOSE_ENV" -f "$PROJECT_DIR/docker-compose.yml" stop api worker
SERVICES_STOPPED=true

echo "正在恢复数据库：$BACKUP_FILE"
docker compose --env-file "$COMPOSE_ENV" -f "$PROJECT_DIR/docker-compose.yml" \
  exec -T postgres pg_restore -U ai_drama -d ai_drama --clean --if-exists --no-owner --exit-on-error < "$BACKUP_FILE"

echo "数据库恢复完成；API 与 Worker 将由退出陷阱重新启动。"
