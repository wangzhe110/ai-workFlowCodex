# LemonFlow V1 Phase 1：数据库迁移方案

> 阶段状态：`已实施；仅在临时 SQLite 验证库执行迁移，未迁移用户生产数据`  
> 前置依据：`LemonFlow_V1_Architecture.md` 已确认  
> 目标：给出可审阅的增量迁移 SQL、旧数据兼容方案和 Alembic 实施顺序。本文不执行任何 DDL。

## 1. 最终架构检查结论

以下四项已作为本阶段不可更改的约束：

| 检查项 | 结论 | 数据库保障方式 |
|---|---|---|
| V1 主流程唯一 | 通过 | `project_production_states.active_stage` 只允许 V1 阶段；旧选题链路不再作为前置条件 |
| 资产可追溯 | 通过 | `shot_asset_bindings` 和 `video_clip_asset_bindings` 固化角色图、场景图、关键帧的版本 ID |
| 锁定不可覆盖 | 通过 | 所有资产表采用递增 `version`；锁定后禁止更新内容字段，只能插入新版本 |
| 模型业务解耦 | 通过 | `model_slots → model_slot_profile_bindings → model_profiles`；业务对象只保存模型快照和调用记录 |

补充边界：已接入协议的模型切换只改槽位绑定或模型配置；接入一种全新供应商协议时只新增 Adapter，不修改业务工作流服务。

## 2. 迁移策略

### 2.1 采用“只增不删”的三次迁移

| Alembic Revision | 内容 | 是否破坏旧数据 |
|---|---|---|
| `0003_v1_production_foundation` | 新建 V1 表、为旧表增加兼容列、创建索引 | 否 |
| `0004_v1_legacy_backfill` | 写入 `Legacy_V0` / `LemonFlow_V1` 工作流定义，回填旧运行和旧模型槽位绑定 | 否 |
| `0005_v1_asset_ownership_and_versions` | 修正角色/场景归属到已选故事，允许已锁定版本并存并由当前指针采用 | 否 |

生产环境只能通过 `alembic upgrade head` 执行迁移。SQLite 本地开发和 PostgreSQL 生产均由同一份 Alembic 迁移管理；正文 SQL 以 PostgreSQL 为阅读基准，实际迁移使用 SQLAlchemy `sa.JSON` 和 `batch_alter_table` 保持 SQLite 兼容。

### 2.2 绝不自动推断的旧数据

旧 `storyboard_images` 无法可靠推断为“角色图、场景图或关键帧”，旧 `video_clips` 也无法确认是否经过人工审核。因此：

- 不自动把旧图片迁入新的角色、场景、关键帧表；
- 不自动把旧视频标记为 `APPROVED`；
- 旧项目保持在 `LEGACY_READONLY` 生产阶段，可查看和导出历史结果；
- 新 V1 项目只走新的生产状态与新表。

## 3. 新增表 SQL

以下 DDL 为目标结构。所有主键由应用层生成 UUID 字符串，真实 API Key 不进入任何表。

