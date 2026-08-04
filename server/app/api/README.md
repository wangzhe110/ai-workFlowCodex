# HTTP 接口模块

路由层只解析请求、校验前置条件、调用服务、返回 API 契约和投递任务；不直接写状态机、不执行长任务、不保存密钥。

## V1 路由职责

- `routes/projects.py`：项目和参考视频上传。
- `routes/production.py`：生产台状态、生成运行入口、分析锁定、故事选择、图片锁定、视频审核和运行状态查询。
- `routes/v1_modeling.py`：Workflow 定义、模型槽位、模型绑定、Prompt 模板版本和人工启用。
- `routes/model_profiles.py`：非敏感模型配置、无扣费预检和兼容管理接口。

`workflows.py`、`topics.py`、`stories.py`、`storyboards.py`、`images.py`、`videos.py` 和 `creative_library.py` 为历史兼容或辅助功能；它们不得成为 V1 主链路前置条件。所有 API 以 `/api/v1` 开头。
