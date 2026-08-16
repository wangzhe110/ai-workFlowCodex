"""补齐已运行 0009 数据库缺失的资产中心外键。

Revision ID: 0020_phase4_asset_center_foreign_key_repair
Revises: 0019_commerce_step_scope_guard_enum_compatibility
Create Date: 2026-08-13

0009 的早期已部署版本只添加了资产版本 ID 列和索引，未将这些列约束到资产中心
表。0003 现已使用固定历史快照，0009 的全新路径会直接创建同一组外键；本迁移只
负责使已经到达 0019 的数据库收敛到相同结构。

迁移绝不回填、删除或改写业务数据。安装每条外键前都会检测悬挂引用；若历史数据
无法安全约束，升级明确失败，要求从备份或人工数据修复流程处理。
"""

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0020_phase4_asset_center_foreign_key_repair"
down_revision = "0019_commerce_step_scope_guard_enum_compatibility"
branch_labels = None
depends_on = None


# 与 0009 完全一致：同字段、同目标、无 ON DELETE / ON UPDATE 覆盖，故默认均为
# NO ACTION。稳定命名让 PostgreSQL/SQLite 的 upgrade、downgrade 都可审计。
_ASSET_FOREIGN_KEYS = (
    ("fk_char_def_asset_library_character", "character_definitions", "asset_library_character_id", "character_assets"),
    ("fk_scene_def_asset_library_scene", "scene_definitions", "asset_library_scene_id", "scene_assets"),
    ("fk_char_ref_image_asset_version", "character_reference_images", "asset_version_id", "character_asset_versions"),
    ("fk_scene_ref_image_asset_version", "scene_reference_images", "asset_version_id", "scene_asset_versions"),
    ("fk_shot_binding_char_asset_version", "shot_asset_bindings", "character_asset_version_id", "character_asset_versions"),
    ("fk_shot_binding_scene_asset_version", "shot_asset_bindings", "scene_asset_version_id", "scene_asset_versions"),
    ("fk_clip_binding_char_asset_version", "video_clip_asset_bindings", "character_asset_version_id", "character_asset_versions"),
    ("fk_clip_binding_scene_asset_version", "video_clip_asset_bindings", "scene_asset_version_id", "scene_asset_versions"),
)


def _table_names() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _column_names(table_name: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def _foreign_keys(table_name: str) -> list[dict]:
    return inspect(op.get_bind()).get_foreign_keys(table_name)


def _has_matching_foreign_key(source_table: str, local_column: str, referent_table: str) -> bool:
    """按语义识别既有外键，兼容旧环境可能使用过的约束名称。"""

    return any(
        item.get("constrained_columns") == [local_column]
        and item.get("referred_table") == referent_table
        and item.get("referred_columns") == ["id"]
        for item in _foreign_keys(source_table)
    )


def _assert_no_orphaned_references(source_table: str, local_column: str, referent_table: str) -> None:
    """在加外键前只读验证历史数据，不能以删除或回填掩盖问题。"""

    bind = op.get_bind()
    count = bind.execute(
        sa.text(
            f"SELECT COUNT(*) FROM {source_table} AS source "
            f"WHERE source.{local_column} IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {referent_table} AS target "
            f"WHERE target.id = source.{local_column})"
        )
    ).scalar_one()
    if count:
        raise RuntimeError(
            f"0020 无法安全为 {source_table}.{local_column} 添加外键："
            f"发现 {count} 条引用不存在的 {referent_table}.id；迁移没有修改数据。"
        )


def _create_foreign_key(name: str, source_table: str, local_column: str, referent_table: str) -> None:
    if _has_matching_foreign_key(source_table, local_column, referent_table):
        return
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(source_table) as batch:
            batch.create_foreign_key(name, referent_table, [local_column], ["id"])
        return
    op.create_foreign_key(name, source_table, referent_table, [local_column], ["id"])


def _drop_named_foreign_key_if_present(name: str, source_table: str) -> None:
    if name not in {item.get("name") for item in _foreign_keys(source_table)}:
        return
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(source_table) as batch:
            batch.drop_constraint(name, type_="foreignkey")
        return
    op.drop_constraint(name, source_table, type_="foreignkey")


def upgrade() -> None:
    """使已部署 0009/0019 数据库的资产中心引用完整性追上全新升级路径。"""

    # ``--sql`` 使用 MockConnection，无法读取数据库目录或历史数据。离线脚本只
    # 输出与在线升级相同的 DDL；实际数据完整性检查必须由在线路径在执行前完成。
    if context.is_offline_mode():
        for name, source_table, local_column, referent_table in _ASSET_FOREIGN_KEYS:
            op.create_foreign_key(name, source_table, referent_table, [local_column], ["id"])
        return

    tables = _table_names()
    for _name, source_table, local_column, referent_table in _ASSET_FOREIGN_KEYS:
        if source_table not in tables or referent_table not in tables:
            raise RuntimeError(
                f"0020 需要 0009 资产中心结构，但缺少表：{source_table} 或 {referent_table}"
            )
        if local_column not in _column_names(source_table):
            raise RuntimeError(f"0020 需要列 {source_table}.{local_column}，但当前数据库缺失该列")

    for name, source_table, local_column, referent_table in _ASSET_FOREIGN_KEYS:
        _assert_no_orphaned_references(source_table, local_column, referent_table)
        _create_foreign_key(name, source_table, local_column, referent_table)


def downgrade() -> None:
    """精确还原 0019 的结构：仅移除 0020 新增的命名外键。"""

    if context.is_offline_mode():
        for name, source_table, _local_column, _referent_table in reversed(_ASSET_FOREIGN_KEYS):
            op.drop_constraint(name, source_table, type_="foreignkey")
        return

    for name, source_table, _local_column, _referent_table in reversed(_ASSET_FOREIGN_KEYS):
        _drop_named_foreign_key_if_present(name, source_table)
