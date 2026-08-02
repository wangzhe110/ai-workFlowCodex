# 运维脚本模块

本目录只放部署维护脚本，不放业务逻辑。所有脚本都有中文注释，并要求绝对路径或显式
确认参数，避免误操作本机文件或生产数据。

- `backup_postgres.sh`：从 Docker Compose 中运行的 PostgreSQL 导出一个自定义格式逻辑备份，不停 API/Worker。
- `restore_postgres.sh`：先创建恢复前备份，再停止 API/Worker、替换数据库并重新启动服务。它必须额外传入 `--confirm-restore` 才会执行。
- `verify_release.sh`：使用临时数据库验证迁移、运行后端回归测试、构建 Vue 生产包并检查运维脚本语法；每次发布前运行一次。

它们只处理 PostgreSQL 元数据和业务记录。源视频、图片、视频片段、完整成片等媒体不在
数据库内：本机验收时位于 Docker 媒体卷，正式生产时应由 S3/MinIO 的版本化、跨区域复制
或定期导出策略负责。具体频率和演练流程见 `../../docs/运维备份恢复说明.md`。
