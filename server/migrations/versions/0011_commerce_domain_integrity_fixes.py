"""修复带货短剧领域的来源删除、数值和定位完整性。

Revision ID: 0011_commerce_domain_integrity_fixes
Revises: 0010_commerce_domain_foundation
Create Date: 2026-08-09

0010 已发布，本迁移只追加结构化产品分析字段，并为既有 Commerce 表增加约束或
调整来源媒体外键。它不删除 0010 表、不迁移旧项目，也不改写历史业务内容。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0011_commerce_domain_integrity_fixes"
down_revision = "0010_commerce_domain_foundation"
branch_labels = None
depends_on = None


_SQLITE_FK_NAMING = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}

_CHECKS: tuple[tuple[str, str, str], ...] = (
    ("script_analysis_versions", "ck_script_analysis_version_positive", "version >= 1"),
    ("product_asset_versions", "ck_product_asset_version_positive", "version >= 1"),
    ("story_runs", "ck_story_run_number_positive", "run_number >= 1"),
    ("story_outline_versions", "ck_story_outline_version_positive", "version >= 1"),
    ("chapter_plans", "ck_chapter_plan_number_positive", "chapter_number >= 1"),
    ("scene_mapping_versions", "ck_scene_mapping_version_positive", "version >= 1"),
    ("video_segment_plans", "ck_video_segment_number_positive", "segment_number >= 1"),
    ("sub_shot_plans", "ck_sub_shot_number_positive", "shot_number >= 1"),
    ("dialogue_lines", "ck_dialogue_line_end_maximum", "end_ms <= 15000"),
    ("render_batches", "ck_render_batch_number_positive", "batch_number >= 1"),
    ("render_batches", "ck_render_batch_cost_nonnegative", "estimated_cost IS NULL OR estimated_cost >= 0"),
    (
        "render_batches",
        "ck_render_batch_task_counts_within_total",
        "completed_tasks + failed_tasks + running_tasks <= total_tasks",
    ),
)


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _sqlite_source_media_fk_name() -> str:
    """读取 SQLite 当前来源媒体外键名，兼容 0010 原始表和本迁移回退后的表。"""

    for foreign_key in inspect(op.get_bind()).get_foreign_keys("product_analysis_versions"):
        if foreign_key.get("constrained_columns") == ["source_media_asset_id"]:
            # 0010 原始 SQLite 外键没有名字；batch naming convention 会把它映射为下面的
            # 稳定名称。0011 downgrade 后则保留显式 RESTRICT 名称。
            return foreign_key.get("name") or "fk_product_analysis_versions_source_media_asset_id_media_assets"
    raise RuntimeError("product_analysis_versions 缺少 source_media_asset_id 外键")


def _add_check_constraint(table_name: str, constraint_name: str, condition: str) -> None:
    """SQLite 用 batch 重建表追加 CHECK，PostgreSQL 直接 ALTER TABLE。"""

    if _is_sqlite():
        with op.batch_alter_table(table_name, recreate="always") as batch:
            batch.create_check_constraint(constraint_name, condition)
    else:
        op.create_check_constraint(constraint_name, table_name, condition)


def _drop_check_constraint(table_name: str, constraint_name: str) -> None:
    """撤销本迁移新增的 CHECK，不触及 0010 已有约束。"""

    if _is_sqlite():
        with op.batch_alter_table(table_name, recreate="always") as batch:
            batch.drop_constraint(constraint_name, type_="check")
    else:
        op.drop_constraint(constraint_name, table_name, type_="check")


def _product_analysis_columns() -> tuple[sa.Column, ...]:
    """可查询产品分析候选字段；默认空 JSON，避免共享可变默认值。"""

    return (
        sa.Column("product_identification", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("package_ocr", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("candidate_reference_images", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("appearance_description_candidates", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("selling_point_candidates", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("user_pain_point_candidates", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("usage_scenario_candidates", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def _upgrade_product_analysis_versions() -> None:
    """补齐结构化字段，并将来源媒体外键改为 ``SET NULL``。"""

    if _is_sqlite():
        # 0010 的 SQLite 外键没有显式名称；batch naming convention 令它可安全定位。
        source_fk_name = _sqlite_source_media_fk_name()
        with op.batch_alter_table(
            "product_analysis_versions",
            recreate="always",
            naming_convention=_SQLITE_FK_NAMING,
        ) as batch:
            for column in _product_analysis_columns():
                batch.add_column(column)
            batch.drop_constraint(source_fk_name, type_="foreignkey")
            batch.create_foreign_key(
                "fk_product_analysis_source_media_set_null",
                "media_assets",
                ["source_media_asset_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch.create_check_constraint("ck_product_analysis_version_positive", "version >= 1")
    else:
        for column in _product_analysis_columns():
            op.add_column("product_analysis_versions", column)
        op.drop_constraint(
            "product_analysis_versions_source_media_asset_id_fkey",
            "product_analysis_versions",
            type_="foreignkey",
        )
        op.create_foreign_key(
            "fk_product_analysis_source_media_set_null",
            "product_analysis_versions",
            "media_assets",
            ["source_media_asset_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_check_constraint("ck_product_analysis_version_positive", "product_analysis_versions", "version >= 1")


def _downgrade_product_analysis_versions() -> None:
    """撤销新增列，并恢复 0010 的来源媒体 RESTRICT 语义。"""

    if _is_sqlite():
        source_fk_name = _sqlite_source_media_fk_name()
        with op.batch_alter_table(
            "product_analysis_versions",
            recreate="always",
            naming_convention=_SQLITE_FK_NAMING,
        ) as batch:
            batch.drop_constraint(source_fk_name, type_="foreignkey")
            batch.create_foreign_key(
                "fk_product_analysis_source_media_restrict",
                "media_assets",
                ["source_media_asset_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            batch.drop_constraint("ck_product_analysis_version_positive", type_="check")
            for column in reversed(_product_analysis_columns()):
                batch.drop_column(column.name)
    else:
        op.drop_constraint(
            "fk_product_analysis_source_media_set_null",
            "product_analysis_versions",
            type_="foreignkey",
        )
        op.create_foreign_key(
            "product_analysis_versions_source_media_asset_id_fkey",
            "product_analysis_versions",
            "media_assets",
            ["source_media_asset_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.drop_constraint("ck_product_analysis_version_positive", "product_analysis_versions", type_="check")
        for column in reversed(_product_analysis_columns()):
            op.drop_column("product_analysis_versions", column.name)


def _replace_product_placement_location_constraint(*, upgrade: bool) -> None:
    """把 0010 的“至少一个”定位改为严格的章节/片段/子镜头三选一。"""

    old_name = "ck_product_placement_has_location"
    new_name = "ck_product_placement_single_location"
    exclusive_condition = (
        "(chapter_id IS NOT NULL AND video_segment_id IS NULL AND sub_shot_id IS NULL) OR "
        "(chapter_id IS NULL AND video_segment_id IS NOT NULL AND sub_shot_id IS NULL) OR "
        "(chapter_id IS NULL AND video_segment_id IS NULL AND sub_shot_id IS NOT NULL)"
    )
    old_condition = "chapter_id IS NOT NULL OR video_segment_id IS NOT NULL OR sub_shot_id IS NOT NULL"
    drop_name, create_name, create_condition = (
        (old_name, new_name, exclusive_condition)
        if upgrade
        else (new_name, old_name, old_condition)
    )
    if _is_sqlite():
        with op.batch_alter_table("product_placement_plans", recreate="always") as batch:
            batch.drop_constraint(drop_name, type_="check")
            batch.create_check_constraint(create_name, create_condition)
    else:
        op.drop_constraint(drop_name, "product_placement_plans", type_="check")
        op.create_check_constraint(create_name, "product_placement_plans", create_condition)


def upgrade() -> None:
    """以最小增量修正 Commerce 领域完整性。"""

    _upgrade_product_analysis_versions()
    for table_name, constraint_name, condition in _CHECKS:
        _add_check_constraint(table_name, constraint_name, condition)
    _replace_product_placement_location_constraint(upgrade=True)


def downgrade() -> None:
    """只撤销 0011 的字段、外键和约束，回到 0010。"""

    _replace_product_placement_location_constraint(upgrade=False)
    for table_name, constraint_name, _ in reversed(_CHECKS):
        _drop_check_constraint(table_name, constraint_name)
    _downgrade_product_analysis_versions()
