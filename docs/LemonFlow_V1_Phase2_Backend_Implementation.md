# LemonFlow V1 Phase 2：后端状态机与审核接口实施记录

> 阶段状态：`已完成`  
> 依赖：`LemonFlow_V1_Architecture.md`、Phase 1 数据迁移  
> 范围：主流程状态、人工审核闸门、模型槽位与 Prompt 版本；不发起任何真实付费模型调用。

## 1. 本阶段完成内容

- 新项目创建后自动初始化 `LemonFlow_V1` 生产状态；历史项目仍由迁移保持 `LEGACY_READONLY`，不会被新流程覆盖。
- 实现正式阶段状态机：分析审核、故事选择、角色图锁定、场景图锁定、关键帧锁定、视频片段通过/驳回。
- 每一次人工决定都写入 `review_decisions`，即使 V1 暂无登录，也记录审核人标签、决定和备注。
- 锁定后的内容版本不修改；角色、场景、关键帧的父对象只更新“本轮采用版本”的指针，历史锁定版本继续存在供溯源。
- 修正资产归属：角色与场景基于已选故事设计，导演分镜只在角色和场景锁定后生成。
- 增加 V1 生产台接口，旧原创选题、旧故事包和旧单镜图片接口不再是主链路前置条件。
- 增加模型槽位接口与 Prompt 模板版本接口：业务流程只看槽位，未绑定 Gemini、Claude、Banana 或 Seedance 的具体协议。

## 2. 阶段状态流转

```text
REFERENCE_ANALYSIS
  -> ANALYSIS_REVIEW
  -> STORY_GENERATION
  -> STORY_REVIEW
  -> CHARACTER_ASSETS
  -> SCENE_ASSETS
  -> DIRECTOR_PLANNING
  -> SHOT_KEYFRAMES
  -> VIDEO_GENERATION
  -> VIDEO_REVIEW
  -> FINAL_EXPORT
```

人工操作只能发生在对应审核阶段；例如，分析未进入 `ANALYSIS_REVIEW` 时不能锁定，视频未进入 `VIDEO_REVIEW` 时不能审核通过。任何跨阶段或跨项目引用都会返回 `409 Conflict`，而不是悄悄跳过闸门。

## 3. 新增 API

以下路径均以 `/api/v1/production` 为前缀：

| 功能 | 接口 |
|---|---|
| 读取生产状态 | `GET /projects/{project_id}/state` |
| 查看/锁定/驳回分析 | `GET /projects/{project_id}/reference-analyses`、`POST /reference-analyses/{id}/lock`、`POST /reference-analyses/{id}/reject` |
| 查看/选择故事 | `GET /projects/{project_id}/story-proposals`、`POST /story-proposals/{id}/select` |
| 查看/锁定角色图 | `GET /projects/{project_id}/character-reference-images`、`POST /character-reference-images/{id}/lock` |
| 查看/锁定场景图 | `GET /projects/{project_id}/scene-reference-images`、`POST /scene-reference-images/{id}/lock` |
| 查看/锁定关键帧 | `GET /projects/{project_id}/shot-keyframes`、`POST /shot-keyframes/{id}/lock` |
| 查看/审核视频 | `GET /projects/{project_id}/video-clips`、`POST /video-clips/{id}/approve`、`POST /video-clips/{id}/reject` |
| 查看 V1 Workflow | `GET /workflow-definition` |
| 模型槽位与绑定 | `GET /model-slots`、`POST /model-slots/{slot_key}/strategy`、`POST /model-slots/{slot_key}/bindings` |
| Prompt 版本 | `GET/POST /prompt-templates`、`POST /prompt-templates/{id}/activate`、`POST /prompt-templates/{id}/archive` |

`ReviewActionRequest` 可传 `reviewer_label` 和 `note`，用于形成可追溯审核记录。

## 4. 数据结构修正

`0005_v1_asset_ownership_and_versions` 做了两项重要修正：

1. `character_definitions`、`scene_definitions` 以 `story_proposal_id` 为主归属，旧 `director_plan_id` 只作为可空历史兼容字段；保证角色/场景先于导演分镜产生。
2. 不再限制“一个角色/场景/分镜只能有一个 `LOCKED` 图片版本”。每个版本都可被永久锁定，父对象的 `locked_reference_image_id` 或 `locked_keyframe_id` 表示当前生产轮采用哪一版。过去的视频片段仍保存其实际使用的版本 ID。

故事选择改为“同一 `StoryGenerationBatch` 只能选中一个方案”；不同批次可以保留历史已选方案，项目状态指针确定当前生产轮使用哪一份。

## 5. 未在本阶段执行的内容

- 不调用真实 Gemini、Claude、Banana 或 Seedance，避免在尚未配置 Adapter、Prompt 和密钥环境时产生费用。
- 不允许前端伪造“模型已生成成功”；Worker 完成后将使用服务层的 `mark_*_ready` 函数把真实结果推进到审核阶段。
- 不做自动模型切换；`AB_TEST` 在 V1 配置接口中明确拒绝，模型质量数据只支持人工决策。

## 6. 验证记录

已验证：

```bash
cd server
source .venv/bin/activate
PYTHONDONTWRITEBYTECODE=1 pytest -q
# 31 passed

DATABASE_URL=sqlite:////tmp/.../fresh.db alembic upgrade head
# 成功升级至 0005_v1_asset_ownership_and_versions
```

这些验证只使用临时 SQLite 文件，不读取用户的 API Key，也没有调用第三方模型或迁移用户现有数据库。

## 7. 下一阶段：前端生产台改造

Phase 3 将把 Vue 3 项目工作台改为唯一 V1 主流程：顶部状态条、逐阶段卡片、审核/锁定按钮、版本选择、阻塞原因和模型/P​rompt 配置入口。旧流程页面保留为历史/可选工具，但不再作为新项目的默认路径。
