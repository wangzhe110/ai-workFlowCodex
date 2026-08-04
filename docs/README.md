# 文档模块

本目录存放随代码演进的接口、部署与模型接入说明。业务代码中的注释解释局部规则；这里记录跨模块的约定和决策。

当前 API 自带 OpenAPI 文档：启动服务后访问 `/docs`。后续会补充：

- 各中转站的第三方模型适配规范与验收样例；
- Redis Worker、回调、超时、幂等与死信队列策略；
- 生产部署、备份与观测规范；
- 自动编排最终成片的审核与导出约定。

当前可直接使用的接入说明：

- `LemonFlow_V1_Architecture.md`：已确认的 LemonFlow V1 架构基线，覆盖主生产链路、审核闸门、数据库 ER、API、模型槽位、Adapter、Prompt/Workflow 版本和旧模块迁移方案。
- `LemonFlow_V1_Phase1_Database_Migration_Plan.md`：V1 数据库迁移实施记录，包含新增/修改表、数据回填、旧数据兼容、迁移顺序和验收标准；已在临时 SQLite 库验证，尚未迁移用户现有数据库。
- `LemonFlow_V1_Phase2_Backend_Implementation.md`：V1 状态机、人工审核接口、模型槽位和 Prompt 版本接口的实施与测试记录。
- `LemonFlow_V1_Phase3_Frontend_Implementation.md`：Vue 3 V1 生产台、审核/锁定交互、旧页面兼容路由和前端构建验证记录。
- `LemonFlow_V1_Phase4_Local_Closure.md`：V1 本地模拟闭环、真实 Adapter、模型中心、任务入口、模型调用审计与完整闭环测试记录；真实渠道仍需用项目自己的 Key 做小样本验收。
- `LemonFlow_V1_Phase5_Quality_Reporting.md`：V1 人工质量评分、预计成本、模型质量快照、报表接口/页面与测试记录。
- `LemonFlow_V1_Phase6_Prompt_Management.md`：V1 Prompt 草稿、激活、归档保护、页面与测试记录。
- `LemonFlow_V1_Phase7_Production_Traceability.md`：项目级 Workflow、模型、Prompt 版本追溯接口、页面与测试记录。
- `LemonFlow_V1_真实模型配置操作.md`：面向非技术制作负责人的 V1 模型中心、服务器 Key、Gemini/Claude/图片/Seedance/FFmpeg 配置与小样本验收操作。
- `LemonFlow_V1_模型质量报表操作.md`：面向制作负责人的质量评分、成本预估、报表解读和人工切换操作说明。
- `LemonFlow_V1_Prompt模板操作.md`：面向制作负责人的 Prompt 草稿、新版本小样本验证、人工启用与归档规则。
- `LemonFlow_V1_生产追溯操作.md`：面向制作负责人的项目模型/Prompt/Workflow 版本定位和问题反馈方法。
- `V1本地闭环验收操作.md`：不配置 API Key 即可检查 V1 主流程、审核节点和状态流转的零基础操作说明。
- `模型配置小白操作卡.md`：不需要技术背景即可完成模型名称填写、预检、小样本测试与启用的操作卡。
- `部署与模型配置快速指南.md`：面向项目负责人的从零启动、Docker 本机验收、模型步骤对应关系、密钥配置、测试与启用操作指南。
- `豆包Seedance视频操作说明.md`：火山方舟 `doubao-seedance-2-0-mini-260615` 的零基础启用、测试与故障处理说明。
- `云雾文本模型接入说明.md`：云雾 OpenAI 兼容文本接口在选题、故事与分镜步骤中的安全配置方式。
- `云雾异步图生视频接入说明.md`：备用通用异步视频中转站的技术接入说明；当前默认视频模型不使用它。
- `云雾视频分析接入说明.md`：参考视频抽帧、视觉模型分析、授权边界与部署验收方式。
- `云雾语音转写接入说明.md`：参考视频开头音轨的转写、内存数据边界、独立模型配置与验收方式。
- `图片结果对象存储说明.md`：将图片模型的临时 URL 转存为稳定 HTTPS 首帧的生产配置方式。
- `Redis_RQ_Worker部署说明.md`：从本地后台任务切换到独立生产 Worker 的配置、启动和故障处理方式；可配合根目录 Docker Compose 本机验收。
- `完整成片导出说明.md`：片段版本选择、FFmpeg 合成、下载、故障排查与生产存储边界。
- `模型配置预检说明.md`：每个模型候选的无扣费预检、小样本验收边界和启用步骤。
- `模型评测记录说明.md`：如何记录和比较模型的成本、耗时、成功率和质量评分。
- `运维备份恢复说明.md`：数据库和媒体对象的备份边界、恢复前检查及本机演练流程。
- `用户使用手册.md`：面向非程序制作人员的完整日常操作、模型验收和失败处理步骤。
