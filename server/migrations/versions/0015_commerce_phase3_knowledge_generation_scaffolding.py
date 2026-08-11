"""Add Commerce Phase 3 knowledge, generation, access, and metering scaffolding.

Revision ID: 0015_commerce_phase3_knowledge_generation_scaffolding
Revises: 0014_commerce_phase2_legacy_compatibility
Create Date: 2026-08-11

This is intentionally additive.  It neither changes published V1/Commerce
workflow tables nor redirects an existing Worker to a new Provider boundary.
"""

from alembic import op
import sqlalchemy as sa


revision = "0015_commerce_phase3_knowledge_generation_scaffolding"
down_revision = "0014_commerce_phase2_legacy_compatibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "viral_cases",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_type", sa.String(length=60), nullable=False),
        sa.Column("source_identifier", sa.String(length=512), nullable=False),
        sa.Column("source_url", sa.String(length=1024), nullable=True),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("transcript_reference", sa.String(length=512), nullable=True),
        sa.Column("raw_analysis", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("structured_analysis", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("category", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVE"),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_viral_case_project", ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "source_type", "source_identifier", name="uq_viral_case_project_source"),
        sa.CheckConstraint("status IN ('DRAFT', 'ACTIVE', 'ARCHIVED')", name="ck_viral_case_status"),
    )
    op.create_index("ix_viral_cases_project_id", "viral_cases", ["project_id"])
    op.create_index("ix_viral_case_project_status_updated", "viral_cases", ["project_id", "status", "updated_at"])

    op.create_table(
        "viral_patterns",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("pattern_key", sa.String(length=36), nullable=False),
        sa.Column("source_case_id", sa.String(length=36), nullable=True),
        sa.Column("pattern_type", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("structured_rules", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("applicable_scenarios", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_viral_pattern_project", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_case_id"], ["viral_cases.id"], name="fk_viral_pattern_source_case", ondelete="SET NULL"),
        sa.UniqueConstraint("project_id", "pattern_key", "version", name="uq_viral_pattern_project_version"),
        sa.CheckConstraint("version >= 1", name="ck_viral_pattern_version_positive"),
        sa.CheckConstraint("status IN ('DRAFT', 'ACTIVE', 'ARCHIVED')", name="ck_viral_pattern_status"),
        sa.CheckConstraint(
            "pattern_type IN ('OPENING_HOOK', 'CONFLICT', 'RHYTHM', 'TRANSITION', 'CHARACTER_RELATIONSHIP', 'PRODUCT_PLACEMENT')",
            name="ck_viral_pattern_type",
        ),
    )
    op.create_index("ix_viral_patterns_project_id", "viral_patterns", ["project_id"])
    op.create_index("ix_viral_patterns_pattern_key", "viral_patterns", ["pattern_key"])
    op.create_index("ix_viral_patterns_source_case_id", "viral_patterns", ["source_case_id"])
    op.create_index("ix_viral_pattern_project_status_updated", "viral_patterns", ["project_id", "status", "updated_at"])
    op.create_index(
        "uq_viral_pattern_current", "viral_patterns", ["project_id", "pattern_key"], unique=True,
        postgresql_where=sa.text("is_current = true"), sqlite_where=sa.text("is_current = 1"),
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("viral_case_id", sa.String(length=36), nullable=True),
        sa.Column("viral_pattern_id", sa.String(length=36), nullable=True),
        sa.Column("resource_type", sa.String(length=30), nullable=False),
        sa.Column("resource_id", sa.String(length=36), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("embedding_provider", sa.String(length=80), nullable=True),
        sa.Column("embedding_model", sa.String(length=160), nullable=True),
        sa.Column("embedding_dimension", sa.Integer(), nullable=True),
        sa.Column("external_vector_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["viral_case_id"], ["viral_cases.id"], name="fk_knowledge_chunk_case", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["viral_pattern_id"], ["viral_patterns.id"], name="fk_knowledge_chunk_pattern", ondelete="CASCADE"),
        sa.UniqueConstraint("resource_type", "resource_id", "chunk_index", name="uq_knowledge_chunk_resource_position"),
        sa.CheckConstraint("chunk_index >= 0", name="ck_knowledge_chunk_index_nonnegative"),
        sa.CheckConstraint("resource_type IN ('VIRAL_CASE', 'VIRAL_PATTERN')", name="ck_knowledge_chunk_resource_type"),
        sa.CheckConstraint(
            "(viral_case_id IS NOT NULL AND viral_pattern_id IS NULL) OR "
            "(viral_case_id IS NULL AND viral_pattern_id IS NOT NULL)",
            name="ck_knowledge_chunk_exactly_one_source",
        ),
    )
    op.create_index("ix_knowledge_chunks_viral_case_id", "knowledge_chunks", ["viral_case_id"])
    op.create_index("ix_knowledge_chunks_viral_pattern_id", "knowledge_chunks", ["viral_pattern_id"])
    op.create_index("ix_knowledge_chunks_content_hash", "knowledge_chunks", ["content_hash"])
    op.create_index("ix_knowledge_chunk_resource", "knowledge_chunks", ["resource_type", "resource_id"])

    op.create_table(
        "retrieval_calls",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("provider_key", sa.String(length=80), nullable=False),
        sa.Column("request_id", sa.String(length=80), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("filter_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("result_references", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_summary", sa.String(length=500), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_retrieval_call_project", ondelete="CASCADE"),
        sa.CheckConstraint("status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')", name="ck_retrieval_call_status"),
    )
    op.create_index("ix_retrieval_calls_project_id", "retrieval_calls", ["project_id"])
    op.create_index("ix_retrieval_calls_request_id", "retrieval_calls", ["request_id"])
    op.create_index("ix_retrieval_call_project_created", "retrieval_calls", ["project_id", "created_at"])

    op.create_table(
        "generation_tasks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("modality", sa.String(length=20), nullable=False),
        sa.Column("capability", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=True),
        sa.Column("request_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("provider_key", sa.String(length=80), nullable=True),
        sa.Column("model_key", sa.String(length=160), nullable=True),
        sa.Column("provider_task_id", sa.String(length=255), nullable=True),
        sa.Column("output_reference", sa.JSON(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("fallback_used", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_generation_task_project", ondelete="CASCADE"),
        sa.CheckConstraint("modality IN ('IMAGE', 'VIDEO')", name="ck_generation_task_modality"),
        sa.CheckConstraint("status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')", name="ck_generation_task_status"),
    )
    op.create_index("ix_generation_tasks_project_id", "generation_tasks", ["project_id"])
    op.create_index("ix_generation_tasks_provider_task_id", "generation_tasks", ["provider_task_id"])
    op.create_index("ix_generation_task_project_created", "generation_tasks", ["project_id", "created_at"])
    op.create_index(
        "uq_generation_task_project_idempotency", "generation_tasks", ["project_id", "idempotency_key"], unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"), sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "generation_invocations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("generation_task_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("provider_key", sa.String(length=80), nullable=False),
        sa.Column("model_key", sa.String(length=160), nullable=False),
        sa.Column("request_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("sanitized_response", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("provider_task_id", sa.String(length=255), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["generation_task_id"], ["generation_tasks.id"], name="fk_generation_invocation_task", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_generation_invocation_project", ondelete="CASCADE"),
        sa.UniqueConstraint("generation_task_id", "attempt_number", name="uq_generation_invocation_attempt"),
        sa.CheckConstraint("attempt_number >= 1", name="ck_generation_invocation_attempt_positive"),
        sa.CheckConstraint("status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')", name="ck_generation_invocation_status"),
    )
    op.create_index("ix_generation_invocations_generation_task_id", "generation_invocations", ["generation_task_id"])
    op.create_index("ix_generation_invocations_project_id", "generation_invocations", ["project_id"])
    op.create_index("ix_generation_invocation_project_created", "generation_invocations", ["project_id", "created_at"])

    op.create_table(
        "project_members",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("principal_id", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_project_member_project", ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "principal_id", name="uq_project_member_principal"),
        sa.CheckConstraint("role IN ('OWNER', 'ADMIN', 'EDITOR', 'VIEWER')", name="ck_project_member_role"),
    )
    op.create_index("ix_project_members_project_id", "project_members", ["project_id"])

    op.create_table(
        "saas_plans",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("quota_policy", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("code", name="uq_saas_plan_code"),
    )

    op.create_table(
        "project_subscriptions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVE"),
        sa.Column("quota_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_project_subscription_project", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_id"], ["saas_plans.id"], name="fk_project_subscription_plan", ondelete="RESTRICT"),
        sa.CheckConstraint("status IN ('ACTIVE', 'CANCELLED', 'EXPIRED')", name="ck_project_subscription_status"),
    )
    op.create_index("ix_project_subscriptions_project_id", "project_subscriptions", ["project_id"])
    op.create_index("ix_project_subscriptions_plan_id", "project_subscriptions", ["plan_id"])
    op.create_index(
        "uq_active_project_subscription", "project_subscriptions", ["project_id"], unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"), sqlite_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_table(
        "usage_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("subscription_id", sa.String(length=36), nullable=True),
        sa.Column("correction_of_event_id", sa.String(length=36), nullable=True),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("event_kind", sa.String(length=20), nullable=False),
        sa.Column("capability", sa.String(length=80), nullable=False),
        sa.Column("unit", sa.String(length=80), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_usage_event_project", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subscription_id"], ["project_subscriptions.id"], name="fk_usage_event_subscription", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["correction_of_event_id"], ["usage_events.id"], name="fk_usage_event_correction", ondelete="SET NULL"),
        sa.UniqueConstraint("project_id", "idempotency_key", name="uq_usage_event_project_idempotency"),
        sa.CheckConstraint("quantity <> 0", name="ck_usage_event_nonzero_quantity"),
        sa.CheckConstraint("event_kind IN ('NORMAL', 'REVERSAL')", name="ck_usage_event_kind"),
    )
    op.create_index("ix_usage_events_project_id", "usage_events", ["project_id"])
    op.create_index("ix_usage_events_subscription_id", "usage_events", ["subscription_id"])
    op.create_index("ix_usage_events_correction_of_event_id", "usage_events", ["correction_of_event_id"])
    op.create_index("ix_usage_event_project_created", "usage_events", ["project_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_usage_event_project_created", table_name="usage_events")
    op.drop_index("ix_usage_events_correction_of_event_id", table_name="usage_events")
    op.drop_index("ix_usage_events_subscription_id", table_name="usage_events")
    op.drop_index("ix_usage_events_project_id", table_name="usage_events")
    op.drop_table("usage_events")
    op.drop_index("uq_active_project_subscription", table_name="project_subscriptions")
    op.drop_index("ix_project_subscriptions_plan_id", table_name="project_subscriptions")
    op.drop_index("ix_project_subscriptions_project_id", table_name="project_subscriptions")
    op.drop_table("project_subscriptions")
    op.drop_table("saas_plans")
    op.drop_index("ix_project_members_project_id", table_name="project_members")
    op.drop_table("project_members")
    op.drop_index("ix_generation_invocation_project_created", table_name="generation_invocations")
    op.drop_index("ix_generation_invocations_project_id", table_name="generation_invocations")
    op.drop_index("ix_generation_invocations_generation_task_id", table_name="generation_invocations")
    op.drop_table("generation_invocations")
    op.drop_index("uq_generation_task_project_idempotency", table_name="generation_tasks")
    op.drop_index("ix_generation_task_project_created", table_name="generation_tasks")
    op.drop_index("ix_generation_tasks_provider_task_id", table_name="generation_tasks")
    op.drop_index("ix_generation_tasks_project_id", table_name="generation_tasks")
    op.drop_table("generation_tasks")
    op.drop_index("ix_retrieval_call_project_created", table_name="retrieval_calls")
    op.drop_index("ix_retrieval_calls_request_id", table_name="retrieval_calls")
    op.drop_index("ix_retrieval_calls_project_id", table_name="retrieval_calls")
    op.drop_table("retrieval_calls")
    op.drop_index("ix_knowledge_chunk_resource", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_content_hash", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_viral_pattern_id", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_viral_case_id", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_index("uq_viral_pattern_current", table_name="viral_patterns")
    op.drop_index("ix_viral_pattern_project_status_updated", table_name="viral_patterns")
    op.drop_index("ix_viral_patterns_source_case_id", table_name="viral_patterns")
    op.drop_index("ix_viral_patterns_pattern_key", table_name="viral_patterns")
    op.drop_index("ix_viral_patterns_project_id", table_name="viral_patterns")
    op.drop_table("viral_patterns")
    op.drop_index("ix_viral_case_project_status_updated", table_name="viral_cases")
    op.drop_index("ix_viral_cases_project_id", table_name="viral_cases")
    op.drop_table("viral_cases")