```sql
CREATE TABLE workflow_definitions (
  id VARCHAR(36) PRIMARY KEY,
  workflow_code VARCHAR(80) NOT NULL,
  version VARCHAR(80) NOT NULL,
  definition_json JSONB NOT NULL,
  status VARCHAR(20) NOT NULL CHECK (status IN ('DRAFT', 'PUBLISHED', 'ARCHIVED')),
  published_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (workflow_code, version)
);

CREATE TABLE project_production_states (
  project_id VARCHAR(36) PRIMARY KEY REFERENCES projects(id),
  active_stage VARCHAR(40) NOT NULL,
  locked_reference_analysis_id VARCHAR(36),
  selected_story_proposal_id VARCHAR(36),
  director_plan_id VARCHAR(36),
  workflow_definition_id VARCHAR(36) NOT NULL REFERENCES workflow_definitions(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE reference_analyses (
  id VARCHAR(36) PRIMARY KEY,
  project_id VARCHAR(36) NOT NULL REFERENCES projects(id),
  workflow_run_id VARCHAR(36) NOT NULL REFERENCES workflow_runs(id),
  version INTEGER NOT NULL,
  video_script_structure JSONB NOT NULL,
  opening_analysis JSONB NOT NULL,
  viral_elements JSONB NOT NULL,
  scene_analysis JSONB NOT NULL,
  creative_brief JSONB NOT NULL,
  generation_status VARCHAR(20) NOT NULL CHECK (generation_status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')),
  review_status VARCHAR(20) NOT NULL CHECK (review_status IN ('PENDING_REVIEW', 'LOCKED', 'REJECTED')),
  locked_snapshot JSONB,
  locked_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (project_id, version)
);

CREATE TABLE review_decisions (
  id VARCHAR(36) PRIMARY KEY,
  project_id VARCHAR(36) NOT NULL REFERENCES projects(id),
  target_type VARCHAR(60) NOT NULL,
  target_id VARCHAR(36) NOT NULL,
  decision VARCHAR(20) NOT NULL CHECK (decision IN ('LOCKED', 'SELECTED', 'APPROVED', 'REJECTED')),
  reviewer_label VARCHAR(120) NOT NULL DEFAULT '人工审核',
  note TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX ix_review_decisions_target ON review_decisions(target_type, target_id, created_at DESC);

CREATE TABLE story_generation_batches (
  id VARCHAR(36) PRIMARY KEY,
  project_id VARCHAR(36) NOT NULL REFERENCES projects(id),
  reference_analysis_id VARCHAR(36) NOT NULL REFERENCES reference_analyses(id),
  workflow_run_id VARCHAR(36) NOT NULL REFERENCES workflow_runs(id),
  request_snapshot JSONB NOT NULL,
  status VARCHAR(20) NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TIMESTAMPTZ
);

CREATE TABLE story_proposals (
  id VARCHAR(36) PRIMARY KEY,
  batch_id VARCHAR(36) NOT NULL REFERENCES story_generation_batches(id),
  project_id VARCHAR(36) NOT NULL REFERENCES projects(id),
  model_invocation_id VARCHAR(36),
  candidate_number INTEGER NOT NULL,
  content JSONB NOT NULL,
  status VARCHAR(20) NOT NULL CHECK (status IN ('CANDIDATE', 'SELECTED', 'REJECTED')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (batch_id, candidate_number)
);
CREATE UNIQUE INDEX uq_selected_story_per_project
  ON story_proposals(project_id) WHERE status = 'SELECTED';

CREATE TABLE director_plans (
  id VARCHAR(36) PRIMARY KEY,
  project_id VARCHAR(36) NOT NULL REFERENCES projects(id),
  story_proposal_id VARCHAR(36) NOT NULL REFERENCES story_proposals(id),
  workflow_run_id VARCHAR(36) NOT NULL REFERENCES workflow_runs(id),
  visual_bible JSONB NOT NULL,
  status VARCHAR(20) NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'READY', 'FAILED')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE character_definitions (
  id VARCHAR(36) PRIMARY KEY,
  director_plan_id VARCHAR(36) NOT NULL REFERENCES director_plans(id),
  project_id VARCHAR(36) NOT NULL REFERENCES projects(id),
  character_code VARCHAR(80) NOT NULL,
  name VARCHAR(160) NOT NULL,
  age_description VARCHAR(120) NOT NULL,
  appearance TEXT NOT NULL,
  costume TEXT NOT NULL,
  temperament TEXT NOT NULL,
  design_status VARCHAR(20) NOT NULL CHECK (design_status IN ('DRAFT', 'READY', 'LOCKED')),
  locked_reference_image_id VARCHAR(36),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (director_plan_id, character_code)
);

CREATE TABLE scene_definitions (
  id VARCHAR(36) PRIMARY KEY,
  director_plan_id VARCHAR(36) NOT NULL REFERENCES director_plans(id),
  project_id VARCHAR(36) NOT NULL REFERENCES projects(id),
  scene_code VARCHAR(80) NOT NULL,
  name VARCHAR(160) NOT NULL,
  location TEXT NOT NULL,
  environment TEXT NOT NULL,
  visual_style TEXT NOT NULL,
  mood TEXT NOT NULL,
  design_status VARCHAR(20) NOT NULL CHECK (design_status IN ('DRAFT', 'READY', 'LOCKED')),
  locked_reference_image_id VARCHAR(36),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (director_plan_id, scene_code)
);

CREATE TABLE character_reference_images (
  id VARCHAR(36) PRIMARY KEY,
  character_id VARCHAR(36) NOT NULL REFERENCES character_definitions(id),
  project_id VARCHAR(36) NOT NULL REFERENCES projects(id),
  generation_run_id VARCHAR(36) NOT NULL REFERENCES workflow_runs(id),
  model_invocation_id VARCHAR(36),
  version INTEGER NOT NULL,
  prompt_snapshot TEXT NOT NULL,
  image_url TEXT,
  generation_status VARCHAR(20) NOT NULL CHECK (generation_status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')),
  review_status VARCHAR(20) NOT NULL CHECK (review_status IN ('PENDING_REVIEW', 'LOCKED', 'REJECTED')),
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (character_id, version)
);
CREATE UNIQUE INDEX uq_locked_character_image
  ON character_reference_images(character_id) WHERE review_status = 'LOCKED';

CREATE TABLE scene_reference_images (
  id VARCHAR(36) PRIMARY KEY,
  scene_id VARCHAR(36) NOT NULL REFERENCES scene_definitions(id),
  project_id VARCHAR(36) NOT NULL REFERENCES projects(id),
  generation_run_id VARCHAR(36) NOT NULL REFERENCES workflow_runs(id),
  model_invocation_id VARCHAR(36),
  version INTEGER NOT NULL,
  prompt_snapshot TEXT NOT NULL,
  image_url TEXT,
  generation_status VARCHAR(20) NOT NULL CHECK (generation_status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')),
  review_status VARCHAR(20) NOT NULL CHECK (review_status IN ('PENDING_REVIEW', 'LOCKED', 'REJECTED')),
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (scene_id, version)
);
CREATE UNIQUE INDEX uq_locked_scene_image
  ON scene_reference_images(scene_id) WHERE review_status = 'LOCKED';

CREATE TABLE shot_plans (
  id VARCHAR(36) PRIMARY KEY,
  director_plan_id VARCHAR(36) NOT NULL REFERENCES director_plans(id),
  project_id VARCHAR(36) NOT NULL REFERENCES projects(id),
  shot_number INTEGER NOT NULL,
  action_description TEXT NOT NULL,
  camera_description TEXT NOT NULL,
  duration_seconds NUMERIC(6, 2) NOT NULL,
  video_action_prompt TEXT NOT NULL,
  locked_keyframe_id VARCHAR(36),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (director_plan_id, shot_number)
);

CREATE TABLE shot_asset_bindings (
  id VARCHAR(36) PRIMARY KEY,
  shot_id VARCHAR(36) NOT NULL REFERENCES shot_plans(id),
  character_id VARCHAR(36) REFERENCES character_definitions(id),
  character_reference_image_id VARCHAR(36) REFERENCES character_reference_images(id),
  scene_id VARCHAR(36) NOT NULL REFERENCES scene_definitions(id),
  scene_reference_image_id VARCHAR(36) NOT NULL REFERENCES scene_reference_images(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (character_id IS NULL OR character_reference_image_id IS NOT NULL)
);
CREATE INDEX ix_shot_asset_bindings_shot ON shot_asset_bindings(shot_id);

CREATE TABLE shot_keyframes (
  id VARCHAR(36) PRIMARY KEY,
  shot_id VARCHAR(36) NOT NULL REFERENCES shot_plans(id),
  project_id VARCHAR(36) NOT NULL REFERENCES projects(id),
  generation_run_id VARCHAR(36) NOT NULL REFERENCES workflow_runs(id),
  model_invocation_id VARCHAR(36),
  version INTEGER NOT NULL,
  prompt_snapshot TEXT NOT NULL,
  image_url TEXT,
  input_asset_snapshot JSONB NOT NULL,
  generation_status VARCHAR(20) NOT NULL CHECK (generation_status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')),
  review_status VARCHAR(20) NOT NULL CHECK (review_status IN ('PENDING_REVIEW', 'LOCKED', 'REJECTED')),
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (shot_id, version)
);
CREATE UNIQUE INDEX uq_locked_shot_keyframe
  ON shot_keyframes(shot_id) WHERE review_status = 'LOCKED';

CREATE TABLE model_slots (
  id VARCHAR(36) PRIMARY KEY,
  slot_key VARCHAR(80) NOT NULL UNIQUE,
  capability VARCHAR(80) NOT NULL,
  selection_mode VARCHAR(20) NOT NULL CHECK (selection_mode IN ('SINGLE', 'MULTI_PARALLEL', 'AB_TEST')),
  description TEXT NOT NULL,
  is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE model_slot_profile_bindings (
  id VARCHAR(36) PRIMARY KEY,
  slot_id VARCHAR(36) NOT NULL REFERENCES model_slots(id),
  model_profile_id VARCHAR(36) NOT NULL REFERENCES model_profiles(id),
  is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
  priority INTEGER NOT NULL DEFAULT 100,
  weight NUMERIC(8, 4),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (slot_id, model_profile_id)
);

CREATE TABLE prompt_templates (
  id VARCHAR(36) PRIMARY KEY,
  task_type VARCHAR(80) NOT NULL,
  name VARCHAR(160) NOT NULL,
  version INTEGER NOT NULL,
  content TEXT NOT NULL,
  variables_schema JSONB NOT NULL,
  status VARCHAR(20) NOT NULL CHECK (status IN ('DRAFT', 'ACTIVE', 'ARCHIVED')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (task_type, name, version)
);
CREATE UNIQUE INDEX uq_active_prompt_name
  ON prompt_templates(task_type, name) WHERE status = 'ACTIVE';

CREATE TABLE model_invocations (
  id VARCHAR(36) PRIMARY KEY,
  project_id VARCHAR(36) NOT NULL REFERENCES projects(id),
  workflow_run_id VARCHAR(36) REFERENCES workflow_runs(id),
  workflow_step_id VARCHAR(36) REFERENCES workflow_steps(id),
  model_slot_id VARCHAR(36) NOT NULL REFERENCES model_slots(id),
  model_profile_id VARCHAR(36) NOT NULL REFERENCES model_profiles(id),
  prompt_template_id VARCHAR(36) REFERENCES prompt_templates(id),
  task_type VARCHAR(80) NOT NULL,
  model_profile_snapshot JSONB NOT NULL,
  prompt_snapshot JSONB NOT NULL,
  input_snapshot JSONB NOT NULL,
  output_reference JSONB,
  provider_task_id VARCHAR(255),
  input_tokens INTEGER,
  output_tokens INTEGER,
  media_units JSONB NOT NULL DEFAULT '{}'::jsonb,
  cost_amount NUMERIC(14, 6),
  currency VARCHAR(12) NOT NULL DEFAULT 'CNY',
  latency_ms INTEGER,
  status VARCHAR(20) NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')),
  error_code VARCHAR(120),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TIMESTAMPTZ
);
CREATE INDEX ix_model_invocations_profile_task ON model_invocations(model_profile_id, task_type, created_at DESC);
CREATE INDEX ix_model_invocations_project ON model_invocations(project_id, created_at DESC);

CREATE TABLE model_quality_evaluations (
  id VARCHAR(36) PRIMARY KEY,
  model_profile_id VARCHAR(36) NOT NULL REFERENCES model_profiles(id),
  prompt_template_id VARCHAR(36) REFERENCES prompt_templates(id),
  task_type VARCHAR(80) NOT NULL,
  scenario VARCHAR(160) NOT NULL,
  aggregation_start TIMESTAMPTZ NOT NULL,
  aggregation_end TIMESTAMPTZ NOT NULL,
  sample_count INTEGER NOT NULL,
  success_count INTEGER NOT NULL,
  success_rate NUMERIC(8, 4) NOT NULL,
  average_cost_amount NUMERIC(14, 6),
  average_latency_ms INTEGER,
  average_human_score NUMERIC(5, 2),
  adoption_rate NUMERIC(8, 4),
  source VARCHAR(40) NOT NULL DEFAULT 'AUTO_AGGREGATED',
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE video_clip_asset_bindings (
  id VARCHAR(36) PRIMARY KEY,
  video_clip_id VARCHAR(36) NOT NULL REFERENCES video_clips(id),
  asset_type VARCHAR(40) NOT NULL CHECK (asset_type IN ('CHARACTER_REFERENCE', 'SCENE_REFERENCE', 'SHOT_KEYFRAME')),
  character_reference_image_id VARCHAR(36) REFERENCES character_reference_images(id),
  scene_reference_image_id VARCHAR(36) REFERENCES scene_reference_images(id),
  shot_keyframe_id VARCHAR(36) REFERENCES shot_keyframes(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (
    (asset_type = 'CHARACTER_REFERENCE' AND character_reference_image_id IS NOT NULL AND scene_reference_image_id IS NULL AND shot_keyframe_id IS NULL)
    OR (asset_type = 'SCENE_REFERENCE' AND character_reference_image_id IS NULL AND scene_reference_image_id IS NOT NULL AND shot_keyframe_id IS NULL)
    OR (asset_type = 'SHOT_KEYFRAME' AND character_reference_image_id IS NULL AND scene_reference_image_id IS NULL AND shot_keyframe_id IS NOT NULL)
  )
);
```

