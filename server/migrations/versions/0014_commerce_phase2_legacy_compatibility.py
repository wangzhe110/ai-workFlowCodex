"""Backfill published Commerce Phase 2 data without rewriting prior revisions.

Revision ID: 0014_commerce_phase2_legacy_compatibility
Revises: 0013_commerce_phase2_integrity_fixes
Create Date: 2026-08-11

``0012`` and ``0013`` have already been published.  This revision is therefore
data-only: it validates pre-existing sidecar rows before relying on their
integrity rules, reconstructs deterministic CHAPTERS attempt membership, and
normalises the previously ambiguous VIDEO_PROMPTS review lifecycle.  It does
not alter the 0013 schema, indexes, or triggers, so downgrade restores the
exact 0013 database structure.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from alembic import context, op
import sqlalchemy as sa


revision = "0014_commerce_phase2_legacy_compatibility"
down_revision = "0013_commerce_phase2_integrity_fixes"
branch_labels = None
depends_on = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _payload(value: object) -> dict:
    """Read JSON consistently from SQLite and PostgreSQL reflected columns."""

    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


def _refs(payload: object) -> tuple[dict, dict]:
    """Return mutable output payload and its mutable artifact reference map."""

    result = _payload(payload)
    raw_refs = result.get("artifact_references")
    refs = dict(raw_refs) if isinstance(raw_refs, dict) else {}
    result["artifact_references"] = refs
    return result, refs


def _invalid(message: str, row_id: object) -> None:
    raise RuntimeError(f"0014 Commerce legacy integrity check failed: {message}: {row_id}")


def _workflow_step_status_mismatch(
    workflow_steps: sa.Table,
    commerce_steps: sa.Table,
) -> sa.ColumnElement[bool]:
    """Compare the current enum value with the legacy VARCHAR sidecar safely.

    ``commerce_workflow_steps.status`` was deliberately introduced as a
    VARCHAR column in 0012, whereas the pre-existing ``workflow_steps.status``
    column is PostgreSQL's native ``runstatus`` enum.  Casting the enum to text
    keeps the same value comparison and lets legacy invalid strings be reported
    by the migration's integrity guard instead of failing PostgreSQL's enum
    input conversion before that guard can run.
    """

    return sa.cast(workflow_steps.c.status, sa.Text()) != commerce_steps.c.status


def _validate_existing_sidecars(bind: sa.Connection, table: dict[str, sa.Table]) -> None:
    """Fail rather than silently bless legacy link/sidecar rows outside 0013 scope."""

    links = table["commerce_workflow_links"]
    commerce_steps = table["commerce_workflow_steps"]
    workflow_runs = table["workflow_runs"]
    workflow_steps = table["workflow_steps"]
    story_runs = table["story_runs"]

    invalid_link = bind.execute(
        sa.select(links.c.workflow_run_id)
        .select_from(
            links.outerjoin(workflow_runs, workflow_runs.c.id == links.c.workflow_run_id).outerjoin(
                story_runs, story_runs.c.id == links.c.story_run_id
            )
        )
        .where(
            sa.or_(
                workflow_runs.c.id.is_(None),
                story_runs.c.id.is_(None),
                workflow_runs.c.workflow_key != "commerce_story_run",
                workflow_runs.c.project_id != story_runs.c.project_id,
            )
        )
        .limit(1)
    ).scalar_one_or_none()
    if invalid_link is not None:
        _invalid("link workflow_key/project scope", invalid_link)

    invalid_step = bind.execute(
        sa.select(commerce_steps.c.workflow_step_id)
        .select_from(
            commerce_steps.outerjoin(
                links,
                sa.and_(
                    links.c.workflow_run_id == commerce_steps.c.workflow_run_id,
                    links.c.story_run_id == commerce_steps.c.story_run_id,
                ),
            )
            .outerjoin(workflow_runs, workflow_runs.c.id == commerce_steps.c.workflow_run_id)
            .outerjoin(story_runs, story_runs.c.id == commerce_steps.c.story_run_id)
            .outerjoin(workflow_steps, workflow_steps.c.id == commerce_steps.c.workflow_step_id)
        )
        .where(
            sa.or_(
                links.c.workflow_run_id.is_(None),
                workflow_runs.c.id.is_(None),
                story_runs.c.id.is_(None),
                workflow_steps.c.id.is_(None),
                workflow_runs.c.workflow_key != "commerce_story_run",
                workflow_runs.c.project_id != story_runs.c.project_id,
                workflow_steps.c.workflow_run_id != commerce_steps.c.workflow_run_id,
                workflow_steps.c.step_key != commerce_steps.c.stage,
                workflow_steps.c.attempt != commerce_steps.c.attempt,
                _workflow_step_status_mismatch(workflow_steps, commerce_steps),
            )
        )
        .limit(1)
    ).scalar_one_or_none()
    if invalid_step is not None:
        _invalid("sidecar parent/stage/attempt/status scope", invalid_step)

    missing_sidecar = bind.execute(
        sa.select(workflow_steps.c.id)
        .select_from(
            workflow_steps.join(links, links.c.workflow_run_id == workflow_steps.c.workflow_run_id).outerjoin(
                commerce_steps, commerce_steps.c.workflow_step_id == workflow_steps.c.id
            )
        )
        .where(commerce_steps.c.workflow_step_id.is_(None))
        .limit(1)
    ).scalar_one_or_none()
    if missing_sidecar is not None:
        _invalid("Commerce WorkflowRun has a WorkflowStep without sidecar", missing_sidecar)


def _backfill_chapter_attempt_membership(bind: sa.Connection, table: dict[str, sa.Table]) -> None:
    """Rebuild deterministic CHAPTERS result groups from published step payloads.

    ``0013`` deliberately did not know about existing 0012 output payloads.
    The payload's ordered ``chapter_ids`` is the immutable source of truth.
    Re-running after a 0013 downgrade is safe because the association table is
    recreated empty; re-running at head finds the exact rows and inserts none.
    """

    commerce_steps = table["commerce_workflow_steps"]
    workflow_steps = table["workflow_steps"]
    chapters = table["chapter_plans"]
    associations = table["commerce_chapter_attempt_chapters"]

    existing = {
        (row.workflow_step_id, row.chapter_plan_id): row
        for row in bind.execute(sa.select(associations)).all()
    }
    chapter_owners = {
        row.chapter_plan_id: row.workflow_step_id
        for row in bind.execute(sa.select(associations.c.workflow_step_id, associations.c.chapter_plan_id)).all()
    }
    steps = bind.execute(
        sa.select(
            commerce_steps.c.workflow_step_id,
            commerce_steps.c.story_run_id,
            workflow_steps.c.output_payload,
        )
        .select_from(commerce_steps.join(workflow_steps, workflow_steps.c.id == commerce_steps.c.workflow_step_id))
        .where(commerce_steps.c.stage == "CHAPTERS")
        .order_by(commerce_steps.c.workflow_step_id)
    ).all()
    for item in steps:
        _, refs = _refs(item.output_payload)
        chapter_ids = refs.get("chapter_ids")
        if chapter_ids is None:
            continue
        if (
            not isinstance(chapter_ids, list)
            or not chapter_ids
            or any(not isinstance(value, str) for value in chapter_ids)
            or len(set(chapter_ids)) != len(chapter_ids)
        ):
            _invalid("CHAPTERS output chapter_ids is not a non-empty unique list", item.workflow_step_id)

        rows = bind.execute(
            sa.select(chapters).where(chapters.c.id.in_(chapter_ids))
        ).all()
        by_id = {row.id: row for row in rows}
        if len(by_id) != len(chapter_ids):
            _invalid("CHAPTERS output references a missing chapter", item.workflow_step_id)
        outline_ids = {by_id[chapter_id].outline_version_id for chapter_id in chapter_ids}
        output_outline_id = refs.get("outline_id")
        if (
            any(by_id[chapter_id].story_run_id != item.story_run_id for chapter_id in chapter_ids)
            or len(outline_ids) != 1
            or (isinstance(output_outline_id, str) and output_outline_id not in outline_ids)
        ):
            _invalid("CHAPTERS output chapter ownership/outline is ambiguous", item.workflow_step_id)
        outline_id = next(iter(outline_ids))

        for position, chapter_id in enumerate(chapter_ids, start=1):
            prior_owner = chapter_owners.get(chapter_id)
            if prior_owner is not None and prior_owner != item.workflow_step_id:
                _invalid("chapter already belongs to a different attempt", chapter_id)
            current = existing.get((item.workflow_step_id, chapter_id))
            if current is not None:
                if (
                    current.story_run_id != item.story_run_id
                    or current.outline_version_id != outline_id
                    or current.position != position
                ):
                    _invalid("existing chapter attempt association conflicts with payload", chapter_id)
                continue
            bind.execute(
                associations.insert().values(
                    id=str(uuid4()),
                    workflow_step_id=item.workflow_step_id,
                    story_run_id=item.story_run_id,
                    outline_version_id=outline_id,
                    chapter_plan_id=chapter_id,
                    position=position,
                    created_at=_utcnow(),
                )
            )
            chapter_owners[chapter_id] = item.workflow_step_id


def _backfill_storyboard_references(bind: sa.Connection, table: dict[str, sa.Table]) -> None:
    """Add only deterministic legacy metadata to Storyboard output payloads."""

    commerce_steps = table["commerce_workflow_steps"]
    workflow_steps = table["workflow_steps"]
    segments = table["video_segment_plans"]
    sub_shots = table["sub_shot_plans"]
    placements = table["product_placement_plans"]
    story_runs = table["story_runs"]

    board_steps = bind.execute(
        sa.select(
            workflow_steps.c.id,
            workflow_steps.c.output_payload,
            commerce_steps.c.story_run_id,
            story_runs.c.product_asset_version_id,
        )
        .select_from(
            commerce_steps.join(workflow_steps, workflow_steps.c.id == commerce_steps.c.workflow_step_id).join(
                story_runs, story_runs.c.id == commerce_steps.c.story_run_id
            )
        )
        .where(commerce_steps.c.stage == "STORYBOARD")
    ).all()
    for board in board_steps:
        payload, refs = _refs(board.output_payload)
        changed = False
        segment_ids = refs.get("video_segment_ids")
        if not isinstance(segment_ids, list) or not segment_ids or any(not isinstance(value, str) for value in segment_ids):
            continue
        segment_rows = bind.execute(
            sa.select(segments).where(segments.c.id.in_(segment_ids))
        ).all()
        by_segment_id = {row.id: row for row in segment_rows}
        if (
            len(by_segment_id) != len(segment_ids)
            or any(by_segment_id[item_id].story_run_id != board.story_run_id for item_id in segment_ids)
        ):
            _invalid("Storyboard output references a missing/foreign segment", board.id)
        if "chapter_ids" not in refs:
            chapter_ids: list[str] = []
            for segment_id in segment_ids:
                chapter_id = by_segment_id[segment_id].chapter_id
                if chapter_id not in chapter_ids:
                    chapter_ids.append(chapter_id)
            refs["chapter_ids"] = chapter_ids
            changed = True

        # A chapter-level placement cannot be assigned to one historical attempt
        # deterministically.  Only placements attached to explicitly referenced
        # segments/sub-shots are safe to derive for legacy output.
        if "product_placement_ids" not in refs:
            sub_shot_ids = [
                row.id
                for row in bind.execute(
                    sa.select(sub_shots.c.id).where(sub_shots.c.video_segment_id.in_(segment_ids))
                ).all()
            ]
            condition = placements.c.video_segment_id.in_(segment_ids)
            if sub_shot_ids:
                condition = sa.or_(condition, placements.c.sub_shot_id.in_(sub_shot_ids))
            derived = [
                row.id
                for row in bind.execute(
                    sa.select(placements.c.id)
                    .where(
                        placements.c.story_run_id == board.story_run_id,
                        placements.c.product_asset_version_id == board.product_asset_version_id,
                        condition,
                    )
                    .order_by(placements.c.created_at, placements.c.id)
                ).all()
            ]
            if derived:
                refs["product_placement_ids"] = derived
                changed = True
        if changed:
            bind.execute(
                workflow_steps.update()
                .where(workflow_steps.c.id == board.id)
                .values(output_payload=payload)
            )


def _normalise_video_prompt_lifecycle(bind: sa.Connection, table: dict[str, sa.Table]) -> None:
    """Resolve legacy STEPWISE prompt states from their append-only reviews."""

    prompts = table["video_prompt_versions"]
    commerce_steps = table["commerce_workflow_steps"]
    workflow_steps = table["workflow_steps"]
    story_runs = table["story_runs"]
    reviews = table["review_decisions"]

    decision_by_step: dict[str, str] = {}
    decision_rows = bind.execute(
        sa.select(reviews.c.target_id, reviews.c.decision)
        .where(reviews.c.target_type == "COMMERCE_STAGE_VIDEO_PROMPTS")
        .order_by(reviews.c.created_at, reviews.c.id)
    ).all()
    for review in decision_rows:
        decision_by_step[review.target_id] = review.decision

    prompt_rows = bind.execute(
        sa.select(
            prompts.c.id,
            prompts.c.status,
            prompts.c.locked_at,
            prompts.c.workflow_step_id,
            story_runs.c.mode,
            workflow_steps.c.status.label("workflow_step_status"),
        )
        .select_from(
            prompts.join(workflow_steps, workflow_steps.c.id == prompts.c.workflow_step_id)
            .join(commerce_steps, commerce_steps.c.workflow_step_id == workflow_steps.c.id)
            .join(story_runs, story_runs.c.id == commerce_steps.c.story_run_id)
        )
        .where(commerce_steps.c.stage == "VIDEO_PROMPTS")
    ).all()
    for prompt in prompt_rows:
        if prompt.mode == "AUTO":
            # AUTO has no fake review decision.  A successfully adopted LOCKED
            # prompt remains locked exactly as published; do not manufacture a
            # state change for failed or pending historical attempts.
            continue
        decision = decision_by_step.get(prompt.workflow_step_id)
        if decision == "APPROVED":
            target_status, locked_at = "LOCKED", prompt.locked_at or _utcnow()
        elif decision == "REJECTED":
            target_status, locked_at = "REJECTED", None
        else:
            target_status, locked_at = "DRAFT", None
        if prompt.status != target_status or prompt.locked_at != locked_at:
            bind.execute(
                prompts.update()
                .where(prompts.c.id == prompt.id)
                .values(status=target_status, locked_at=locked_at)
            )


def upgrade() -> None:
    # Alembic's offline ``--sql`` mode exposes a MockConnection and cannot
    # inspect rows.  The online migration performs all deterministic data work;
    # offline output records an explicit no-op marker while retaining a fully
    # compilable 0013 -> 0014 PostgreSQL chain.
    if context.is_offline_mode():
        op.execute("SELECT 1 /* 0014 Commerce legacy data backfill runs online */")
        return
    bind = op.get_bind()
    metadata = sa.MetaData()
    metadata.reflect(bind=bind)
    table = metadata.tables
    _validate_existing_sidecars(bind, table)
    _backfill_chapter_attempt_membership(bind, table)
    _backfill_storyboard_references(bind, table)
    _normalise_video_prompt_lifecycle(bind, table)
    # Re-check after data work to make an interrupted/manual legacy database
    # fail deterministically rather than carrying inconsistent sidecars forward.
    _validate_existing_sidecars(bind, table)


def downgrade() -> None:
    """0014 is data-only; leave the exact 0013 schema, triggers, and audit data intact."""

    # Prompt state and explicit reference metadata are irreversible audit facts:
    # reconstructing their pre-0014 ambiguity would be less safe than retaining
    # the corrected data while returning the database structure to revision 0013.
    pass
