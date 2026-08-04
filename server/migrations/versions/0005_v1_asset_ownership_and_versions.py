"""修正 V1 基础资产归属与不可变版本的选择语义。

Revision ID: 0005_v1_asset_ownership_and_versions
Revises: 0004_v1_legacy_backfill
Create Date: 2026-08-03

角色和场景必须在 AI 导演分镜前完成，故其主归属应是已选故事而非 DirectorPlan。
同时，锁定的是某个不可变版本，父对象的当前指针才是“本轮采用版本”；历史锁定版
必须能够并存，供视频片段完整回溯。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0005_v1_asset_ownership_and_versions"
down_revision = "0004_v1_legacy_backfill"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> dict[str, dict]:
    """读取现有列，兼容新库和曾运行过 0003 草稿结构的数据库。"""

    return {column["name"]: column for column in inspect(op.get_bind()).get_columns(table_name)}


def _unique_names(table_name: str) -> set[str]:
    return {item.get("name") for item in inspect(op.get_bind()).get_unique_constraints(table_name)}


def _index_names(table_name: str) -> set[str]:
    return {item.get("name") for item in inspect(op.get_bind()).get_indexes(table_name)}


def _add_story_ownership(
    *,
    table_name: str,
    code_column: str,
    old_unique_name: str,
    new_unique_name: str,
) -> None:
    """将角色/场景从旧 DirectorPlan 归属迁移到已选故事。

    迁移不能猜测归属：若旧记录无法从 DirectorPlan 找回故事，直接中止而不是生成
    不可追溯的数据。正常 V1 草稿数据都有 DirectorPlan -> StoryProposal 外键。
    """

    bind = op.get_bind()
    columns = _columns(table_name)
    added_story_column = "story_proposal_id" not in columns
    if added_story_column:
        op.add_column(table_name, sa.Column("story_proposal_id", sa.String(length=36), nullable=True))
        bind.execute(
            sa.text(
                f"UPDATE {table_name} SET story_proposal_id = "
                "(SELECT story_proposal_id FROM director_plans "
                f"WHERE director_plans.id = {table_name}.director_plan_id) "
                "WHERE story_proposal_id IS NULL"
            )
        )
        unresolved = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {table_name} WHERE story_proposal_id IS NULL")
        ).scalar_one()
        if unresolved:
            raise RuntimeError(
                f"{table_name} 有 {unresolved} 条记录无法追溯到故事；请先备份并人工处理后再升级"
            )

    columns = _columns(table_name)
    old_unique_exists = old_unique_name in _unique_names(table_name)
    new_unique_exists = new_unique_name in _unique_names(table_name)
    director_is_required = not columns["director_plan_id"]["nullable"]
    story_is_required = not columns["story_proposal_id"]["nullable"]

    if not (old_unique_exists or not new_unique_exists or director_is_required or not story_is_required):
        return

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(table_name) as batch:
            if old_unique_exists:
                batch.drop_constraint(old_unique_name, type_="unique")
            if director_is_required:
                batch.alter_column("director_plan_id", existing_type=sa.String(length=36), nullable=True)
            if not story_is_required:
                batch.alter_column("story_proposal_id", existing_type=sa.String(length=36), nullable=False)
            if not new_unique_exists:
                batch.create_unique_constraint(new_unique_name, ["story_proposal_id", code_column])
        return

    if old_unique_exists:
        op.drop_constraint(old_unique_name, table_name, type_="unique")
    if director_is_required:
        op.alter_column(table_name, "director_plan_id", existing_type=sa.String(length=36), nullable=True)
    if not story_is_required:
        op.alter_column(table_name, "story_proposal_id", existing_type=sa.String(length=36), nullable=False)
    if not new_unique_exists:
        op.create_unique_constraint(new_unique_name, table_name, ["story_proposal_id", code_column])


def _replace_partial_indexes() -> None:
    """取消“只能有一个锁定版本”的错误限制，改为每批只选一份故事。"""

    for table_name, index_name in (
        ("story_proposals", "uq_selected_story_per_project"),
        ("character_reference_images", "uq_locked_character_reference_image"),
        ("scene_reference_images", "uq_locked_scene_reference_image"),
        ("shot_keyframes", "uq_locked_shot_keyframe"),
    ):
        if index_name in _index_names(table_name):
            op.drop_index(index_name, table_name=table_name)

    if "uq_selected_story_per_batch" not in _index_names("story_proposals"):
        op.create_index(
            "uq_selected_story_per_batch",
            "story_proposals",
            ["batch_id"],
            unique=True,
            postgresql_where=sa.text("status = 'SELECTED'"),
            sqlite_where=sa.text("status = 'SELECTED'"),
        )


def upgrade() -> None:
    """按正式主流程修正资产归属；所有数据更新均可由外键链路回溯。"""

    _add_story_ownership(
        table_name="character_definitions",
        code_column="character_code",
        old_unique_name="uq_character_plan_code",
        new_unique_name="uq_character_story_code",
    )
    _add_story_ownership(
        table_name="scene_definitions",
        code_column="scene_code",
        old_unique_name="uq_scene_plan_code",
        new_unique_name="uq_scene_story_code",
    )
    _replace_partial_indexes()


def downgrade() -> None:
    """生产数据已使用新资产版本语义时不允许危险的结构回退。"""

    raise RuntimeError("0005 包含资产归属语义修正；如需回退，请从升级前备份恢复数据库")