以下外键因表创建顺序形成循环引用，统一在全部新表创建完成后补充：

```sql
ALTER TABLE project_production_states
  ADD CONSTRAINT fk_state_locked_analysis FOREIGN KEY (locked_reference_analysis_id) REFERENCES reference_analyses(id),
  ADD CONSTRAINT fk_state_selected_story FOREIGN KEY (selected_story_proposal_id) REFERENCES story_proposals(id),
  ADD CONSTRAINT fk_state_director_plan FOREIGN KEY (director_plan_id) REFERENCES director_plans(id);

ALTER TABLE character_definitions
  ADD CONSTRAINT fk_character_locked_image FOREIGN KEY (locked_reference_image_id) REFERENCES character_reference_images(id);
ALTER TABLE scene_definitions
  ADD CONSTRAINT fk_scene_locked_image FOREIGN KEY (locked_reference_image_id) REFERENCES scene_reference_images(id);
ALTER TABLE shot_plans
  ADD CONSTRAINT fk_shot_locked_keyframe FOREIGN KEY (locked_keyframe_id) REFERENCES shot_keyframes(id);

ALTER TABLE story_proposals
  ADD CONSTRAINT fk_story_invocation FOREIGN KEY (model_invocation_id) REFERENCES model_invocations(id);
ALTER TABLE character_reference_images
  ADD CONSTRAINT fk_character_image_invocation FOREIGN KEY (model_invocation_id) REFERENCES model_invocations(id);
ALTER TABLE scene_reference_images
  ADD CONSTRAINT fk_scene_image_invocation FOREIGN KEY (model_invocation_id) REFERENCES model_invocations(id);
ALTER TABLE shot_keyframes
  ADD CONSTRAINT fk_keyframe_invocation FOREIGN KEY (model_invocation_id) REFERENCES model_invocations(id);
```

