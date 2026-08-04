"""回填 LemonFlow V1 工作流定义、历史项目状态和模型槽位。

Revision ID: 0004_v1_legacy_backfill
Revises: 0003_v1_production_foundation
Create Date: 2026-08-03

本迁移不猜测旧内容的角色、场景或审核结论。历史项目统一标为只读兼容，V1
新生产链路只在新项目或显式迁移项目中启用。
"""

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from alembic import op
import sqlalchemy as sa


revision = "0004_v1_legacy_backfill"
down_revision = "0003_v1_production_foundation"
branch_labels = None
depends_on = None


def _id(name: str) -> str:
    """为迁移种子数据生成稳定 ID，重复执行时不会产生不同引用。"""

    return str(uuid5(NAMESPACE_URL, f"lemonflow-v1/{name}"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def upgrade() -> None:
    """建立新旧工作流并存时所需的最小可审计数据。"""

    bind = op.get_bind()
    now = _now()
    workflow_definitions = sa.table(
        "workflow_definitions",
        sa.column("id", sa.String),
        sa.column("workflow_code", sa.String),
        sa.column("version", sa.String),
        sa.column("definition_json", sa.JSON),
        sa.column("status", sa.String),
        sa.column("published_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    legacy_definition_id = _id("workflow/legacy-v0")
    v1_definition_id = _id("workflow/lemonflow-v1")
    existing_definition_ids = {
        row[0] for row in bind.execute(sa.text("SELECT id FROM workflow_definitions"))
    }
    definition_rows = []
    if legacy_definition_id not in existing_definition_ids:
        definition_rows.append(
            {
                "id": legacy_definition_id,
                "workflow_code": "LEMONFLOW_LEGACY",
                "version": "Legacy_V0",
                "definition_json": {"mode": "legacy", "entry": "historical-read-only"},
                "status": "PUBLISHED",
                "published_at": now,
                "created_at": now,
                "updated_at": now,
            }
        )
    if v1_definition_id not in existing_definition_ids:
        definition_rows.append(
            {
                "id": v1_definition_id,
                "workflow_code": "LEMONFLOW_PRODUCTION",
                "version": "LemonFlow_V1",
                "definition_json": {
                    "mode": "v1-production",
                    "stages": [
                        "REFERENCE_ANALYSIS",
                        "ANALYSIS_REVIEW",
                        "STORY_GENERATION",
                        "STORY_REVIEW",
                        "CHARACTER_ASSETS",
                        "SCENE_ASSETS",
                        "DIRECTOR_PLANNING",
                        "SHOT_KEYFRAMES",
                        "VIDEO_GENERATION",
                        "VIDEO_REVIEW",
                        "FINAL_EXPORT",
                    ],
                },
                "status": "PUBLISHED",
                "published_at": now,
                "created_at": now,
                "updated_at": now,
            }
        )
    if definition_rows:
        op.bulk_insert(workflow_definitions, definition_rows)

    # 已存在的运行永远归入历史版本；不会因为升级被误解释为 V1 任务。
    bind.execute(
        sa.text(
            "UPDATE workflow_runs "
            "SET workflow_definition_id = :definition_id, workflow_version = 'Legacy_V0', "
            "input_snapshot = COALESCE(input_snapshot, :empty_snapshot) "
            "WHERE workflow_definition_id IS NULL"
        ),
        {"definition_id": legacy_definition_id, "empty_snapshot": "{}"},
    )

    project_state_ids = {
        row[0] for row in bind.execute(sa.text("SELECT project_id FROM project_production_states"))
    }
    project_rows = bind.execute(sa.text("SELECT id FROM projects")).fetchall()
    project_states = sa.table(
        "project_production_states",
        sa.column("project_id", sa.String),
        sa.column("active_stage", sa.String),
        sa.column("workflow_definition_id", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    missing_states = [
        {
            "project_id": project_id,
            "active_stage": "LEGACY_READONLY",
            "workflow_definition_id": legacy_definition_id,
            "created_at": now,
            "updated_at": now,
        }
        for (project_id,) in project_rows
        if project_id not in project_state_ids
    ]
    if missing_states:
        op.bulk_insert(project_states, missing_states)

    # 新字段仅从旧的非敏感配置派生，不读取任何环境变量或真实密钥。
    model_rows = bind.execute(
        sa.text("SELECT id, provider_key, model_key, provider_config FROM model_profiles")
    ).mappings()
    for row in model_rows:
        config = row["provider_config"] if isinstance(row["provider_config"], dict) else {}
        display_name = config.get("display_name") if isinstance(config.get("display_name"), str) else row["model_key"]
        bind.execute(
            sa.text(
                "UPDATE model_profiles SET adapter_key = COALESCE(adapter_key, :adapter_key), "
                "model_version = COALESCE(model_version, :model_version), "
                "display_name = COALESCE(display_name, :display_name) WHERE id = :id"
            ),
            {
                "id": row["id"],
                "adapter_key": row["provider_key"],
                "model_version": row["model_key"],
                "display_name": display_name,
            },
        )

    model_slots = sa.table(
        "model_slots",
        sa.column("id", sa.String),
        sa.column("slot_key", sa.String),
        sa.column("capability", sa.String),
        sa.column("selection_mode", sa.String),
        sa.column("description", sa.Text),
        sa.column("is_enabled", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    slot_specs = (
        ("VIDEO_ANALYSIS", "VIDEO_ANALYSIS", "SINGLE", "视频结构、开头、爆点、场景与创作简报分析"),
        ("STORY_GENERATE", "STORY_GENERATE", "MULTI_PARALLEL", "多个编剧模型并行产出原创故事方案"),
        ("CHARACTER_DESIGN", "CHARACTER_DESIGN", "SINGLE", "角色文字资产设计"),
        ("SCENE_DESIGN", "SCENE_DESIGN", "SINGLE", "场景文字资产设计"),
        ("DIRECTOR_PLAN", "DIRECTOR_PLAN", "SINGLE", "导演视觉方案与分镜规划"),
        ("CHARACTER_IMAGE_GENERATE", "IMAGE_GENERATE", "SINGLE", "角色参考图生成"),
        ("SCENE_IMAGE_GENERATE", "IMAGE_GENERATE", "SINGLE", "场景参考图生成"),
        ("SHOT_KEYFRAME_GENERATE", "IMAGE_GENERATE", "SINGLE", "分镜关键画面生成"),
        ("VIDEO_GENERATE", "VIDEO_GENERATE", "SINGLE", "锁定资产驱动的视频片段生成"),
        ("FINAL_COMPOSE", "FINAL_COMPOSE", "SINGLE", "审核通过片段的成片合成"),
    )
    existing_slots = {row[0] for row in bind.execute(sa.text("SELECT slot_key FROM model_slots"))}
    slot_rows = [
        {
            "id": _id(f"slot/{slot_key}"),
            "slot_key": slot_key,
            "capability": capability,
            "selection_mode": selection_mode,
            "description": description,
            "is_enabled": True,
            "created_at": now,
            "updated_at": now,
        }
        for slot_key, capability, selection_mode, description in slot_specs
        if slot_key not in existing_slots
    ]
    if slot_rows:
        op.bulk_insert(model_slots, slot_rows)

    # 旧配置仅绑定到历史兼容槽位，避免迁移后自动接管 V1 生产流。
    legacy_slot_by_step = {
        "analyze_reference_mechanisms": "LEGACY_VIDEO_ANALYSIS",
        "generate_original_topics": "OPTIONAL_TOPIC_GENERATE",
        "generate_story_package": "LEGACY_STORY_GENERATE",
        "generate_storyboard": "LEGACY_STORYBOARD",
        "generate_storyboard_images": "LEGACY_SINGLE_SHOT_IMAGE",
        "generate_storyboard_video_groups": "LEGACY_VIDEO_GENERATE",
        "assemble_final_video": "LEGACY_FINAL_COMPOSE",
    }
    existing_slots = {row[0] for row in bind.execute(sa.text("SELECT slot_key FROM model_slots"))}
    legacy_slot_rows = []
    for slot_key in sorted(set(legacy_slot_by_step.values())):
        if slot_key not in existing_slots:
            legacy_slot_rows.append(
                {
                    "id": _id(f"slot/{slot_key}"),
                    "slot_key": slot_key,
                    "capability": "LEGACY_COMPATIBILITY",
                    "selection_mode": "SINGLE",
                    "description": "历史工作流兼容槽位，不作为 LemonFlow V1 主流程默认模型。",
                    "is_enabled": False,
                    "created_at": now,
                    "updated_at": now,
                }
            )
    if legacy_slot_rows:
        op.bulk_insert(model_slots, legacy_slot_rows)

    profile_rows = bind.execute(sa.text("SELECT id, step_key FROM model_profiles")).fetchall()
    bindings = sa.table(
        "model_slot_profile_bindings",
        sa.column("id", sa.String),
        sa.column("slot_id", sa.String),
        sa.column("model_profile_id", sa.String),
        sa.column("is_enabled", sa.Boolean),
        sa.column("priority", sa.Integer),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    existing_bindings = {
        (row[0], row[1])
        for row in bind.execute(sa.text("SELECT slot_id, model_profile_id FROM model_slot_profile_bindings"))
    }
    binding_rows = []
    for profile_id, step_key in profile_rows:
        slot_key = legacy_slot_by_step.get(step_key)
        if slot_key is None:
            continue
        slot_id = _id(f"slot/{slot_key}")
        if (slot_id, profile_id) not in existing_bindings:
            binding_rows.append(
                {
                    "id": _id(f"binding/{slot_key}/{profile_id}"),
                    "slot_id": slot_id,
                    "model_profile_id": profile_id,
                    "is_enabled": False,
                    "priority": 100,
                    "created_at": now,
                }
            )
    if binding_rows:
        op.bulk_insert(bindings, binding_rows)


def downgrade() -> None:
    """删除本次写入的种子数据；不删除用户自行新增的 V1 配置。"""

    bind = op.get_bind()
    # 仅清理由本迁移稳定 ID 创建的槽位/定义；外键依赖先删绑定与项目状态。
    slot_ids = [_id(f"slot/{slot_key}") for slot_key in (
        "VIDEO_ANALYSIS", "STORY_GENERATE", "CHARACTER_DESIGN", "SCENE_DESIGN", "DIRECTOR_PLAN",
        "CHARACTER_IMAGE_GENERATE", "SCENE_IMAGE_GENERATE", "SHOT_KEYFRAME_GENERATE", "VIDEO_GENERATE",
        "FINAL_COMPOSE", "LEGACY_VIDEO_ANALYSIS", "OPTIONAL_TOPIC_GENERATE", "LEGACY_STORY_GENERATE",
        "LEGACY_STORYBOARD", "LEGACY_SINGLE_SHOT_IMAGE", "LEGACY_VIDEO_GENERATE", "LEGACY_FINAL_COMPOSE",
    )]
    if slot_ids:
        bind.execute(sa.text("DELETE FROM model_slot_profile_bindings WHERE slot_id IN :slot_ids").bindparams(sa.bindparam("slot_ids", expanding=True)), {"slot_ids": slot_ids})
        bind.execute(sa.text("DELETE FROM model_slots WHERE id IN :slot_ids").bindparams(sa.bindparam("slot_ids", expanding=True)), {"slot_ids": slot_ids})
    bind.execute(sa.text("DELETE FROM project_production_states WHERE workflow_definition_id IN (:legacy, :v1)"), {"legacy": _id("workflow/legacy-v0"), "v1": _id("workflow/lemonflow-v1")})
    bind.execute(sa.text("DELETE FROM workflow_definitions WHERE id IN (:legacy, :v1)"), {"legacy": _id("workflow/legacy-v0"), "v1": _id("workflow/lemonflow-v1")})
