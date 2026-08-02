#!/usr/bin/env bash
# 发布前最小验证门禁：检查迁移、后端回归测试和前端生产构建。
# 用法：bash infra/scripts/verify_release.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON_BIN="$PROJECT_DIR/server/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  echo "未找到 server/.venv/bin/python；请先按 README 安装后端依赖。" >&2
  exit 2
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "未找到 npm；请先安装 Node.js 并在 web/ 安装依赖。" >&2
  exit 2
fi

TEMP_DATABASE_DIR="$(mktemp -d /tmp/ai-drama-release-verify.XXXXXX)"
TEMP_DATABASE_URL="sqlite:///$TEMP_DATABASE_DIR/release-check.db"

echo "[1/4] 验证全新数据库可以迁移到最新版本…"
(
  cd "$PROJECT_DIR/server"
  DATABASE_URL="$TEMP_DATABASE_URL" DATABASE_SCHEMA_MODE=migrate \
    "$PYTHON_BIN" -m alembic upgrade head
)

echo "[2/4] 运行后端编译与回归测试…"
(
  cd "$PROJECT_DIR/server"
  PYTHONPYCACHEPREFIX=/tmp/ai-drama-pycache "$PYTHON_BIN" -m compileall -q app
  PYTHONPYCACHEPREFIX=/tmp/ai-drama-pycache "$PYTHON_BIN" -m pytest -q
)

echo "[3/4] 构建 Vue 生产包…"
(
  cd "$PROJECT_DIR/web"
  npm_config_cache=/tmp/ai-drama-npm-cache npm run build
)

echo "[4/4] 检查运维脚本语法…"
bash -n "$PROJECT_DIR/infra/scripts/backup_postgres.sh"
bash -n "$PROJECT_DIR/infra/scripts/restore_postgres.sh"

echo "发布前检查通过。临时迁移数据库位于：$TEMP_DATABASE_DIR"
echo "该临时目录只用于本次本机校验，可由系统临时目录策略清理。"