## 4. 修改现有表 SQL

以下修改均为增量列；旧列暂不删除。

```sql
ALTER TABLE workflow_runs
  ADD COLUMN workflow_definition_id VARCHAR(36) REFERENCES workflow_definitions(id),
  ADD COLUMN workflow_version VARCHAR(80),
  ADD COLUMN input_snapshot JSONB;

ALTER TABLE model_profiles
  ADD COLUMN adapter_key VARCHAR(80),
  ADD COLUMN model_version VARCHAR(160),
  ADD COLUMN display_name VARCHAR(160);

ALTER TABLE video_clips
  ADD COLUMN shot_plan_id VARCHAR(36) REFERENCES shot_plans(id),
  ADD COLUMN model_invocation_id VARCHAR(36) REFERENCES model_invocations(id),
  ADD COLUMN generation_status VARCHAR(20),
  ADD COLUMN review_status VARCHAR(20),
  ADD COLUMN reviewed_at TIMESTAMPTZ,
  ADD COLUMN review_note TEXT,
  ADD COLUMN input_asset_snapshot JSONB;

ALTER TABLE final_videos
  ADD COLUMN director_plan_id VARCHAR(36) REFERENCES director_plans(id),
  ADD COLUMN workflow_definition_id VARCHAR(36) REFERENCES workflow_definitions(id),
  ADD COLUMN workflow_version VARCHAR(80),
  ADD COLUMN approved_clip_ids JSONB,
  ADD COLUMN input_snapshot JSONB;

-- V1 成片不再强依赖旧 storyboard_packages；执行该变更前必须确认旧代码已停止写入。
ALTER TABLE final_videos
  ALTER COLUMN storyboard_package_id DROP NOT NULL;
```

