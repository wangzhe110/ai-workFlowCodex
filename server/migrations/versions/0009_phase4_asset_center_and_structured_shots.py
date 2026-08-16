"""新增可跨项目复用的资产中心和结构化导演分镜字段。

Revision ID: 0009_phase4_asset_center_and_structured_shots
Revises: 0008_v1_model_profile_editing
Create Date: 2026-08-07

本迁移只新增表、字段和索引，不删除或改写任何既有项目、锁图、分镜、视频片段或
Workflow 快照。已有锁图会在下一次进入导演阶段时由服务层惰性补齐资产中心版本，
避免迁移期间擅自改变历史生产结果。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy import inspect


revision = "0009_phase4_asset_center_and_structured_shots"
down_revision = "0008_v1_model_profile_editing"
branch_labels = None
depends_on = None


# PostgreSQL 仅允许 63 字节标识符。该旧名称在 SQLAlchemy 正常执行路径中会在
# 发送 DDL 前报错；保留其 63 字节候选名仅用于兼容旧 SQLite 半完成迁移或人工
# 以原始 SQL 建立过同字段索引的数据库，避免重复创建等价索引。
_CHARACTER_VERSION_INDEX = "ix_pcar_char_asset_version_id"
_LEGACY_TRUNCATED_CHARACTER_VERSION_INDEX = "ix_project_character_asset_references_character_asset_version_i"

# 这些列在 0009 才出现，因此不能在 0003 的历史建表快照中提前带入。资产表
# 已创建后立即建立外键，避免 PostgreSQL 与 SQLite 对“引用尚未建表”的不同容忍度
# 造成最终结构漂移。未指定 ON DELETE / ON UPDATE，保持 ORM 的默认 NO ACTION 语义。
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

# 必须以单一对象声明资产库类型。0001 的历史动态元数据曾在部分旧环境中提前创建
# PostgreSQL ``assetlibrarykind``；另一些按固定 0003 快照升级的环境则没有该类型。
# 0009 在创建资产表前显式、幂等地确保类型存在，随后 PostgreSQL 列定义不再隐式
# CREATE TYPE。SQLite 使用常规 Enum 编译为字符串检查类型。字段值和类型名称均不变。
_ASSET_LIBRARY_KIND = sa.Enum("CHARACTER", "SCENE", name="assetlibrarykind")
_POSTGRES_ASSET_LIBRARY_KIND = postgresql.ENUM(
    "CHARACTER", "SCENE", name="assetlibrarykind", create_type=False
)


def _tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    return {item.get("name") for item in inspect(op.get_bind()).get_indexes(table_name)}


def _has_foreign_key(table_name: str, local_column: str, referent_table: str) -> bool:
    """按字段与目标表识别外键，兼容历史未命名约束。"""

    for item in inspect(op.get_bind()).get_foreign_keys(table_name):
        if (
            item.get("constrained_columns") == [local_column]
            and item.get("referred_table") == referent_table
            and item.get("referred_columns") == ["id"]
        ):
            return True
    return False


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name in _columns(table_name):
        return
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(table_name) as batch:
            batch.add_column(column)
    else:
        op.add_column(table_name, column)


def _create_asset_foreign_key_if_missing(
    name: str,
    source_table: str,
    local_column: str,
    referent_table: str,
) -> None:
    """为 0009 新列建立与 ORM 一致的外键，且允许重复升级。"""

    if _has_foreign_key(source_table, local_column, referent_table):
        return
    if op.get_bind().dialect.name == "sqlite":
        # SQLite 不支持 ADD CONSTRAINT；Alembic batch 会重建该单表并保留原有数据、
        # 索引和约束。迁移环境在重建期间统一处理 foreign_keys 开关。
        with op.batch_alter_table(source_table) as batch:
            batch.create_foreign_key(name, referent_table, [local_column], ["id"])
        return
    op.create_foreign_key(name, source_table, referent_table, [local_column], ["id"])


def _create_asset_tables() -> None:
    """创建新资产中心表；此处没有历史数据迁移或删除操作。"""

    if "asset_libraries" not in _tables():
        if op.get_bind().dialect.name == "postgresql":
            # 不能依赖 0001 的动态元数据是否曾偶然创建该类型。duplicate_object 被
            # 显式忽略，以兼容已运行过旧迁移的 PostgreSQL 数据库。
            op.execute(
                """
                DO $$
                BEGIN
                    CREATE TYPE assetlibrarykind AS ENUM ('CHARACTER', 'SCENE');
                EXCEPTION
                    WHEN duplicate_object THEN NULL;
                END $$;
                """
            )
        op.create_table(
            "asset_libraries",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "kind",
                _POSTGRES_ASSET_LIBRARY_KIND
                if op.get_bind().dialect.name == "postgresql"
                else _ASSET_LIBRARY_KIND,
                nullable=False,
            ),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVE"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("kind", "name", name="uq_asset_library_kind_name"),
        )
        op.create_index("ix_asset_libraries_kind", "asset_libraries", ["kind"])
    if "character_assets" not in _tables():
        op.create_table(
            "character_assets",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("library_id", sa.String(length=36), sa.ForeignKey("asset_libraries.id"), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVE"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("library_id", "name", name="uq_character_asset_library_name"),
        )
        op.create_index("ix_character_assets_library_id", "character_assets", ["library_id"])
    if "character_asset_versions" not in _tables():
        op.create_table(
            "character_asset_versions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("character_asset_id", sa.String(length=36), sa.ForeignKey("character_assets.id"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("age", sa.String(length=120), nullable=True),
            sa.Column("gender", sa.String(length=40), nullable=True),
            sa.Column("personality", sa.Text(), nullable=True),
            sa.Column("style", sa.Text(), nullable=True),
            sa.Column("appearance", sa.Text(), nullable=True),
            sa.Column("costume", sa.Text(), nullable=True),
            sa.Column("reference_images", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("character_asset_id", "version", name="uq_character_asset_version"),
        )
        op.create_index("ix_character_asset_versions_character_asset_id", "character_asset_versions", ["character_asset_id"])
    if "scene_assets" not in _tables():
        op.create_table(
            "scene_assets",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("library_id", sa.String(length=36), sa.ForeignKey("asset_libraries.id"), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVE"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("library_id", "name", name="uq_scene_asset_library_name"),
        )
        op.create_index("ix_scene_assets_library_id", "scene_assets", ["library_id"])
    if "scene_asset_versions" not in _tables():
        op.create_table(
            "scene_asset_versions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("scene_asset_id", sa.String(length=36), sa.ForeignKey("scene_assets.id"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("style", sa.Text(), nullable=True),
            sa.Column("weather", sa.String(length=120), nullable=True),
            sa.Column("time_of_day", sa.String(length=120), nullable=True),
            sa.Column("location", sa.Text(), nullable=True),
            sa.Column("environment", sa.Text(), nullable=True),
            sa.Column("mood", sa.Text(), nullable=True),
            sa.Column("reference_images", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("scene_asset_id", "version", name="uq_scene_asset_version"),
        )
        op.create_index("ix_scene_asset_versions_scene_asset_id", "scene_asset_versions", ["scene_asset_id"])


def _create_project_reference_tables() -> None:
    if "project_character_asset_references" not in _tables():
        op.create_table(
            "project_character_asset_references",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("character_definition_id", sa.String(length=36), sa.ForeignKey("character_definitions.id"), nullable=False),
            sa.Column("character_asset_id", sa.String(length=36), sa.ForeignKey("character_assets.id"), nullable=False),
            sa.Column("character_asset_version_id", sa.String(length=36), sa.ForeignKey("character_asset_versions.id"), nullable=False),
            sa.Column("source_reference_image_id", sa.String(length=36), sa.ForeignKey("character_reference_images.id"), nullable=True),
            sa.Column("is_selected", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("character_definition_id", "character_asset_version_id", name="uq_project_character_asset_version_reference"),
        )
        op.create_index("ix_project_character_asset_references_project_id", "project_character_asset_references", ["project_id"])
        op.create_index("ix_project_character_asset_references_character_definition_id", "project_character_asset_references", ["character_definition_id"])
        op.create_index("ix_project_character_asset_references_character_asset_id", "project_character_asset_references", ["character_asset_id"])
        op.create_index("ix_project_character_asset_references_source_reference_image_id", "project_character_asset_references", ["source_reference_image_id"])
        op.create_index(
            "uq_selected_project_character_asset", "project_character_asset_references", ["character_definition_id"], unique=True,
            postgresql_where=sa.text("is_selected = true"), sqlite_where=sa.text("is_selected = 1"),
        )
    # PostgreSQL 标识符上限为 63 字节。原名称为 64 字节，会让整个 0009
    # 事务回滚；这里仅缩短索引名称，不改变索引字段、顺序或业务语义。
    # 同时让 SQLite 的旧半完成升级可安全重试。
    character_reference_indexes = _indexes("project_character_asset_references")
    if (
        _CHARACTER_VERSION_INDEX not in character_reference_indexes
        and _LEGACY_TRUNCATED_CHARACTER_VERSION_INDEX not in character_reference_indexes
    ):
        op.create_index(
            _CHARACTER_VERSION_INDEX,
            "project_character_asset_references",
            ["character_asset_version_id"],
        )
    if "project_scene_asset_references" not in _tables():
        op.create_table(
            "project_scene_asset_references",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("scene_definition_id", sa.String(length=36), sa.ForeignKey("scene_definitions.id"), nullable=False),
            sa.Column("scene_asset_id", sa.String(length=36), sa.ForeignKey("scene_assets.id"), nullable=False),
            sa.Column("scene_asset_version_id", sa.String(length=36), sa.ForeignKey("scene_asset_versions.id"), nullable=False),
            sa.Column("source_reference_image_id", sa.String(length=36), sa.ForeignKey("scene_reference_images.id"), nullable=True),
            sa.Column("is_selected", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("scene_definition_id", "scene_asset_version_id", name="uq_project_scene_asset_version_reference"),
        )
        op.create_index("ix_project_scene_asset_references_project_id", "project_scene_asset_references", ["project_id"])
        op.create_index("ix_project_scene_asset_references_scene_definition_id", "project_scene_asset_references", ["scene_definition_id"])
        op.create_index("ix_project_scene_asset_references_scene_asset_id", "project_scene_asset_references", ["scene_asset_id"])
        op.create_index("ix_project_scene_asset_references_scene_asset_version_id", "project_scene_asset_references", ["scene_asset_version_id"])
        op.create_index("ix_project_scene_asset_references_source_reference_image_id", "project_scene_asset_references", ["source_reference_image_id"])
        op.create_index(
            "uq_selected_project_scene_asset", "project_scene_asset_references", ["scene_definition_id"], unique=True,
            postgresql_where=sa.text("is_selected = true"), sqlite_where=sa.text("is_selected = 1"),
        )


def upgrade() -> None:
    _create_asset_tables()
    _add_column_if_missing("character_definitions", sa.Column("asset_library_character_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("scene_definitions", sa.Column("asset_library_scene_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("character_reference_images", sa.Column("asset_version_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("scene_reference_images", sa.Column("asset_version_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("shot_asset_bindings", sa.Column("character_asset_version_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("shot_asset_bindings", sa.Column("scene_asset_version_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("video_clip_asset_bindings", sa.Column("character_asset_version_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("video_clip_asset_bindings", sa.Column("scene_asset_version_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("shot_plans", sa.Column("emotion", sa.Text(), nullable=False, server_default="未指定"))
    _add_column_if_missing("shot_plans", sa.Column("camera_type", sa.String(length=120), nullable=False, server_default="中景"))
    _add_column_if_missing("shot_plans", sa.Column("camera_move", sa.Text(), nullable=False, server_default="固定机位"))
    _add_column_if_missing("shot_plans", sa.Column("lighting", sa.Text(), nullable=False, server_default="自然光"))
    _add_column_if_missing("shot_plans", sa.Column("image_prompt", sa.Text(), nullable=False, server_default=""))
    _add_column_if_missing("shot_plans", sa.Column("video_prompt", sa.Text(), nullable=False, server_default=""))
    _add_column_if_missing("shot_plans", sa.Column("sound_prompt", sa.Text(), nullable=False, server_default=""))

    # 必须晚于 _create_asset_tables()；否则空 PostgreSQL 会因引用的资产表不存在而
    # 失败。与 0003 固定快照配合后，空库和历史路径都得到同一套外键结构。
    for name, source_table, local_column, referent_table in _ASSET_FOREIGN_KEYS:
        _create_asset_foreign_key_if_missing(name, source_table, local_column, referent_table)
    _create_project_reference_tables()

    # 为热点追溯路径补充普通索引，避免资产中心进入后项目生产台 N+1 扫描。
    for table, name, columns in (
        ("character_definitions", "ix_character_definitions_asset_library_character_id", ["asset_library_character_id"]),
        ("scene_definitions", "ix_scene_definitions_asset_library_scene_id", ["asset_library_scene_id"]),
        ("character_reference_images", "ix_character_reference_images_asset_version_id", ["asset_version_id"]),
        ("scene_reference_images", "ix_scene_reference_images_asset_version_id", ["asset_version_id"]),
        ("shot_asset_bindings", "ix_shot_asset_bindings_character_asset_version_id", ["character_asset_version_id"]),
        ("shot_asset_bindings", "ix_shot_asset_bindings_scene_asset_version_id", ["scene_asset_version_id"]),
        ("video_clip_asset_bindings", "ix_video_clip_asset_bindings_character_asset_version_id", ["character_asset_version_id"]),
        ("video_clip_asset_bindings", "ix_video_clip_asset_bindings_scene_asset_version_id", ["scene_asset_version_id"]),
    ):
        if name not in _indexes(table):
            op.create_index(name, table, columns)


def downgrade() -> None:
    raise RuntimeError("资产中心含跨项目生产引用；请从升级前备份恢复，禁止危险回退")