SQLite 不支持 PostgreSQL 的部分索引和 `ALTER COLUMN`。实际 Alembic 实施规则：

- 使用 `sa.JSON`，不直接写 `JSONB`；
- 使用 `op.batch_alter_table` 修改 `video_clips` 与 `final_videos`；
- PostgreSQL 的“每个对象只能有一个锁定版本”由部分唯一索引保障；SQLite 在服务层事务中二次校验，并有同等集成测试；
- `media_units` 的应用层默认值设为 `{}`，避免 SQLite 的 PostgreSQL JSON 默认表达式差异。

## 5. 旧数据回填 SQL 与兼容策略

### 5.1 写入 Workflow 定义

```sql
INSERT INTO workflow_definitions
  (id, workflow_code, version, definition_json, status, published_at)
VALUES
  ('00000000-0000-0000-0000-0000000000v0', 'LEMONFLOW_LEGACY', 'Legacy_V0', '{"mode":"legacy"}'::jsonb, 'PUBLISHED', CURRENT_TIMESTAMP),
  ('00000000-0000-0000-0000-0000000000v1', 'LEMONFLOW_PRODUCTION', 'LemonFlow_V1', '{"mode":"v1-production"}'::jsonb, 'PUBLISHED', CURRENT_TIMESTAMP);

UPDATE workflow_runs
SET workflow_definition_id = '00000000-0000-0000-0000-0000000000v0',
    workflow_version = 'Legacy_V0',
    input_snapshot = COALESCE(input_snapshot, '{}'::jsonb)
WHERE workflow_definition_id IS NULL;
```

正式实现中不使用上面的示例固定 ID，而由 Alembic/Python 生成常量 UUID；这里仅说明回填顺序。

### 5.2 回填旧模型配置

```sql
UPDATE model_profiles
SET adapter_key = provider_key,
    model_version = model_key,
    display_name = COALESCE(provider_config->>'display_name', model_key)
WHERE adapter_key IS NULL;
```

随后依据旧 `step_key` 创建**历史兼容槽位**并绑定旧配置：

| 旧步骤 | 兼容槽位 |
|---|---|
| `analyze_reference_mechanisms` | `LEGACY_VIDEO_ANALYSIS` |
| `generate_original_topics` | `OPTIONAL_TOPIC_GENERATE` |
| `generate_story_package` | `LEGACY_STORY_GENERATE` |
| `generate_storyboard` | `LEGACY_STORYBOARD` |
| `generate_storyboard_images` | `LEGACY_SINGLE_SHOT_IMAGE` |
| `generate_storyboard_video_groups` | `LEGACY_VIDEO_GENERATE` |
| `assemble_final_video` | `LEGACY_FINAL_COMPOSE` |

新 V1 槽位单独创建，不将旧 `is_active` 状态直接转为 V1 生产默认值。这样不会让模拟模型或旧云雾配置意外成为新生产链路的默认模型。

### 5.3 历史对象处理

| 旧对象 | V1 处理 |
|---|---|
| `topic_candidates` | 保留，仅供可选灵感工具读取 |
| `story_packages` | 保留历史，不自动转换为 `story_proposals` |
| `storyboard_packages` / `storyboard_images` | 保留历史，不自动猜测角色、场景和关键帧关系 |
| `video_clips` | 保留原 `status`，`review_status` 保持 `NULL`，不可进入 V1 成片导出 |
| `final_videos` | 保留原成片和 `clip_ids`；新 V1 使用 `approved_clip_ids` |
| `model_evaluations` | 保留为 `LEGACY_MANUAL` 历史统计；不伪造采用率 |

### 5.4 切换时机

1. 执行 `0003`，仅创建结构；旧 API 与旧页面继续可用。
2. 执行 `0004`，完成历史回填；仍不修改旧项目业务状态。
3. Phase 2 后端接口上线后，创建的新项目默认初始化为 `LemonFlow_V1`。
4. Phase 3 前端生产台上线后，隐藏旧流程入口；旧项目仍可在“历史项目”中查看。
5. 旧表至少保留一个完整发布周期，确认没有回滚需求后再单独评估清理迁移。

## 6. 必须在实际迁移中加入的约束

| 规则 | 实现方式 |
|---|---|
| 一个项目同时只能有一份锁定分析 | `project_production_states.locked_reference_analysis_id` + 服务层事务校验 |
| 同一故事生成批次只能选中一个故事 | PostgreSQL/SQLite 部分唯一索引 `uq_selected_story_per_batch` |
| 角色/场景/分镜可保留多个已锁定图片版本 | 父对象的 `locked_*_id` 选择本轮采用版本；历史锁定版本永久保留 |
| 视频只能使用锁定资产 | `video_clip_asset_bindings` 的三类明确外键 + 创建前校验资产状态为 `LOCKED` |
| 成片只能使用审核通过片段 | 创建成片任务前校验 `video_clips.review_status = 'APPROVED'` |
| 锁定结果不可修改 | 服务层拒绝更新锁定行；数据库审计触发器作为 PostgreSQL 增强项，V1 不依赖触发器保证跨库一致性 |
| 真实密钥不得入库 | `provider_config`、`input_snapshot`、`prompt_snapshot` 均进行敏感字段拒绝/脱敏校验 |

## 7. Phase 1 验收标准

Phase 1 代码实施时必须通过：

1. 从空数据库升级到最新 revision；
2. 从现有 `0002_model_evaluations` 数据库升级且历史行数量不减少；
3. PostgreSQL 与 SQLite 的迁移测试均通过；
4. 旧项目仍可读取旧故事、图片、视频和成片；
5. 新 V1 项目不会被 `topic_candidates` 或旧故事包阻挡；
6. 锁定版本无法被更新，重新生成只能得到版本加一的新行；
7. 外键、唯一索引和服务层校验能阻止跨项目资产引用；
8. API、日志、快照中均不出现真实 API Key。

## 8. 本阶段交付与下一阶段

已创建 `0003`、`0004` 和 `0005` Alembic revision，并在临时 SQLite 库完成空库升级验证；
未对用户现有数据库执行迁移。角色/场景的归属在 Phase 2 开始前复核后已修正为“已选故事”，
避免它们被错误地设计在导演分镜之后。

Phase 2 的目标是实现后端状态机、新 Workflow 节点、API 契约和 Adapter 能力接口。Phase 2 结束时必须单独报告：完成内容、修改文件列表、数据结构变化、测试结果与 Phase 3 计划。
