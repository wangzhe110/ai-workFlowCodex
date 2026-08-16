"""Commerce Phase 2：状态机、幂等、审核及本地 Mock 工作流。"""

import ast
import importlib.util
import inspect
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier, Event, Thread
from time import sleep
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from sqlalchemy import Column, Enum, MetaData, String, Table, create_engine, event, func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified
from alembic import command
from alembic.config import Config

from app.core.database import Base, SessionLocal, engine
from app.models import (
    ProductAnalysisStatus,
    ProductAnalysisVersion,
    ProductAsset,
    ProductAssetVersion,
    ProductAssetVersionStatus,
    CommerceWorkflowLink,
    CommerceWorkflowStep,
    CommerceChapterAttemptChapter,
    DialogueLine,
    ModelInvocation,
    Project,
    ProjectProductSelection,
    RunStatus,
    ReviewDecision,
    ProductPlacementPlan,
    ProductPlacementMethod,
    ProductPlacementStrength,
    RenderBatchStatus,
    SegmentPlanStatus,
    StoryRunMode,
    StoryRun,
    StoryRunStage,
    StoryRunStatus,
    TopicCandidate,
    VideoSegmentPlan,
    VideoPromptVersion,
    WorkflowRun,
    WorkflowStep,
)
from app.services.commerce_domain_service import (
    add_sub_shot_plan,
    create_dialogue_line,
    create_product_placement_plan,
    create_project_product_selection,
    create_video_segment_plan,
    freeze_product_asset_version,
)
from app.services.commerce_workflow_service import (
    CommerceNodeRegistry,
    CommerceNodeResult,
    CommerceNodeContext,
    cancel_story_run,
    continue_story_run,
    create_next_story_run,
    execute_commerce_workflow,
    review_stage,
    retry_step,
    start_story_run,
    workflow_for_story_run,
)
from app.services.v1_configuration_service import ensure_v1_foundation
from app.main import app
from app.services import worker_runtime
from app.services.worker_runtime import _WORKFLOW_EXECUTORS


@pytest.fixture()
def commerce_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        ensure_v1_foundation(db)
        yield db
    finally:
        db.rollback()
        db.close()


def _context(db, *, mode: StoryRunMode = StoryRunMode.STEPWISE):
    suffix = uuid4().hex[:8]
    project = Project(title=f"Commerce Phase2 {suffix}")
    db.add(project)
    db.flush()
    seed_run = WorkflowRun(project_id=project.id, workflow_key=f"seed-{suffix}", status=RunStatus.SUCCEEDED)
    db.add(seed_run)
    db.flush()
    topic = TopicCandidate(
        project_id=project.id,
        generation_run_id=seed_run.id,
        position=1,
        title="可验证的原创带货选题",
        opening_hook="三秒内展示人物痛点",
        synopsis="用于 Commerce 编排 Mock 闭环。",
    )
    product = ProductAsset(name=f"产品 {suffix}")
    db.add_all((topic, product))
    db.flush()
    analysis = ProductAnalysisVersion(
        product_asset_id=product.id, version=1, raw_analysis={}, analysis_status=ProductAnalysisStatus.SUCCEEDED
    )
    db.add(analysis)
    db.flush()
    product_version = ProductAssetVersion(
        product_asset_id=product.id, source_analysis_version_id=analysis.id, version=1,
        product_name="Mock 产品", appearance_description="白色包装", selling_points=[], user_pain_points=[],
        usage_scenarios=[], package_ocr={}, reference_images=[], status=ProductAssetVersionStatus.CONFIRMED,
    )
    db.add(product_version)
    db.flush()
    freeze_product_asset_version(db, product_asset_version_id=product_version.id)
    selection = create_project_product_selection(
        db, project_id=project.id, product_asset_id=product.id, product_asset_version_id=product_version.id
    )
    db.commit()
    return project, topic, selection, mode


def _execute(run):
    execute_commerce_workflow(run.id)


def _confirm(db, story_run, stage):
    return review_stage(
        db, story_run_id=story_run.id, stage=stage, decision="CONFIRMED", reviewer_label="测试审核", note=None,
        quality_score=8,
    )


def test_stepwise_mock_closure_preserves_attempts_reviews_and_product_binding(commerce_db):
    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(
        commerce_db, project_id=project.id, topic_candidate_id=topic.id,
        project_product_selection_id=selection.id, mode=mode,
    )
    assert story_run.state.current_stage == StoryRunStage.TOPIC
    assert story_run.state.status == StoryRunStatus.PENDING

    story_run, outline_run, created = start_story_run(commerce_db, story_run.id)
    assert created
    same_story_run, same_run, duplicate = start_story_run(commerce_db, story_run.id)
    assert same_story_run.id == story_run.id and same_run.id == outline_run.id and not duplicate
    _execute(outline_run)
    commerce_db.refresh(story_run)
    assert story_run.state.current_stage == StoryRunStage.OUTLINE
    assert story_run.state.status == StoryRunStatus.PAUSED

    _confirm(commerce_db, story_run, StoryRunStage.OUTLINE)
    story_run, chapters_run, _ = continue_story_run(commerce_db, story_run.id)
    _execute(chapters_run)
    _confirm(commerce_db, story_run, StoryRunStage.CHAPTERS)
    story_run, storyboard_run, _ = continue_story_run(commerce_db, story_run.id)
    _execute(storyboard_run)
    _confirm(commerce_db, story_run, StoryRunStage.STORYBOARD)
    story_run, visual_run, _ = continue_story_run(commerce_db, story_run.id)
    _execute(visual_run)
    _confirm(commerce_db, story_run, StoryRunStage.VISUAL_ASSETS)
    story_run, prompts_run, _ = continue_story_run(commerce_db, story_run.id)
    _execute(prompts_run)
    _confirm(commerce_db, story_run, StoryRunStage.VIDEO_PROMPTS)
    story_run, render_run, _ = continue_story_run(commerce_db, story_run.id)
    _execute(render_run)
    finished, should_dispatch = _confirm(commerce_db, story_run, StoryRunStage.SEGMENT_RENDER)

    assert not should_dispatch
    assert finished.state.current_stage == StoryRunStage.COMPLETED
    assert finished.state.status == StoryRunStatus.COMPLETED
    with pytest.raises(HTTPException) as completed_terminal:
        continue_story_run(commerce_db, finished.id)
    assert completed_terminal.value.status_code == 409
    commerce_db.expire_all()
    runs = workflow_for_story_run(commerce_db, story_run.id)
    assert all(item.commerce_link and item.commerce_link.story_run_id == story_run.id for item in runs)
    assert len(runs) == 1
    assert [item.step_key for item in runs[0].steps] == [
        "TOPIC", "OUTLINE", "CHAPTERS", "STORYBOARD", "VISUAL_ASSETS", "VIDEO_PROMPTS", "SEGMENT_RENDER"
    ]
    assert all(item.attempt == 1 for item in runs[0].steps)
    assert all(item.status == RunStatus.SUCCEEDED for item in runs[0].steps), [
        (item.step_key, item.status.value, item.error_message) for item in runs[0].steps
    ]


def test_auto_chains_non_review_stage_but_stops_at_storyboard_gate(commerce_db):
    project, topic, selection, _ = _context(commerce_db, mode=StoryRunMode.AUTO)
    story_run = create_next_story_run(commerce_db, project_id=project.id, topic_candidate_id=topic.id, project_product_selection_id=selection.id, mode=StoryRunMode.AUTO)
    story_run, outline_run, _ = start_story_run(commerce_db, story_run.id)
    _execute(outline_run)
    story_run, should_dispatch = _confirm(commerce_db, story_run, StoryRunStage.OUTLINE)
    assert should_dispatch
    story_run, chapters_run, _ = continue_story_run(commerce_db, story_run.id)
    _execute(chapters_run)
    commerce_db.expire_all()
    story_run = commerce_db.get(StoryRun, story_run.id)
    assert story_run.state.current_stage == StoryRunStage.STORYBOARD
    assert story_run.state.status == StoryRunStatus.PAUSED, [
        (item.workflow_key, item.status.value, item.steps[0].status.value, item.steps[0].error_message)
        for item in workflow_for_story_run(commerce_db, story_run.id)
    ]
    assert story_run.state.stage_data["blocked_reason"] == "awaiting_review"


def test_rejected_outline_creates_new_attempt_and_preserves_old_result(commerce_db):
    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(commerce_db, project_id=project.id, topic_candidate_id=topic.id, project_product_selection_id=selection.id, mode=mode)
    _, first_run, _ = start_story_run(commerce_db, story_run.id)
    _execute(first_run)
    review_stage(commerce_db, story_run_id=story_run.id, stage=StoryRunStage.OUTLINE, decision="REJECTED", reviewer_label="测试审核", note="重做", quality_score=3)
    story_run, second_run, created = continue_story_run(commerce_db, story_run.id)
    assert created and second_run.id == first_run.id
    attempts = [item for item in second_run.steps if item.step_key == "OUTLINE"]
    assert attempts[-1].attempt == 2
    assert attempts[0].output_payload["artifact_references"]["outline_id"] != attempts[-1].id
    _execute(second_run)
    old_outline_id = attempts[0].output_payload["artifact_references"]["outline_id"]
    from app.models import StoryOutlineVersion
    assert commerce_db.get(StoryOutlineVersion, old_outline_id).status.value == "SUPERSEDED"


def test_failure_retry_only_current_failed_step_and_nonmock_never_succeeds(commerce_db, monkeypatch):
    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(commerce_db, project_id=project.id, topic_candidate_id=topic.id, project_product_selection_id=selection.id, mode=mode)
    _, run, _ = start_story_run(commerce_db, story_run.id)
    outline_step = next(step for step in run.steps if step.step_key == "OUTLINE")
    outline_step.model_profile_snapshot["model_bindings"]["STORY_GENERATE"][0]["profile_snapshot"]["adapter_key"] = "unsupported_adapter"
    flag_modified(outline_step, "model_profile_snapshot")
    commerce_db.commit()
    _execute(run)
    commerce_db.expire_all()
    story_run = commerce_db.get(StoryRun, story_run.id)
    run = commerce_db.get(WorkflowRun, run.id)
    assert story_run.state.status == StoryRunStatus.FAILED
    assert outline_step.status == RunStatus.FAILED
    story_run, retried, created = retry_step(commerce_db, story_run.id, outline_step.id)
    retried_outline = [item for item in retried.steps if item.step_key == "OUTLINE"][-1]
    assert created and retried_outline.attempt == 2
    assert retried.id == run.id
    assert retried.status == RunStatus.PENDING
    assert commerce_db.get(StoryRun, story_run.id).state.status == StoryRunStatus.RUNNING
    assert outline_step.error_message and "供应商" not in outline_step.error_message


def test_cancel_is_terminal_and_other_run_is_independent(commerce_db):
    project, topic, selection, mode = _context(commerce_db)
    one = create_next_story_run(commerce_db, project_id=project.id, topic_candidate_id=topic.id, project_product_selection_id=selection.id, mode=mode)
    two = create_next_story_run(commerce_db, project_id=project.id, topic_candidate_id=topic.id, project_product_selection_id=selection.id, mode=mode)
    assert (one.run_number, two.run_number) == (1, 2)
    cancel_story_run(commerce_db, one.id)
    assert one.state.status == StoryRunStatus.CANCELLED
    assert two.state.status == StoryRunStatus.PENDING
    with pytest.raises(HTTPException) as exc:
        start_story_run(commerce_db, one.id)
    assert exc.value.status_code == 409


def test_failure_in_one_story_run_does_not_change_another_story_run(commerce_db):
    """规定场景 #9：失败必须仅停留在本 StoryRun，不能污染同项目的另一运行。"""

    project, topic, selection, mode = _context(commerce_db)
    failed_story = create_next_story_run(
        commerce_db, project_id=project.id, topic_candidate_id=topic.id,
        project_product_selection_id=selection.id, mode=mode,
    )
    healthy_story = create_next_story_run(
        commerce_db, project_id=project.id, topic_candidate_id=topic.id,
        project_product_selection_id=selection.id, mode=mode,
    )
    _, failed_parent, _ = start_story_run(commerce_db, failed_story.id)
    _, healthy_parent, _ = start_story_run(commerce_db, healthy_story.id)
    failed_step = _stage_steps(failed_parent, StoryRunStage.OUTLINE)[0]
    failed_step.model_profile_snapshot["model_bindings"]["STORY_GENERATE"][0]["profile_snapshot"]["adapter_key"] = "unconfigured_adapter"
    flag_modified(failed_step, "model_profile_snapshot")
    commerce_db.commit()

    _execute(failed_parent)
    commerce_db.expire_all()
    assert commerce_db.get(StoryRun, failed_story.id).state.status == StoryRunStatus.FAILED
    assert commerce_db.get(WorkflowRun, failed_parent.id).status == RunStatus.FAILED
    assert commerce_db.get(StoryRun, healthy_story.id).state.status == StoryRunStatus.RUNNING
    assert commerce_db.get(WorkflowRun, healthy_parent.id).status == RunStatus.PENDING

    _execute(healthy_parent)
    commerce_db.expire_all()
    assert commerce_db.get(StoryRun, healthy_story.id).state.status == StoryRunStatus.PAUSED
    assert commerce_db.get(WorkflowRun, healthy_parent.id).status == RunStatus.PENDING


def test_commerce_api_contract_and_worker_registry(commerce_db):
    """API 仅通过服务推进状态，inline BackgroundTask 使用确定性 Mock 完成 OUTLINE。"""

    project, topic, selection, _ = _context(commerce_db)
    assert "commerce_story_run" in _WORKFLOW_EXECUTORS
    with TestClient(app) as client:
        definition = client.get("/api/v1/commerce/workflow-definition")
        assert definition.status_code == 200
        assert definition.json()["workflow_code"] == "LEMONFLOW_COMMERCE"
        bad = client.post(f"/api/v1/commerce/projects/{project.id}/story-runs", json={
            "topic_candidate_id": topic.id, "project_product_selection_id": "missing", "mode": "STEPWISE"
        })
        assert bad.status_code == 404
        invalid = client.post(f"/api/v1/commerce/projects/{project.id}/story-runs", json={
            "topic_candidate_id": topic.id, "project_product_selection_id": selection.id, "mode": "NOT_A_MODE"
        })
        assert invalid.status_code == 422
        created = client.post(f"/api/v1/commerce/projects/{project.id}/story-runs", json={
            "topic_candidate_id": topic.id, "project_product_selection_id": selection.id, "mode": "STEPWISE"
        })
        assert created.status_code == 201, created.text
        story_run_id = created.json()["id"]
        started = client.post(f"/api/v1/commerce/story-runs/{story_run_id}/start")
        assert started.status_code == 202, started.text
        assert started.json()["current_stage"] == "OUTLINE"
        # 202 反映已投递的瞬间；TestClient 执行完 BackgroundTask 后必须通过详情读取
        # 持久化终态，不能把提交响应误当作 Worker 结果。
        current = client.get(f"/api/v1/commerce/story-runs/{story_run_id}")
        assert current.status_code == 200 and current.json()["current_status"] == "PAUSED"
        conflict = client.post(f"/api/v1/commerce/story-runs/{story_run_id}/stages/CHAPTERS/confirm", json={})
        assert conflict.status_code == 409
        confirmed = client.post(f"/api/v1/commerce/story-runs/{story_run_id}/stages/OUTLINE/confirm", json={"quality_score": 9})
        assert confirmed.status_code == 202, confirmed.text
        outlines = client.get(f"/api/v1/commerce/story-runs/{story_run_id}/outlines")
        assert outlines.status_code == 200 and len(outlines.json()) == 1
        locked_patch = client.patch(
            f"/api/v1/commerce/story-runs/{story_run_id}/outlines/{outlines.json()[0]['id']}",
            json={"title": "不允许原地覆盖"},
        )
        assert locked_patch.status_code == 409
        workflow = client.get(f"/api/v1/commerce/story-runs/{story_run_id}/workflow")
        reviews = client.get(f"/api/v1/commerce/story-runs/{story_run_id}/reviews")
        assert workflow.status_code == 200 and workflow.json()[0]["steps"][1]["attempt"] == 1
        assert reviews.status_code == 200 and len(reviews.json()) >= 2
        assert client.get("/api/v1/commerce/story-runs/not-found").status_code == 404


def _stage_steps(run: WorkflowRun, stage: StoryRunStage) -> list[WorkflowStep]:
    return [step for step in run.steps if step.step_key == stage.value]


def _run_to_storyboard_pause(db, story_run: StoryRun) -> WorkflowRun:
    _, run, _ = start_story_run(db, story_run.id)
    _execute(run)
    _confirm(db, story_run, StoryRunStage.OUTLINE)
    _, run, _ = continue_story_run(db, story_run.id)
    _execute(run)
    _confirm(db, story_run, StoryRunStage.CHAPTERS)
    _, run, _ = continue_story_run(db, story_run.id)
    _execute(run)
    return run


def test_single_parent_run_attempt_timeline_and_complete_task_snapshot(commerce_db):
    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(
        commerce_db, project_id=project.id, topic_candidate_id=topic.id,
        project_product_selection_id=selection.id, mode=mode,
    )
    _, parent, created = start_story_run(commerce_db, story_run.id)
    assert created and parent.workflow_key == "commerce_story_run"
    assert len(workflow_for_story_run(commerce_db, story_run.id)) == 1
    outline = _stage_steps(parent, StoryRunStage.OUTLINE)[0]
    snapshot = outline.input_payload
    assert snapshot["workflow_definition"]["workflow_code"] == "LEMONFLOW_COMMERCE"
    assert snapshot["commerce"]["project_product_selection_snapshot"]["id"] == selection.id
    assert snapshot["commerce"]["product_asset_version_snapshot"]["id"] == selection.product_asset_version_id
    binding = snapshot["model_bindings"]["STORY_GENERATE"][0]
    assert binding["slot_snapshot"]["slot_key"] == "STORY_GENERATE"
    assert binding["profile_snapshot"]["profile_id"] == binding["model_profile_id"]
    assert binding["adapter_snapshot"]["key"] == binding["profile_snapshot"]["adapter_key"]
    assert snapshot["prompt_templates"]["STORY_GENERATE"]["content"]

    # 任务已创建后，中心对象的配置变化不能回写任务快照。
    profile = commerce_db.get(ProductAssetVersion, selection.product_asset_version_id)
    profile.product_name = "后续修改不能影响快照"
    commerce_db.commit()
    assert outline.input_payload["commerce"]["product_asset_version_snapshot"]["product_name"] == "Mock 产品"


def test_pause_resume_and_cross_story_review_rejection_preserve_state(commerce_db):
    project, topic, selection, mode = _context(commerce_db)
    one = create_next_story_run(commerce_db, project_id=project.id, topic_candidate_id=topic.id, project_product_selection_id=selection.id, mode=mode)
    two = create_next_story_run(commerce_db, project_id=project.id, topic_candidate_id=topic.id, project_product_selection_id=selection.id, mode=mode)
    _, run, _ = start_story_run(commerce_db, one.id)
    with pytest.raises(HTTPException) as active_pause:
        from app.services.commerce_workflow_service import pause_story_run
        pause_story_run(commerce_db, one.id)
    assert active_pause.value.status_code == 409
    commerce_db.rollback()
    _execute(run)
    _, two_parent, _ = start_story_run(commerce_db, two.id)
    _execute(two_parent)
    foreign_outline_id = _stage_steps(two_parent, StoryRunStage.OUTLINE)[0].output_payload["artifact_references"]["outline_id"]
    with pytest.raises(HTTPException) as foreign:
        review_stage(
            commerce_db, story_run_id=one.id, stage=StoryRunStage.OUTLINE, decision="CONFIRMED",
            reviewer_label="测试", note=None, quality_score=8, outline_id=foreign_outline_id,
        )
    assert foreign.value.status_code == 422
    assert one.state.current_stage == StoryRunStage.OUTLINE and one.state.status == StoryRunStatus.PAUSED
    _confirm(commerce_db, one, StoryRunStage.OUTLINE)
    from app.services.commerce_workflow_service import pause_story_run, resume_story_run
    paused = pause_story_run(commerce_db, one.id)
    assert paused.state.status == StoryRunStatus.PAUSED and paused.state.stage_data["blocked_reason"] == "manual_pause"
    resumed = resume_story_run(commerce_db, one.id)
    assert resumed.state.status == StoryRunStatus.PENDING and resumed.state.current_stage == StoryRunStage.CHAPTERS


def test_storyboard_and_render_review_validation_preserve_unapproved_result(commerce_db):
    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(commerce_db, project_id=project.id, topic_candidate_id=topic.id, project_product_selection_id=selection.id, mode=mode)
    run = _run_to_storyboard_pause(commerce_db, story_run)
    board_step = _stage_steps(run, StoryRunStage.STORYBOARD)[0]
    original = dict(board_step.output_payload)
    board_step.output_payload = {**original, "artifact_references": {"scene_mapping_id": "missing", "video_segment_ids": []}}
    flag_modified(board_step, "output_payload")
    commerce_db.commit()
    with pytest.raises(HTTPException) as invalid_board:
        _confirm(commerce_db, story_run, StoryRunStage.STORYBOARD)
    assert invalid_board.value.status_code == 422
    commerce_db.refresh(board_step)
    assert board_step.output_payload["artifact_references"]["scene_mapping_id"] == "missing"


def test_storyboard_dialogue_validation_accepts_owned_lines_and_rejects_foreign_fragment_or_time(commerce_db):
    """#25：分镜审核必须显式校验对白的 StoryRun、片段及子镜头时间归属。"""

    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(
        commerce_db, project_id=project.id, topic_candidate_id=topic.id,
        project_product_selection_id=selection.id, mode=mode,
    )
    parent = _run_to_storyboard_pause(commerce_db, story_run)
    board_step = _stage_steps(parent, StoryRunStage.STORYBOARD)[0]
    refs = board_step.output_payload["artifact_references"]
    owned_line_id = refs["dialogue_line_ids"][0]
    owned_line = commerce_db.get(DialogueLine, owned_line_id)
    assert owned_line is not None and owned_line.sub_shot is not None
    # 正常路径：Mock 产生的对白属于当前分镜的片段和子镜头，确认必须通过。
    _confirm(commerce_db, story_run, StoryRunStage.STORYBOARD)
    assert story_run.state.current_stage == StoryRunStage.VISUAL_ASSETS

    # 同一 StoryRun 中、但未列入本分镜的另一个片段的对白也不能被偷换进来。
    invalid_run = create_next_story_run(
        commerce_db, project_id=project.id, topic_candidate_id=topic.id,
        project_product_selection_id=selection.id, mode=mode,
    )
    invalid_parent = _run_to_storyboard_pause(commerce_db, invalid_run)
    invalid_step = _stage_steps(invalid_parent, StoryRunStage.STORYBOARD)[0]
    invalid_refs = invalid_step.output_payload["artifact_references"]
    current_segment = commerce_db.get(VideoSegmentPlan, invalid_refs["video_segment_ids"][0])
    assert current_segment is not None
    sibling = create_video_segment_plan(
        commerce_db,
        story_run_id=invalid_run.id,
        chapter_id=current_segment.chapter_id,
        segment_number=current_segment.segment_number + 1,
        target_duration_ms=4000,
        narrative_target="不属于当前分镜输出的同章片段",
    )
    sibling_shot = add_sub_shot_plan(
        commerce_db, sibling, shot_number=1, start_ms=0, end_ms=4000,
        action="另一段动作", emotion="平静", shot_scale="中景", camera_move="固定",
        lighting="柔光", visual_description="另一段画面",
    )
    sibling_line = create_dialogue_line(
        commerce_db, video_segment_id=None, sub_shot_id=sibling_shot.id,
        speaker="配角", dialogue="这句对白属于另一个片段。", start_ms=100, end_ms=900,
    )
    invalid_step.output_payload = {
        **invalid_step.output_payload,
        "artifact_references": {**invalid_refs, "dialogue_line_ids": [sibling_line.id]},
    }
    flag_modified(invalid_step, "output_payload")
    commerce_db.commit()
    reviews_before = commerce_db.scalar(
        select(func.count()).select_from(ReviewDecision).where(
            ReviewDecision.target_type == "COMMERCE_STAGE_STORYBOARD",
            ReviewDecision.target_id == invalid_step.id,
        )
    )
    with pytest.raises(HTTPException) as wrong_fragment:
        _confirm(commerce_db, invalid_run, StoryRunStage.STORYBOARD)
    assert wrong_fragment.value.status_code == 422
    assert invalid_run.state.status == StoryRunStatus.PAUSED
    assert invalid_step.output_payload["artifact_references"]["dialogue_line_ids"] == [sibling_line.id]
    assert commerce_db.scalar(
        select(func.count()).select_from(ReviewDecision).where(
            ReviewDecision.target_type == "COMMERCE_STAGE_STORYBOARD",
            ReviewDecision.target_id == invalid_step.id,
        )
    ) == reviews_before

    # 即使对白仍属于当前子镜头，只要超出该子镜头时间范围也必须拒绝，且不写审核事实。
    own_line_id = invalid_refs["dialogue_line_ids"][0]
    own_line = commerce_db.get(DialogueLine, own_line_id)
    assert own_line is not None and own_line.sub_shot is not None
    own_line.end_ms = own_line.sub_shot.end_ms + 1
    invalid_step.output_payload = {
        **invalid_step.output_payload,
        "artifact_references": {**invalid_refs, "dialogue_line_ids": [own_line.id]},
    }
    flag_modified(invalid_step, "output_payload")
    commerce_db.commit()
    with pytest.raises(HTTPException) as out_of_range:
        _confirm(commerce_db, invalid_run, StoryRunStage.STORYBOARD)
    assert out_of_range.value.status_code == 422
    assert invalid_run.state.status == StoryRunStatus.PAUSED
    assert commerce_db.scalar(
        select(func.count()).select_from(ReviewDecision).where(
            ReviewDecision.target_type == "COMMERCE_STAGE_STORYBOARD",
            ReviewDecision.target_id == invalid_step.id,
        )
    ) == reviews_before

    # 其他 StoryRun 的对白更不能作为本 StoryRun 的分镜结果。
    foreign_run = create_next_story_run(
        commerce_db, project_id=project.id, topic_candidate_id=topic.id,
        project_product_selection_id=selection.id, mode=mode,
    )
    foreign_parent = _run_to_storyboard_pause(commerce_db, foreign_run)
    foreign_step = _stage_steps(foreign_parent, StoryRunStage.STORYBOARD)[0]
    foreign_line_id = foreign_step.output_payload["artifact_references"]["dialogue_line_ids"][0]
    invalid_step.output_payload = {
        **invalid_step.output_payload,
        "artifact_references": {**invalid_refs, "dialogue_line_ids": [foreign_line_id]},
    }
    flag_modified(invalid_step, "output_payload")
    commerce_db.commit()
    with pytest.raises(HTTPException) as foreign_line:
        _confirm(commerce_db, invalid_run, StoryRunStage.STORYBOARD)
    assert foreign_line.value.status_code == 422
    assert invalid_run.state.status == StoryRunStatus.PAUSED
    assert commerce_db.scalar(
        select(func.count()).select_from(ReviewDecision).where(
            ReviewDecision.target_type == "COMMERCE_STAGE_STORYBOARD",
            ReviewDecision.target_id == invalid_step.id,
        )
    ) == reviews_before


def test_review_decisions_are_append_only_and_failed_reviews_create_no_audit_row(commerce_db):
    """#20：确认和驳回都是独立审核事实，冲突调用不得覆盖或新增记录。"""

    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(
        commerce_db, project_id=project.id, topic_candidate_id=topic.id,
        project_product_selection_id=selection.id, mode=mode,
    )
    _, parent, _ = start_story_run(commerce_db, story_run.id)
    _execute(parent)
    first_outline = _stage_steps(parent, StoryRunStage.OUTLINE)[0]
    initial_count = commerce_db.scalar(
        select(func.count()).select_from(ReviewDecision).where(
            ReviewDecision.project_id == project.id,
            ReviewDecision.target_type == "COMMERCE_STAGE_OUTLINE",
        )
    )
    review_stage(
        commerce_db, story_run_id=story_run.id, stage=StoryRunStage.OUTLINE,
        decision="REJECTED", reviewer_label="审片人 A", note="开头冲突不够强", quality_score=3,
    )
    rejected = commerce_db.scalars(
        select(ReviewDecision).where(
            ReviewDecision.target_type == "COMMERCE_STAGE_OUTLINE",
            ReviewDecision.target_id == first_outline.id,
        )
    ).one()
    assert (
        rejected.project_id,
        rejected.target_type,
        rejected.target_id,
        rejected.decision,
        rejected.reviewer_label,
        rejected.note,
        rejected.quality_score,
    ) == (
        project.id,
        "COMMERCE_STAGE_OUTLINE",
        first_outline.id,
        "REJECTED",
        "审片人 A",
        "开头冲突不够强",
        3,
    )
    with pytest.raises(HTTPException) as repeat_reject:
        review_stage(
            commerce_db, story_run_id=story_run.id, stage=StoryRunStage.OUTLINE,
            decision="REJECTED", reviewer_label="审片人 A", note="重复", quality_score=2,
        )
    assert repeat_reject.value.status_code == 409
    assert commerce_db.scalar(
        select(func.count()).select_from(ReviewDecision).where(
            ReviewDecision.project_id == project.id,
            ReviewDecision.target_type == "COMMERCE_STAGE_OUTLINE",
        )
    ) == initial_count + 1

    _, parent, created = continue_story_run(commerce_db, story_run.id)
    assert created
    _execute(parent)
    second_outline = _stage_steps(parent, StoryRunStage.OUTLINE)[-1]
    review_stage(
        commerce_db, story_run_id=story_run.id, stage=StoryRunStage.OUTLINE,
        decision="CONFIRMED", reviewer_label="审片人 B", note="采用第二版", quality_score=9,
    )
    decisions = list(
        commerce_db.scalars(
            select(ReviewDecision)
            .where(
                ReviewDecision.project_id == project.id,
                ReviewDecision.target_type == "COMMERCE_STAGE_OUTLINE",
            )
            .order_by(ReviewDecision.created_at, ReviewDecision.id)
        ).all()
    )
    assert [(item.target_id, item.decision) for item in decisions[-2:]] == [
        (first_outline.id, "REJECTED"),
        (second_outline.id, "APPROVED"),
    ]
    accepted = decisions[-1]
    assert accepted.project_id == project.id
    assert accepted.reviewer_label == "审片人 B"
    assert accepted.note == "采用第二版"
    assert accepted.quality_score == 9
    count_after_confirm = len(decisions)
    with pytest.raises(HTTPException) as conflicting_review:
        review_stage(
            commerce_db, story_run_id=story_run.id, stage=StoryRunStage.OUTLINE,
            decision="REJECTED", reviewer_label="审片人 C", note="冲突审核", quality_score=1,
        )
    assert conflicting_review.value.status_code == 409
    assert commerce_db.scalar(
        select(func.count()).select_from(ReviewDecision).where(
            ReviewDecision.project_id == project.id,
            ReviewDecision.target_type == "COMMERCE_STAGE_OUTLINE",
        )
    ) == count_after_confirm


def test_render_batch_with_running_or_failed_tasks_cannot_complete_story_run(commerce_db):
    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(commerce_db, project_id=project.id, topic_candidate_id=topic.id, project_product_selection_id=selection.id, mode=mode)
    run = _run_to_storyboard_pause(commerce_db, story_run)
    _confirm(commerce_db, story_run, StoryRunStage.STORYBOARD)
    _, run, _ = continue_story_run(commerce_db, story_run.id)
    _execute(run)
    _confirm(commerce_db, story_run, StoryRunStage.VISUAL_ASSETS)
    _, run, _ = continue_story_run(commerce_db, story_run.id)
    _execute(run)
    _confirm(commerce_db, story_run, StoryRunStage.VIDEO_PROMPTS)
    _, run, _ = continue_story_run(commerce_db, story_run.id)
    _execute(run)
    render_step = _stage_steps(run, StoryRunStage.SEGMENT_RENDER)[0]
    batch_id = render_step.output_payload["artifact_references"]["render_batch_id"]
    from app.models import RenderBatch
    batch = commerce_db.get(RenderBatch, batch_id)
    batch.completed_tasks = 0
    batch.running_tasks = 1
    commerce_db.commit()
    with pytest.raises(HTTPException) as incomplete:
        _confirm(commerce_db, story_run, StoryRunStage.SEGMENT_RENDER)
    assert incomplete.value.status_code == 409
    assert story_run.state.current_stage == StoryRunStage.SEGMENT_RENDER


def test_duplicate_continue_and_attempt_execution_are_idempotent(commerce_db, monkeypatch):
    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(commerce_db, project_id=project.id, topic_candidate_id=topic.id, project_product_selection_id=selection.id, mode=mode)
    _, parent, _ = start_story_run(commerce_db, story_run.id)
    _, duplicate_parent, duplicate = start_story_run(commerce_db, story_run.id)
    assert duplicate_parent.id == parent.id and not duplicate

    calls: list[str] = []

    class CountingExecutor:
        def execute(self, context):
            calls.append(context.workflow_step.id)
            sleep(0.05)
            return CommerceNodeResult(
                artifact_references={"outline_id": create_next_story_outline_version(
                    context.db, story_run_id=context.story_run.id, title="并发大纲", premise="并发测试", story_beats=[], product_placement_strategy={}
                ).id},
                structured_output={}, usage={}, cost={},
            )

    from app.services.commerce_domain_service import create_next_story_outline_version
    monkeypatch.setattr(CommerceNodeRegistry, "_executor", CountingExecutor())
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: execute_commerce_workflow(parent.id), range(2)))
    commerce_db.expire_all()
    parent = commerce_db.get(WorkflowRun, parent.id)
    assert len(calls) == 1
    assert _stage_steps(parent, StoryRunStage.OUTLINE)[0].status == RunStatus.SUCCEEDED
    assert commerce_db.scalar(select(func.count()).select_from(WorkflowStep).where(WorkflowStep.workflow_run_id == parent.id, WorkflowStep.step_key == "OUTLINE")) == 1


def test_late_worker_cannot_overwrite_a_cancelled_attempt(tmp_path, monkeypatch):
    """用户取消先提交时，慢模型返回也不得复活父运行或节点。"""

    local_engine = create_engine(f"sqlite:///{tmp_path / 'commerce-cancel-race.db'}", connect_args={"check_same_thread": False})
    local_session = sessionmaker(bind=local_engine, autoflush=False, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(bind=local_engine)
    from app.services import commerce_workflow_service
    monkeypatch.setattr(commerce_workflow_service, "SessionLocal", local_session)
    setup_db = local_session()
    try:
        ensure_v1_foundation(setup_db)
        project, topic, selection, mode = _context(setup_db)
        story_run = create_next_story_run(
            setup_db, project_id=project.id, topic_candidate_id=topic.id,
            project_product_selection_id=selection.id, mode=mode,
        )
        _, parent, _ = start_story_run(setup_db, story_run.id)
        started, release = Event(), Event()

        class SlowExecutor:
            def execute(self, _context):
                started.set()
                assert release.wait(timeout=5)
                return CommerceNodeResult(artifact_references={}, structured_output={}, usage={}, cost={})

        monkeypatch.setattr(CommerceNodeRegistry, "_executor", SlowExecutor())
        worker = Thread(target=execute_commerce_workflow, args=(parent.id,))
        worker.start()
        assert started.wait(timeout=5)

        cancel_db = local_session()
        try:
            cancel_story_run(cancel_db, story_run.id)
        finally:
            cancel_db.close()
        release.set()
        worker.join(timeout=5)
        assert not worker.is_alive()

        verify_db = local_session()
        try:
            stored_run = verify_db.get(WorkflowRun, parent.id)
            stored_step = _stage_steps(stored_run, StoryRunStage.OUTLINE)[0]
            stored_story = verify_db.get(StoryRun, story_run.id)
            assert stored_run.status == RunStatus.CANCELLED
            assert stored_step.status == RunStatus.CANCELLED
            assert stored_story.state.status == StoryRunStatus.CANCELLED
        finally:
            verify_db.close()
    finally:
        setup_db.close()
        local_engine.dispose()


def test_concurrent_start_continue_and_retry_do_not_create_duplicate_attempts(tmp_path, monkeypatch):
    """真实线程并发请求；SQLite 短暂锁冲突会按客户端重试语义重新读取。"""

    # 使用独立的真实文件 SQLite 库，避免其它单元测试的读取事务掩盖并发语义。
    local_engine = create_engine(f"sqlite:///{tmp_path / 'commerce-concurrency.db'}", connect_args={"check_same_thread": False})
    local_session = sessionmaker(bind=local_engine, autoflush=False, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(bind=local_engine)
    from app.services import commerce_workflow_service
    monkeypatch.setattr(commerce_workflow_service, "SessionLocal", local_session)
    commerce_db = local_session()
    ensure_v1_foundation(commerce_db)
    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(commerce_db, project_id=project.id, topic_candidate_id=topic.id, project_product_selection_id=selection.id, mode=mode)

    def concurrent_call(action):
        barrier = Barrier(2)

        def invoke(_):
            db = local_session()
            try:
                barrier.wait(timeout=5)
                for _attempt in range(10):
                    try:
                        result = action(db)
                        db.commit()
                        return result[1].id, result[2]
                    except (OperationalError, HTTPException) as exc:
                        db.rollback()
                        if isinstance(exc, HTTPException) and exc.status_code != 409:
                            raise
                        sleep(0.02)
                raise AssertionError("SQLite 并发锁在重试后仍未释放")
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(invoke, range(2)))

    starts = concurrent_call(lambda db: start_story_run(db, story_run.id))
    assert len({item[0] for item in starts}) == 1 and sum(created for _, created in starts) == 1
    parent_id = starts[0][0]
    execute_commerce_workflow(parent_id)
    _confirm(commerce_db, story_run, StoryRunStage.OUTLINE)
    continues = concurrent_call(lambda db: continue_story_run(db, story_run.id))
    assert len({item[0] for item in continues}) == 1 and sum(created for _, created in continues) == 1

    parent = commerce_db.get(WorkflowRun, parent_id)
    chapter_step = _stage_steps(parent, StoryRunStage.CHAPTERS)[0]
    chapter_step.model_profile_snapshot["model_bindings"]["STORY_GENERATE"][0]["profile_snapshot"]["adapter_key"] = "unsupported"
    flag_modified(chapter_step, "model_profile_snapshot")
    commerce_db.commit()
    execute_commerce_workflow(parent_id)
    commerce_db.expire_all()
    parent = commerce_db.get(WorkflowRun, parent_id)
    failed = _stage_steps(parent, StoryRunStage.CHAPTERS)[0]
    retries = concurrent_call(lambda db: retry_step(db, story_run.id, failed.id))
    assert len({item[0] for item in retries}) == 1 and sum(created for _, created in retries) == 1
    commerce_db.expire_all()
    assert len(_stage_steps(commerce_db.get(WorkflowRun, parent_id), StoryRunStage.CHAPTERS)) == 2
    commerce_db.close()
    local_engine.dispose()


def test_fake_queue_submission_is_post_commit_deduplicated_and_recoverable(commerce_db, monkeypatch):
    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(commerce_db, project_id=project.id, topic_candidate_id=topic.id, project_product_selection_id=selection.id, mode=mode)
    observed: list[tuple[str, str]] = []

    def fake_enqueue(workflow_key: str, run_id: str) -> None:
        db = SessionLocal()
        try:
            run = db.get(WorkflowRun, run_id)
            assert run is not None and run.status == RunStatus.PENDING
            assert _stage_steps(run, StoryRunStage.OUTLINE)[0].status == RunStatus.PENDING
            observed.append((workflow_key, run_id))
        finally:
            db.close()

    monkeypatch.setattr(worker_runtime, "settings", replace(worker_runtime.settings, task_execution_mode="rq", redis_url="redis://fake"))
    monkeypatch.setattr(worker_runtime, "_enqueue_rq_job", fake_enqueue)
    _, parent, created = start_story_run(commerce_db, story_run.id)
    assert created
    worker_runtime.dispatch_workflow(BackgroundTasks(), parent.workflow_key, parent.id)
    assert observed == [("commerce_story_run", parent.id)]
    _, same_parent, duplicate = start_story_run(commerce_db, story_run.id)
    assert same_parent.id == parent.id and not duplicate and len(observed) == 1

    def failed_enqueue(*_args):
        raise RuntimeError("fake redis unavailable")

    monkeypatch.setattr(worker_runtime, "_enqueue_rq_job", failed_enqueue)
    with pytest.raises(RuntimeError):
        worker_runtime.dispatch_workflow(BackgroundTasks(), parent.workflow_key, parent.id)
    commerce_db.expire_all()
    failed_story = commerce_db.get(StoryRun, story_run.id)
    failed_parent = commerce_db.get(WorkflowRun, parent.id)
    assert failed_story.state.status == StoryRunStatus.FAILED
    assert failed_parent.status == RunStatus.FAILED
    failed_step = _stage_steps(failed_parent, StoryRunStage.OUTLINE)[0]
    assert failed_step.status == RunStatus.FAILED
    _, retried, created = retry_step(commerce_db, story_run.id, failed_step.id)
    assert created and _stage_steps(retried, StoryRunStage.OUTLINE)[-1].attempt == 2
    assert retried.status == RunStatus.PENDING
    assert commerce_db.get(StoryRun, story_run.id).state.status == StoryRunStatus.RUNNING


def test_commerce_api_exposes_recoverable_dispatch_failure_as_503(commerce_db, monkeypatch):
    """投递错误已持久化为 FAILED，HTTP 语义必须是可理解的服务暂不可用。"""

    project, topic, selection, _ = _context(commerce_db)
    from app.api.routes import commerce

    def failing_dispatch(_background_tasks, _workflow_key: str, run_id: str) -> None:
        worker_runtime._mark_dispatch_failure(run_id, "Fake Queue unavailable")
        raise RuntimeError("Fake Queue unavailable")

    monkeypatch.setattr(commerce, "dispatch_workflow", failing_dispatch)
    with TestClient(app) as client:
        created = client.post(f"/api/v1/commerce/projects/{project.id}/story-runs", json={
            "topic_candidate_id": topic.id,
            "project_product_selection_id": selection.id,
            "mode": "STEPWISE",
        })
        assert created.status_code == 201
        response = client.post(f"/api/v1/commerce/story-runs/{created.json()['id']}/start")
    assert response.status_code == 503
    assert "任务暂时无法投递" in response.json()["detail"]
    commerce_db.expire_all()
    stored = commerce_db.get(StoryRun, created.json()["id"])
    assert stored.state.status == StoryRunStatus.FAILED


def _assert_commerce_sidecars_match_real_steps(db, story_run_id: str) -> tuple[WorkflowRun, list[tuple[CommerceWorkflowStep, WorkflowStep]]]:
    """Commerce sidecar 只承载归属/唯一约束；审计实体仍是 WorkflowStep。"""

    # Worker 使用独立会话；先丢弃调用 API 会话的旧 identity-map，比较持久化事实而
    # 非本地缓存，才能证明数据库侧同步没有漂移。
    db.expire_all()
    parent = workflow_for_story_run(db, story_run_id)[0]
    linked = db.get(CommerceWorkflowLink, parent.id)
    assert linked is not None and linked.story_run_id == story_run_id
    rows = list(
        db.execute(
            select(CommerceWorkflowStep, WorkflowStep)
            .join(WorkflowStep, WorkflowStep.id == CommerceWorkflowStep.workflow_step_id)
            .where(CommerceWorkflowStep.workflow_run_id == parent.id)
            .order_by(WorkflowStep.position, WorkflowStep.attempt)
        ).all()
    )
    assert {sidecar.workflow_step_id for sidecar, _ in rows} == {step.id for step in parent.steps}
    assert len({sidecar.workflow_step_id for sidecar, _ in rows}) == len(rows)
    assert all(sidecar.workflow_run_id == parent.id for sidecar, _ in rows)
    assert all(sidecar.story_run_id == story_run_id for sidecar, _ in rows)
    assert all(sidecar.status == real_step.status.value for sidecar, real_step in rows), [
        (sidecar.workflow_step_id, sidecar.status, real_step.status.value)
        for sidecar, real_step in rows
    ]
    return parent, rows


def test_commerce_sidecars_are_one_to_one_transactional_and_status_synchronized(commerce_db):
    """sidecar 的状态不能领先或滞后真实 WorkflowStep，retry/cancel 也必须同步。"""

    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(
        commerce_db, project_id=project.id, topic_candidate_id=topic.id,
        project_product_selection_id=selection.id, mode=mode,
    )
    _, parent, _ = start_story_run(commerce_db, story_run.id)
    _, rows = _assert_commerce_sidecars_match_real_steps(commerce_db, story_run.id)
    assert [real.step_key for _, real in rows] == ["TOPIC", "OUTLINE"]

    _execute(parent)
    parent, _ = _assert_commerce_sidecars_match_real_steps(commerce_db, story_run.id)
    review_stage(
        commerce_db, story_run_id=story_run.id, stage=StoryRunStage.OUTLINE,
        decision="REJECTED", reviewer_label="一致性审核", note="要求重做", quality_score=4,
    )
    _, parent, created = continue_story_run(commerce_db, story_run.id)
    assert created
    second_outline = _stage_steps(parent, StoryRunStage.OUTLINE)[-1]
    second_outline.model_profile_snapshot["model_bindings"]["STORY_GENERATE"][0]["profile_snapshot"]["adapter_key"] = "unconfigured_adapter"
    flag_modified(second_outline, "model_profile_snapshot")
    commerce_db.commit()
    _execute(parent)
    parent, rows = _assert_commerce_sidecars_match_real_steps(commerce_db, story_run.id)
    assert rows[-1][1].status == RunStatus.FAILED

    _, parent, created = retry_step(commerce_db, story_run.id, second_outline.id)
    assert created
    parent, rows = _assert_commerce_sidecars_match_real_steps(commerce_db, story_run.id)
    assert rows[-1][1].attempt == 3 and rows[-1][1].status == RunStatus.PENDING
    cancel_story_run(commerce_db, story_run.id)
    parent, rows = _assert_commerce_sidecars_match_real_steps(commerce_db, story_run.id)
    assert parent.status == RunStatus.CANCELLED
    assert rows[-1][1].status == RunStatus.CANCELLED

    # GET /workflow 仍序列化真正的 WorkflowStep；sidecar 不替代步骤审计记录。
    with TestClient(app) as client:
        response = client.get(f"/api/v1/commerce/story-runs/{story_run.id}/workflow")
    assert response.status_code == 200
    returned_steps = response.json()[0]["steps"]
    assert {item["id"] for item in returned_steps} == {real.id for _, real in rows}


def test_sidecar_constraint_failure_rolls_back_parent_and_real_step_together(commerce_db):
    """sidecar 写入失败时服务回滚同一事务，不能留下孤儿父运行或 WorkflowStep。"""

    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(
        commerce_db, project_id=project.id, topic_candidate_id=topic.id,
        project_product_selection_id=selection.id, mode=mode,
    )

    def fail_sidecar_insert(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "insert into commerce_workflow_steps" in statement.lower():
            raise IntegrityError(
                statement,
                _parameters,
                sqlite3.IntegrityError("forced Commerce sidecar failure"),
            )

    event.listen(engine, "before_cursor_execute", fail_sidecar_insert)
    try:
        with pytest.raises(HTTPException) as failed_start:
            start_story_run(commerce_db, story_run.id)
    finally:
        event.remove(engine, "before_cursor_execute", fail_sidecar_insert)
    assert failed_start.value.status_code == 409
    commerce_db.rollback()
    assert commerce_db.scalar(
        select(func.count()).select_from(CommerceWorkflowLink).where(
            CommerceWorkflowLink.story_run_id == story_run.id
        )
    ) == 0
    assert commerce_db.scalar(
        select(func.count())
        .select_from(WorkflowStep)
        .join(WorkflowRun, WorkflowRun.id == WorkflowStep.workflow_run_id)
        .where(WorkflowRun.workflow_key == "commerce_story_run", WorkflowRun.project_id == project.id)
    ) == 0
    stored = commerce_db.get(StoryRun, story_run.id)
    assert stored is not None and stored.state.current_stage == StoryRunStage.TOPIC
    assert stored.state.status == StoryRunStatus.PENDING


def test_commerce_routes_only_delegate_and_never_mutate_orm_state_directly():
    """#28：Commerce 路由只能解析/映射 HTTP，不得绕过服务层写领域状态。"""

    from app.api.routes import commerce as commerce_routes

    source = inspect.getsource(commerce_routes)
    tree = ast.parse(source)
    forbidden_session_methods = {"add", "add_all", "delete", "execute", "commit", "flush", "rollback", "merge"}
    direct_calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden_session_methods
    ]
    assert direct_calls == []
    direct_state_assignments = [
        target.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        for target in (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        if isinstance(target, ast.Attribute)
        and target.attr in {"status", "current_stage", "stage_data"}
    ]
    assert direct_state_assignments == []
    # 路由从 commerce_workflow_service 导入状态入口，实际 endpoint 只调用这些入口。
    assert "from app.services.commerce_workflow_service import" in source
    assert "review_stage(" in source and "start_story_run(" in source and "retry_step(" in source


def test_inline_mock_closure_never_reads_decoy_key_or_calls_real_adapter(commerce_db, monkeypatch):
    """#32：完整 Commerce Mock 闭环禁止网络、真实 Adapter、供应商提交和付费调用。"""

    import app.services.analysis_provider as analysis_provider
    import app.services.v1_model_adapter_service as v1_model_adapter_service
    from app.models import ModelProfile

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Commerce inline Mock 不得触达真实供应商入口")

    # 诱饵配置存在也只能被快照保存，Mock 不得读取 key 或发起网络。
    for profile in commerce_db.scalars(select(ModelProfile).where(ModelProfile.adapter_key == "mock_v1")).all():
        profile.provider_config = {
            "api_key": "decoy-provider-key-must-not-be-read",
            "base_url": "https://provider.invalid/should-not-be-called",
        }
    commerce_db.commit()
    monkeypatch.setattr(analysis_provider, "urlopen", forbidden)
    monkeypatch.setattr(v1_model_adapter_service, "generate_structured_text", forbidden)
    monkeypatch.setattr(worker_runtime, "_enqueue_rq_job", forbidden)

    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(
        commerce_db, project_id=project.id, topic_candidate_id=topic.id,
        project_product_selection_id=selection.id, mode=mode,
    )
    _, parent, _ = start_story_run(commerce_db, story_run.id)
    _execute(parent)
    for stage in (
        StoryRunStage.OUTLINE,
        StoryRunStage.CHAPTERS,
        StoryRunStage.STORYBOARD,
        StoryRunStage.VISUAL_ASSETS,
        StoryRunStage.VIDEO_PROMPTS,
        StoryRunStage.SEGMENT_RENDER,
    ):
        _confirm(commerce_db, story_run, stage)
        if stage != StoryRunStage.SEGMENT_RENDER:
            _, parent, _ = continue_story_run(commerce_db, story_run.id)
            _execute(parent)
    assert story_run.state.status == StoryRunStatus.COMPLETED
    parent, rows = _assert_commerce_sidecars_match_real_steps(commerce_db, story_run.id)
    assert parent.status == RunStatus.SUCCEEDED
    assert all(real.provider_task_id is None for _, real in rows)
    assert all((real.output_payload or {})["cost"]["amount"] == 0 for _, real in rows)
    assert commerce_db.scalar(
        select(func.count()).select_from(ModelInvocation).where(ModelInvocation.project_id == project.id)
    ) == 0

    # 非 Mock 且没有 Commerce Adapter 的冻结配置必须真实失败，绝不能伪造成功。
    failed_run = create_next_story_run(
        commerce_db, project_id=project.id, topic_candidate_id=topic.id,
        project_product_selection_id=selection.id, mode=mode,
    )
    _, failed_parent, _ = start_story_run(commerce_db, failed_run.id)
    failed_outline = _stage_steps(failed_parent, StoryRunStage.OUTLINE)[0]
    failed_outline.model_profile_snapshot["model_bindings"]["STORY_GENERATE"][0]["profile_snapshot"]["adapter_key"] = "real_provider_decoy"
    failed_outline.model_profile_snapshot["model_bindings"]["STORY_GENERATE"][0]["adapter_snapshot"]["key"] = "real_provider_decoy"
    flag_modified(failed_outline, "model_profile_snapshot")
    commerce_db.commit()
    _execute(failed_parent)
    commerce_db.expire_all()
    persisted_step = commerce_db.get(WorkflowStep, failed_outline.id)
    persisted_run = commerce_db.get(StoryRun, failed_run.id)
    assert persisted_step.status == RunStatus.FAILED
    assert persisted_step.output_payload is None
    assert persisted_step.provider_task_id is None
    assert persisted_run.state.status == StoryRunStatus.FAILED


def test_existing_provider_task_is_never_resubmitted(commerce_db, monkeypatch):
    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(commerce_db, project_id=project.id, topic_candidate_id=topic.id, project_product_selection_id=selection.id, mode=mode)
    _, parent, _ = start_story_run(commerce_db, story_run.id)
    outline = _stage_steps(parent, StoryRunStage.OUTLINE)[0]
    outline.provider_task_id = "provider-task-redacted"
    commerce_db.commit()
    calls: list[str] = []

    class MustNotRun:
        def execute(self, context):
            calls.append(context.workflow_step.id)
            raise AssertionError("已有 provider_task_id 不得重新提交")

    monkeypatch.setattr(CommerceNodeRegistry, "_executor", MustNotRun())
    execute_commerce_workflow(parent.id)
    execute_commerce_workflow(parent.id)
    commerce_db.expire_all()
    step = _stage_steps(commerce_db.get(WorkflowRun, parent.id), StoryRunStage.OUTLINE)[0]
    assert calls == [] and step.status == RunStatus.RUNNING and step.provider_task_id == "provider-task-redacted"


def _run_to_video_prompts_pause(db, story_run: StoryRun) -> WorkflowRun:
    """推进 STEPWISE Mock 闭环至待审核的视频提示词阶段。"""

    run = _run_to_storyboard_pause(db, story_run)
    _confirm(db, story_run, StoryRunStage.STORYBOARD)
    _, run, _ = continue_story_run(db, story_run.id)
    _execute(run)
    _confirm(db, story_run, StoryRunStage.VISUAL_ASSETS)
    _, run, _ = continue_story_run(db, story_run.id)
    _execute(run)
    return run


def test_chapters_rejected_attempt_is_preserved_and_storyboard_uses_only_replacement(commerce_db):
    """回归 1：CHAPTERS 重做永不覆盖旧章节，后续只引用新 attempt 的结果组。"""

    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(
        commerce_db, project_id=project.id, topic_candidate_id=topic.id,
        project_product_selection_id=selection.id, mode=mode,
    )
    _, parent, _ = start_story_run(commerce_db, story_run.id)
    _execute(parent)
    _confirm(commerce_db, story_run, StoryRunStage.OUTLINE)
    _, parent, _ = continue_story_run(commerce_db, story_run.id)
    _execute(parent)
    first_step = _stage_steps(parent, StoryRunStage.CHAPTERS)[0]
    first_chapter_id = first_step.output_payload["artifact_references"]["chapter_ids"][0]
    review_stage(
        commerce_db, story_run_id=story_run.id, stage=StoryRunStage.CHAPTERS,
        decision="REJECTED", reviewer_label="章节审核", note="重做章节", quality_score=3,
    )
    _, parent, created = continue_story_run(commerce_db, story_run.id)
    assert created
    _execute(parent)
    commerce_db.expire_all()
    parent = commerce_db.get(WorkflowRun, parent.id)
    second_step = _stage_steps(parent, StoryRunStage.CHAPTERS)[-1]
    assert second_step.output_payload is not None, (second_step.status, second_step.error_message)
    second_chapter_id = second_step.output_payload["artifact_references"]["chapter_ids"][0]
    assert second_step.attempt == 2 and second_chapter_id != first_chapter_id
    links = list(commerce_db.scalars(select(CommerceChapterAttemptChapter).where(
        CommerceChapterAttemptChapter.workflow_step_id.in_([first_step.id, second_step.id])
    )).all())
    assert {(item.workflow_step_id, item.chapter_plan_id) for item in links} == {
        (first_step.id, first_chapter_id), (second_step.id, second_chapter_id)
    }
    assert commerce_db.get(__import__("app.models", fromlist=["ChapterPlan"]).ChapterPlan, first_chapter_id) is not None
    _confirm(commerce_db, story_run, StoryRunStage.CHAPTERS)
    _, parent, _ = continue_story_run(commerce_db, story_run.id)
    _execute(parent)
    commerce_db.expire_all()
    parent = commerce_db.get(WorkflowRun, parent.id)
    storyboard = _stage_steps(parent, StoryRunStage.STORYBOARD)[0]
    assert storyboard.output_payload["artifact_references"]["chapter_ids"] == [second_chapter_id]
    assert first_chapter_id not in storyboard.output_payload["artifact_references"]["chapter_ids"]


def test_auto_incomplete_chapters_attempt_does_not_advance(commerce_db, monkeypatch):
    """回归 2：AUTO 必须校验当前 CHAPTERS attempt，不得读取旧章节后继续。"""

    project, topic, selection, _ = _context(commerce_db, mode=StoryRunMode.AUTO)
    story_run = create_next_story_run(
        commerce_db, project_id=project.id, topic_candidate_id=topic.id,
        project_product_selection_id=selection.id, mode=StoryRunMode.AUTO,
    )
    original = CommerceNodeRegistry._executor

    class IncompleteChapters:
        def execute(self, context):
            if context.stage != StoryRunStage.CHAPTERS:
                return original.execute(context)
            outline = commerce_db.scalars(select(__import__("app.models", fromlist=["StoryOutlineVersion"]).StoryOutlineVersion).where(
                __import__("app.models", fromlist=["StoryOutlineVersion"]).StoryOutlineVersion.story_run_id == context.story_run.id,
            )).first()
            return CommerceNodeResult(
                artifact_references={"chapter_ids": [], "outline_id": outline.id},
                structured_output={}, usage={}, cost={},
            )

    monkeypatch.setattr(CommerceNodeRegistry, "_executor", IncompleteChapters())
    _, parent, _ = start_story_run(commerce_db, story_run.id)
    _execute(parent)
    _confirm(commerce_db, story_run, StoryRunStage.OUTLINE)
    _, parent, _ = continue_story_run(commerce_db, story_run.id)
    _execute(parent)
    commerce_db.expire_all()
    stored = commerce_db.get(StoryRun, story_run.id)
    failed = _stage_steps(commerce_db.get(WorkflowRun, parent.id), StoryRunStage.CHAPTERS)[0]
    assert stored.state.current_stage == StoryRunStage.CHAPTERS
    assert stored.state.status == StoryRunStatus.FAILED
    assert failed.status == RunStatus.FAILED
    assert not _stage_steps(commerce_db.get(WorkflowRun, parent.id), StoryRunStage.STORYBOARD)


def test_storyboard_product_placement_validation_rejects_wrong_version_and_foreign_subshot(commerce_db):
    """回归 5：章节、片段、子镜头植入都必须属于当前结果并使用冻结产品版本。"""

    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(commerce_db, project_id=project.id, topic_candidate_id=topic.id, project_product_selection_id=selection.id, mode=mode)
    parent = _run_to_storyboard_pause(commerce_db, story_run)
    board = _stage_steps(parent, StoryRunStage.STORYBOARD)[0]
    refs = board.output_payload["artifact_references"]
    current_segment = commerce_db.get(VideoSegmentPlan, refs["video_segment_ids"][0])
    wrong_project, _, wrong_selection, _ = _context(commerce_db)
    wrong = ProductPlacementPlan(
        story_run_id=story_run.id, product_asset_version_id=wrong_selection.product_asset_version_id,
        chapter_id=None, video_segment_id=None, sub_shot_id=current_segment.sub_shots[0].id,
        placement_method=ProductPlacementMethod.SOFT_PROP, placement_strength=ProductPlacementStrength.LIGHT,
        pain_point_trigger="错误产品", product_action="错误", ad_entry_point="错误", story_recovery_point="错误", planned_duration_ms=1,
    )
    commerce_db.add(wrong)
    commerce_db.flush()
    board.output_payload = {**board.output_payload, "artifact_references": {**refs, "product_placement_ids": [wrong.id]}}
    flag_modified(board, "output_payload")
    commerce_db.commit()
    before = commerce_db.scalar(
        select(func.count()).select_from(ReviewDecision).where(ReviewDecision.target_id == board.id)
    )
    with pytest.raises(HTTPException) as wrong_version:
        _confirm(commerce_db, story_run, StoryRunStage.STORYBOARD)
    assert wrong_version.value.status_code == 422
    assert commerce_db.scalar(
        select(func.count()).select_from(ReviewDecision).where(ReviewDecision.target_id == board.id)
    ) == before

    # 新建一个同项目 StoryRun 的子镜头，直接伪造跨运行引用；审核仍必须拒绝且不落审核事实。
    other = create_next_story_run(commerce_db, project_id=project.id, topic_candidate_id=topic.id, project_product_selection_id=selection.id, mode=mode)
    other_parent = _run_to_storyboard_pause(commerce_db, other)
    other_shot_id = commerce_db.get(VideoSegmentPlan, _stage_steps(other_parent, StoryRunStage.STORYBOARD)[0].output_payload["artifact_references"]["video_segment_ids"][0]).sub_shots[0].id
    foreign = ProductPlacementPlan(
        story_run_id=story_run.id, product_asset_version_id=story_run.product_asset_version_id,
        chapter_id=None, video_segment_id=None, sub_shot_id=other_shot_id,
        placement_method=ProductPlacementMethod.SOFT_PROP, placement_strength=ProductPlacementStrength.LIGHT,
        pain_point_trigger="跨运行", product_action="错误", ad_entry_point="错误", story_recovery_point="错误", planned_duration_ms=1,
    )
    commerce_db.add(foreign)
    commerce_db.flush()
    board.output_payload = {**board.output_payload, "artifact_references": {**refs, "product_placement_ids": [foreign.id]}}
    flag_modified(board, "output_payload")
    commerce_db.commit()
    with pytest.raises(HTTPException) as foreign_target:
        _confirm(commerce_db, story_run, StoryRunStage.STORYBOARD)
    assert foreign_target.value.status_code == 422
    assert commerce_db.scalar(
        select(func.count()).select_from(ReviewDecision).where(ReviewDecision.target_id == board.id)
    ) == before


def test_stepwise_prompt_rejects_versions_without_locking_them(commerce_db):
    """回归 8：STEPWISE 成功仅生成 DRAFT，驳回后保留版本且不再被采用。"""

    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(commerce_db, project_id=project.id, topic_candidate_id=topic.id, project_product_selection_id=selection.id, mode=mode)
    parent = _run_to_video_prompts_pause(commerce_db, story_run)
    step = _stage_steps(parent, StoryRunStage.VIDEO_PROMPTS)[0]
    prompt_ids = step.output_payload["artifact_references"]["video_prompt_version_ids"]
    prompts = [commerce_db.get(VideoPromptVersion, item_id) for item_id in prompt_ids]
    assert all(item.status == "DRAFT" and item.locked_at is None for item in prompts)
    review_stage(commerce_db, story_run_id=story_run.id, stage=StoryRunStage.VIDEO_PROMPTS,
                 decision="REJECTED", reviewer_label="提示词审核", note="重做", quality_score=4)
    assert all(item.status == "REJECTED" and item.locked_at is None for item in prompts)


def test_video_prompt_review_requires_complete_accepted_storyboard_coverage(commerce_db):
    """回归 6：视频提示词不能只引用已确认 Storyboard 的非空子集。"""

    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(commerce_db, project_id=project.id, topic_candidate_id=topic.id, project_product_selection_id=selection.id, mode=mode)
    parent = _run_to_storyboard_pause(commerce_db, story_run)
    board = _stage_steps(parent, StoryRunStage.STORYBOARD)[0]
    refs = board.output_payload["artifact_references"]
    first_segment = commerce_db.get(VideoSegmentPlan, refs["video_segment_ids"][0])
    second_segment = create_video_segment_plan(
        commerce_db, story_run_id=story_run.id, chapter_id=first_segment.chapter_id,
        segment_number=first_segment.segment_number + 1, target_duration_ms=4000,
        narrative_target="第二个必须覆盖的已确认片段",
    )
    second_shot = add_sub_shot_plan(
        commerce_db, second_segment, shot_number=1, start_ms=0, end_ms=4000,
        action="继续体验产品", emotion="安心", shot_scale="中景", camera_move="固定",
        lighting="柔光", visual_description="第二段连续画面",
    )
    second_placement = create_product_placement_plan(
        commerce_db, story_run_id=story_run.id, product_asset_version_id=story_run.product_asset_version_id,
        chapter_id=None, video_segment_id=None, sub_shot_id=second_shot.id,
        placement_method=ProductPlacementMethod.SOFT_PROP, placement_strength=ProductPlacementStrength.LIGHT,
        pain_point_trigger="继续展示", product_action="继续使用", ad_entry_point="第二段", story_recovery_point="收束", planned_duration_ms=1000,
    )
    board.output_payload = {
        **board.output_payload,
        "artifact_references": {
            **refs,
            "video_segment_ids": [*refs["video_segment_ids"], second_segment.id],
            "product_placement_ids": [*refs["product_placement_ids"], second_placement.id],
        },
    }
    flag_modified(board, "output_payload")
    commerce_db.commit()
    _confirm(commerce_db, story_run, StoryRunStage.STORYBOARD)
    _, parent, _ = continue_story_run(commerce_db, story_run.id)
    _execute(parent)
    _confirm(commerce_db, story_run, StoryRunStage.VISUAL_ASSETS)
    _, parent, _ = continue_story_run(commerce_db, story_run.id)
    _execute(parent)
    commerce_db.expire_all()
    parent = commerce_db.get(WorkflowRun, parent.id)
    prompt_step = _stage_steps(parent, StoryRunStage.VIDEO_PROMPTS)[0]
    prompt_ids = prompt_step.output_payload["artifact_references"]["video_prompt_version_ids"]
    assert len(prompt_ids) == 2
    prompt_step.output_payload = {
        **prompt_step.output_payload,
        "artifact_references": {"video_prompt_version_ids": [prompt_ids[0]]},
    }
    flag_modified(prompt_step, "output_payload")
    commerce_db.commit()
    before = commerce_db.scalar(
        select(func.count()).select_from(ReviewDecision).where(ReviewDecision.target_id == prompt_step.id)
    )
    with pytest.raises(HTTPException) as partial:
        _confirm(commerce_db, story_run, StoryRunStage.VIDEO_PROMPTS)
    assert partial.value.status_code == 422
    assert story_run.state.status == StoryRunStatus.PAUSED
    assert commerce_db.scalar(
        select(func.count()).select_from(ReviewDecision).where(ReviewDecision.target_id == prompt_step.id)
    ) == before


def test_render_review_requires_completed_batch_and_every_accepted_segment(commerce_db):
    """回归 7：批次计数与状态都不能替代全部已采用片段的成功结果。"""

    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(commerce_db, project_id=project.id, topic_candidate_id=topic.id, project_product_selection_id=selection.id, mode=mode)
    parent = _run_to_video_prompts_pause(commerce_db, story_run)
    _confirm(commerce_db, story_run, StoryRunStage.VIDEO_PROMPTS)
    _, parent, _ = continue_story_run(commerce_db, story_run.id)
    _execute(parent)
    step = _stage_steps(parent, StoryRunStage.SEGMENT_RENDER)[0]
    batch = commerce_db.get(__import__("app.models", fromlist=["RenderBatch"]).RenderBatch, step.output_payload["artifact_references"]["render_batch_id"])
    accepted = commerce_db.get(VideoPromptVersion, step.workflow_run.steps[-2].output_payload["artifact_references"]["video_prompt_version_ids"][0]).video_segment
    before = commerce_db.scalar(select(func.count()).select_from(ReviewDecision))
    for batch_status, total, completed, failed, running, segment_status in (
        (RenderBatchStatus.PENDING, 1, 1, 0, 0, SegmentPlanStatus.COMPLETED),
        (RenderBatchStatus.FAILED, 1, 1, 0, 0, SegmentPlanStatus.COMPLETED),
        (RenderBatchStatus.COMPLETED, 0, 0, 0, 0, SegmentPlanStatus.COMPLETED),
        (RenderBatchStatus.COMPLETED, 1, 0, 1, 0, SegmentPlanStatus.COMPLETED),
        (RenderBatchStatus.COMPLETED, 1, 1, 0, 0, SegmentPlanStatus.FAILED),
    ):
        batch.status, batch.total_tasks, batch.completed_tasks = batch_status, total, completed
        batch.failed_tasks, batch.running_tasks, accepted.status = failed, running, segment_status
        commerce_db.commit()
        with pytest.raises(HTTPException) as incomplete:
            _confirm(commerce_db, story_run, StoryRunStage.SEGMENT_RENDER)
        assert incomplete.value.status_code == 409
        assert commerce_db.scalar(select(func.count()).select_from(ReviewDecision)) == before


def test_retry_rejects_stale_failed_attempt_and_accepts_latest(commerce_db):
    """回归 9：retry 只能从当前阶段最后一个 FAILED attempt 产生下一版。"""

    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(commerce_db, project_id=project.id, topic_candidate_id=topic.id, project_product_selection_id=selection.id, mode=mode)
    _, parent, _ = start_story_run(commerce_db, story_run.id)
    first = _stage_steps(parent, StoryRunStage.OUTLINE)[0]
    first.model_profile_snapshot["model_bindings"]["STORY_GENERATE"][0]["profile_snapshot"]["adapter_key"] = "unsupported"
    flag_modified(first, "model_profile_snapshot")
    commerce_db.commit()
    _execute(parent)
    _, parent, _ = retry_step(commerce_db, story_run.id, first.id)
    second = _stage_steps(parent, StoryRunStage.OUTLINE)[-1]
    second.model_profile_snapshot["model_bindings"]["STORY_GENERATE"][0]["profile_snapshot"]["adapter_key"] = "unsupported"
    flag_modified(second, "model_profile_snapshot")
    commerce_db.commit()
    _execute(parent)
    with pytest.raises(HTTPException) as stale:
        retry_step(commerce_db, story_run.id, first.id)
    assert stale.value.status_code == 409
    _, parent, created = retry_step(commerce_db, story_run.id, second.id)
    assert created and _stage_steps(parent, StoryRunStage.OUTLINE)[-1].attempt == 3


def test_auto_mode_locks_prompts_and_reaches_completed_after_required_review_gates(commerce_db):
    """回归 1：AUTO 不伪造提示词审核，但必须能采用 LOCKED attempt 完成渲染。"""

    project, topic, selection, _ = _context(commerce_db, mode=StoryRunMode.AUTO)
    story_run = create_next_story_run(
        commerce_db, project_id=project.id, topic_candidate_id=topic.id,
        project_product_selection_id=selection.id, mode=StoryRunMode.AUTO,
    )
    _, parent, _ = start_story_run(commerce_db, story_run.id)
    _execute(parent)  # OUTLINE -> PAUSED
    _confirm(commerce_db, story_run, StoryRunStage.OUTLINE)
    _, parent, _ = continue_story_run(commerce_db, story_run.id)
    _execute(parent)  # CHAPTERS -> 自动创建 STORYBOARD
    _execute(parent)  # STORYBOARD -> PAUSED
    assert story_run.state.current_stage == StoryRunStage.STORYBOARD
    _confirm(commerce_db, story_run, StoryRunStage.STORYBOARD)
    _, parent, _ = continue_story_run(commerce_db, story_run.id)
    _execute(parent)  # VISUAL_ASSETS -> PAUSED
    _confirm(commerce_db, story_run, StoryRunStage.VISUAL_ASSETS)
    _, parent, _ = continue_story_run(commerce_db, story_run.id)
    _execute(parent)  # VIDEO_PROMPTS -> 自动锁定并创建 SEGMENT_RENDER

    commerce_db.expire_all()
    parent = commerce_db.get(WorkflowRun, parent.id)
    prompt_step = _stage_steps(parent, StoryRunStage.VIDEO_PROMPTS)[-1]
    prompt_ids = prompt_step.output_payload["artifact_references"]["video_prompt_version_ids"]
    prompts = [commerce_db.get(VideoPromptVersion, item_id) for item_id in prompt_ids]
    assert all(item.status == "LOCKED" and item.locked_at is not None for item in prompts)
    assert commerce_db.scalar(
        select(func.count()).select_from(ReviewDecision).where(
            ReviewDecision.target_type == "COMMERCE_STAGE_VIDEO_PROMPTS",
            ReviewDecision.target_id == prompt_step.id,
        )
    ) == 0

    _execute(parent)  # SEGMENT_RENDER reads the AUTO-adopted locked prompt attempt.
    commerce_db.expire_all()
    assert commerce_db.get(StoryRun, story_run.id).state.current_stage == StoryRunStage.SEGMENT_RENDER
    assert commerce_db.get(StoryRun, story_run.id).state.status == StoryRunStatus.PAUSED
    _confirm(commerce_db, story_run, StoryRunStage.SEGMENT_RENDER)
    commerce_db.expire_all()
    finished = commerce_db.get(StoryRun, story_run.id)
    assert finished.state.current_stage == StoryRunStage.COMPLETED
    assert finished.state.status == StoryRunStatus.COMPLETED


def test_retry_rejects_old_failed_step_when_attempt_three_is_active_and_nonfailed_step(commerce_db):
    """回归 7/8：仅失败的最新 predecessor 可复用紧邻的活动 retry。"""

    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(
        commerce_db, project_id=project.id, topic_candidate_id=topic.id,
        project_product_selection_id=selection.id, mode=mode,
    )
    _, parent, _ = start_story_run(commerce_db, story_run.id)
    first = _stage_steps(parent, StoryRunStage.OUTLINE)[0]
    first.model_profile_snapshot["model_bindings"]["STORY_GENERATE"][0]["profile_snapshot"]["adapter_key"] = "unsupported"
    flag_modified(first, "model_profile_snapshot")
    commerce_db.commit()
    _execute(parent)
    _, parent, _ = retry_step(commerce_db, story_run.id, first.id)
    second = _stage_steps(parent, StoryRunStage.OUTLINE)[-1]
    second.model_profile_snapshot["model_bindings"]["STORY_GENERATE"][0]["profile_snapshot"]["adapter_key"] = "unsupported"
    flag_modified(second, "model_profile_snapshot")
    commerce_db.commit()
    _execute(parent)
    _, parent, _ = retry_step(commerce_db, story_run.id, second.id)  # attempt 3 is active.
    third = _stage_steps(parent, StoryRunStage.OUTLINE)[-1]
    assert third.attempt == 3 and third.status == RunStatus.PENDING

    with pytest.raises(HTTPException) as stale_attempt_one:
        retry_step(commerce_db, story_run.id, first.id)
    assert stale_attempt_one.value.status_code == 409

    # A successfully completed attempt cannot be retried even if the caller has
    # a valid StoryRun ID; it must not be mistaken for an idempotent retry.
    completed = create_next_story_run(
        commerce_db, project_id=project.id, topic_candidate_id=topic.id,
        project_product_selection_id=selection.id, mode=mode,
    )
    _, completed_parent, _ = start_story_run(commerce_db, completed.id)
    _execute(completed_parent)
    succeeded_outline = _stage_steps(completed_parent, StoryRunStage.OUTLINE)[0]
    with pytest.raises(HTTPException) as non_failed:
        retry_step(commerce_db, completed.id, succeeded_outline.id)
    assert non_failed.value.status_code == 409


def test_storyboard_retry_does_not_adopt_rejected_chapter_level_placement(commerce_db):
    """回归 9：显式 product_placement_ids 隔离同章的历史被驳回植入。"""

    project, topic, selection, mode = _context(commerce_db)
    story_run = create_next_story_run(
        commerce_db, project_id=project.id, topic_candidate_id=topic.id,
        project_product_selection_id=selection.id, mode=mode,
    )
    parent = _run_to_storyboard_pause(commerce_db, story_run)
    first_board = _stage_steps(parent, StoryRunStage.STORYBOARD)[0]
    first_refs = first_board.output_payload["artifact_references"]
    old_placement = create_product_placement_plan(
        commerce_db,
        story_run_id=story_run.id,
        product_asset_version_id=story_run.product_asset_version_id,
        chapter_id=first_refs["chapter_ids"][0],
        video_segment_id=None,
        sub_shot_id=None,
        placement_method=ProductPlacementMethod.SOFT_PROP,
        placement_strength=ProductPlacementStrength.LIGHT,
        pain_point_trigger="旧 attempt 的章节级痛点",
        product_action="旧植入动作",
        ad_entry_point="旧进入点",
        story_recovery_point="旧恢复点",
        planned_duration_ms=800,
    )
    first_board.output_payload = {
        **first_board.output_payload,
        "artifact_references": {
            **first_refs,
            "product_placement_ids": [*first_refs["product_placement_ids"], old_placement.id],
        },
    }
    flag_modified(first_board, "output_payload")
    commerce_db.commit()
    review_stage(
        commerce_db, story_run_id=story_run.id, stage=StoryRunStage.STORYBOARD,
        decision="REJECTED", reviewer_label="分镜审核", note="重做", quality_score=3,
    )
    _, parent, _ = continue_story_run(commerce_db, story_run.id)
    _execute(parent)
    commerce_db.expire_all()
    second_board = _stage_steps(commerce_db.get(WorkflowRun, parent.id), StoryRunStage.STORYBOARD)[-1]
    second_refs = second_board.output_payload["artifact_references"]
    assert old_placement.id not in second_refs["product_placement_ids"]
    _confirm(commerce_db, story_run, StoryRunStage.STORYBOARD)
    assert story_run.state.current_stage == StoryRunStage.VISUAL_ASSETS


def test_0012_nonempty_round_trip_preserves_existing_workflow_runs(tmp_path):
    """0012 不回填历史，也必须在非空 0011 数据上安全往返。"""

    database_url = f"sqlite:///{tmp_path / 'commerce-0012.db'}"
    server_root = Path(__file__).resolve().parents[1]

    def migrate(action: str, revision: str) -> None:
        config = Config(str(server_root / "alembic.ini"))
        config.set_main_option("script_location", str(server_root / "migrations"))
        migration_engine = create_engine(database_url)
        try:
            with migration_engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()
                config.attributes["connection"] = connection
                getattr(command, action)(config, revision)
                config.attributes.pop("connection", None)
        finally:
            migration_engine.dispose()

    migrate("upgrade", "0011_commerce_domain_integrity_fixes")
    migration_engine = create_engine(database_url)
    project_id, run_id = str(uuid4()), str(uuid4())
    try:
        with migration_engine.begin() as connection:
            metadata = MetaData()
            metadata.reflect(bind=connection, only=["projects", "workflow_runs"])
            now = datetime.now(timezone.utc)
            connection.execute(metadata.tables["projects"].insert(), {
                "id": project_id, "title": "0012 非空测试", "description": None,
                "created_at": now, "updated_at": now,
            })
            connection.execute(metadata.tables["workflow_runs"].insert(), {
                "id": run_id, "project_id": project_id, "workflow_key": "legacy_before_0012",
                "workflow_definition_id": None, "workflow_version": None, "input_snapshot": None,
                "idempotency_key": None, "status": "SUCCEEDED", "created_at": now,
                "started_at": now, "finished_at": now,
            })
    finally:
        migration_engine.dispose()
    migrate("upgrade", "0012_commerce_workflow_orchestration")
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.connect() as connection:
            columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(workflow_runs)")}
            assert "story_run_id" not in columns
            assert connection.exec_driver_sql("SELECT id FROM workflow_runs WHERE id = ?", (run_id,)).one() == (run_id,)
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM commerce_workflow_links").scalar_one() == 0
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM video_prompt_versions").scalar_one() == 0
    finally:
        migration_engine.dispose()
    migrate("downgrade", "0011_commerce_domain_integrity_fixes")
    migrate("upgrade", "0012_commerce_workflow_orchestration")


def test_0013_sqlite_integrity_triggers_and_nonempty_round_trip(tmp_path):
    """非空 0012 → 0013 → 0012 → 0013 保留引用并加固跨表 sidecar 语义。"""

    database_url = f"sqlite:///{tmp_path / 'commerce-0012-integrity.db'}"
    server_root = Path(__file__).resolve().parents[1]

    def migrate(revision: str, *, downgrade: bool = False) -> None:
        config = Config(str(server_root / "alembic.ini"))
        config.set_main_option("script_location", str(server_root / "migrations"))
        migration_engine = create_engine(database_url)
        try:
            with migration_engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()
                config.attributes["connection"] = connection
                (command.downgrade if downgrade else command.upgrade)(config, revision)
                config.attributes.pop("connection", None)
        finally:
            migration_engine.dispose()

    def sqlite_state(expected_prompt_table: bool) -> tuple[dict[str, int], set[str], set[str]]:
        migration_engine = create_engine(database_url)
        try:
            with migration_engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
                tables = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
                assert ("video_prompt_versions" in tables) is expected_prompt_table
                assert connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp_%'").fetchall() == []
                counts = {
                    table: int(connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar_one())
                    for table in ("projects", "topic_candidates", "product_assets", "product_analysis_versions", "product_asset_versions", "project_product_selections", "story_runs", "workflow_runs", "workflow_steps")
                }
                triggers = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger'")}
                indexes = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='index'")}
                return counts, triggers, indexes
        finally:
            migration_engine.dispose()

    migrate("0011_commerce_domain_integrity_fixes")
    seed_ids = {name: str(uuid4()) for name in (
        "project", "legacy_run", "legacy_step", "topic", "product", "analysis", "product_version", "selection",
        "story_run", "outline", "chapter", "segment",
    )}
    now = datetime.now(timezone.utc)
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.begin() as connection:
            metadata = MetaData()
            metadata.reflect(bind=connection)
            table = metadata.tables
            connection.execute(table["projects"].insert(), {"id": seed_ids["project"], "title": "0012 完整性", "description": None, "created_at": now, "updated_at": now})
            connection.execute(table["workflow_runs"].insert(), {"id": seed_ids["legacy_run"], "project_id": seed_ids["project"], "workflow_key": "legacy", "workflow_definition_id": None, "workflow_version": None, "idempotency_key": None, "input_snapshot": {}, "status": "SUCCEEDED", "created_at": now, "started_at": now, "finished_at": now})
            connection.execute(table["workflow_steps"].insert(), {"id": seed_ids["legacy_step"], "workflow_run_id": seed_ids["legacy_run"], "step_key": "LEGACY", "position": 1, "status": "SUCCEEDED", "progress": 100, "attempt": 1, "input_payload": {}, "output_payload": {}, "error_message": None, "model_profile_snapshot": None, "idempotency_key": None, "shot_plan_id": None, "video_clip_id": None, "provider_task_id": None, "created_at": now, "started_at": now, "finished_at": now})
            connection.execute(table["topic_candidates"].insert(), {"id": seed_ids["topic"], "project_id": seed_ids["project"], "generation_run_id": seed_ids["legacy_run"], "position": 1, "title": "选题", "opening_hook": "开头", "synopsis": "摘要", "score": None, "scoring_notes": None, "status": "DRAFT", "created_at": now, "updated_at": now})
            connection.execute(table["product_assets"].insert(), {"id": seed_ids["product"], "name": "产品", "description": None, "created_at": now, "updated_at": now})
            connection.execute(table["product_analysis_versions"].insert(), {"id": seed_ids["analysis"], "product_asset_id": seed_ids["product"], "source_media_asset_id": None, "version": 1, "raw_analysis": {}, "analysis_status": "SUCCEEDED", "created_at": now, "updated_at": now, "product_identification": {}, "package_ocr": {}, "candidate_reference_images": [], "appearance_description_candidates": [], "selling_point_candidates": [], "user_pain_point_candidates": [], "usage_scenario_candidates": []})
            connection.execute(table["product_asset_versions"].insert(), {"id": seed_ids["product_version"], "product_asset_id": seed_ids["product"], "source_analysis_version_id": seed_ids["analysis"], "version": 1, "product_name": "产品 v1", "appearance_description": "包装", "selling_points": [], "user_pain_points": [], "usage_scenarios": [], "package_ocr": {}, "reference_images": [], "status": "CONFIRMED", "frozen_at": now, "created_at": now})
            connection.execute(table["project_product_selections"].insert(), {"id": seed_ids["selection"], "project_id": seed_ids["project"], "product_asset_id": seed_ids["product"], "product_asset_version_id": seed_ids["product_version"], "selected_at": now, "created_at": now})
            connection.execute(table["story_runs"].insert(), {"id": seed_ids["story_run"], "project_id": seed_ids["project"], "topic_candidate_id": seed_ids["topic"], "project_product_selection_id": seed_ids["selection"], "product_asset_version_id": seed_ids["product_version"], "run_number": 1, "mode": "STEPWISE", "created_at": now, "updated_at": now})
            connection.execute(table["story_run_states"].insert(), {"story_run_id": seed_ids["story_run"], "current_stage": "VIDEO_PROMPTS", "status": "PAUSED", "stage_data": {}, "created_at": now, "updated_at": now})
            connection.execute(table["story_outline_versions"].insert(), {"id": seed_ids["outline"], "story_run_id": seed_ids["story_run"], "version": 1, "title": "大纲", "premise": "前提", "story_beats": [], "product_placement_strategy": {}, "status": "LOCKED", "created_at": now})
            connection.execute(table["chapter_plans"].insert(), {"id": seed_ids["chapter"], "story_run_id": seed_ids["story_run"], "outline_version_id": seed_ids["outline"], "chapter_number": 1, "title": "章节", "narrative_purpose": "目的", "content_summary": "内容", "product_plan": {}, "created_at": now, "updated_at": now})
            connection.execute(table["video_segment_plans"].insert(), {"id": seed_ids["segment"], "story_run_id": seed_ids["story_run"], "chapter_id": seed_ids["chapter"], "segment_number": 1, "target_duration_ms": 4000, "narrative_target": "叙事", "status": "DRAFT", "video_prompt_version": None, "video_prompt_trace": {}, "created_at": now, "updated_at": now})
    finally:
        migration_engine.dispose()
    counts_0011, triggers_0011, _ = sqlite_state(False)
    assert counts_0011["story_runs"] == 1 and not any(name.startswith("trg_commerce_") for name in triggers_0011)

    # 明确从已发布 0012 的非空 Commerce 数据升级到 0013，而非直接跳过中间 schema。
    migrate("0012_commerce_workflow_orchestration")
    migrate("0013_commerce_phase2_integrity_fixes")
    commerce_run, commerce_step, prompt = str(uuid4()), str(uuid4()), str(uuid4())
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
            metadata = MetaData()
            metadata.reflect(bind=connection)
            table = metadata.tables
            connection.commit()
            with connection.begin():
                connection.execute(table["workflow_runs"].insert(), {"id": commerce_run, "project_id": seed_ids["project"], "workflow_key": "commerce_story_run", "workflow_definition_id": None, "workflow_version": "LemonFlow_Commerce_V1", "idempotency_key": "run-integrity", "input_snapshot": {}, "status": "PENDING", "created_at": now, "started_at": None, "finished_at": None})
                connection.execute(table["commerce_workflow_links"].insert(), {"workflow_run_id": commerce_run, "story_run_id": seed_ids["story_run"], "created_at": now})
                connection.execute(table["workflow_steps"].insert(), {"id": commerce_step, "workflow_run_id": commerce_run, "step_key": "VIDEO_PROMPTS", "position": 6, "status": "SUCCEEDED", "progress": 100, "attempt": 1, "input_payload": {}, "output_payload": {}, "error_message": None, "model_profile_snapshot": {}, "idempotency_key": "step-integrity", "shot_plan_id": None, "video_clip_id": None, "provider_task_id": None, "created_at": now, "started_at": now, "finished_at": now})
                connection.execute(table["commerce_workflow_steps"].insert(), {"workflow_step_id": commerce_step, "workflow_run_id": commerce_run, "story_run_id": seed_ids["story_run"], "stage": "VIDEO_PROMPTS", "attempt": 1, "status": "SUCCEEDED", "created_at": now})
                connection.execute(table["video_prompt_versions"].insert(), {"id": prompt, "video_segment_id": seed_ids["segment"], "workflow_step_id": commerce_step, "version": 1, "prompt": "冻结提示词", "trace": {}, "status": "DRAFT", "created_at": now, "locked_at": None})
            # Commerce sidecar 的非法 INSERT 与 UPDATE 都由 SQLite trigger/FK 拒绝。
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql("INSERT INTO commerce_workflow_links (workflow_run_id, story_run_id, created_at) VALUES (?, ?, ?)", (str(uuid4()), "missing-story", now))
            connection.rollback()
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql("UPDATE commerce_workflow_steps SET story_run_id = ? WHERE workflow_step_id = ?", ("missing-story", commerce_step))
            connection.rollback()
            # 已建立 link 的父运行既不能改成 V1/其他 workflow_key，也不能改到其他
            # 项目；这两条约束不能只依赖应用服务的创建路径。
            foreign_project = str(uuid4())
            with connection.begin():
                connection.exec_driver_sql(
                    "INSERT INTO projects (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (foreign_project, "错误项目", now, now),
                )
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql(
                    "UPDATE workflow_runs SET workflow_key = 'v1_video_generation' WHERE id = ?", (commerce_run,)
                )
            connection.rollback()
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql(
                    "UPDATE workflow_runs SET project_id = ? WHERE id = ?", (foreign_project, commerce_run)
                )
            connection.rollback()
            # 不同阶段也不能在同一 Commerce 父运行下同时处于活动状态；否则当前
            # 步骤、retry 目标和队列消息都会产生歧义。
            connection.exec_driver_sql("UPDATE workflow_steps SET status = 'PENDING' WHERE id = ?", (commerce_step,))
            connection.commit()
            assert connection.exec_driver_sql(
                "SELECT status FROM commerce_workflow_steps WHERE workflow_step_id = ?", (commerce_step,)
            ).scalar_one() == "PENDING"
            # sidecar 不能被直接改成领先/滞后真实 WorkflowStep 的状态；反方向由
            # workflow_steps trigger 在同一事务同步，这同时保护活动部分唯一索引。
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql(
                    "UPDATE commerce_workflow_steps SET status = 'SUCCEEDED' WHERE workflow_step_id = ?",
                    (commerce_step,),
                )
            connection.rollback()
            assert connection.exec_driver_sql(
                "SELECT c.status, w.status FROM commerce_workflow_steps c "
                "JOIN workflow_steps w ON w.id = c.workflow_step_id WHERE c.workflow_step_id = ?",
                (commerce_step,),
            ).one() == ("PENDING", "PENDING")
            # 0013 除状态外还冻结 stage 与 attempt；sidecar 不能伪装为另一个真实节点。
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql(
                    "UPDATE commerce_workflow_steps SET stage = 'CHAPTERS' WHERE workflow_step_id = ?",
                    (commerce_step,),
                )
            connection.rollback()
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql(
                    "UPDATE commerce_workflow_steps SET attempt = 2 WHERE workflow_step_id = ?",
                    (commerce_step,),
                )
            connection.rollback()
            competing_step = str(uuid4())
            connection.exec_driver_sql(
                "INSERT INTO workflow_steps (id, workflow_run_id, step_key, position, status, progress, attempt, input_payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (competing_step, commerce_run, "CHAPTERS", 3, "PENDING", 0, 1, "{}", now),
            )
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql(
                    "INSERT INTO commerce_workflow_steps (workflow_step_id, workflow_run_id, story_run_id, stage, attempt, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (competing_step, commerce_run, seed_ids["story_run"], "CHAPTERS", 1, "PENDING", now),
                )
            connection.rollback()
            connection.exec_driver_sql("UPDATE workflow_steps SET status = 'SUCCEEDED' WHERE id = ?", (commerce_step,))
            connection.commit()
            assert connection.exec_driver_sql(
                "SELECT c.status, w.status FROM commerce_workflow_steps c "
                "JOIN workflow_steps w ON w.id = c.workflow_step_id WHERE c.workflow_step_id = ?",
                (commerce_step,),
            ).one() == ("SUCCEEDED", "SUCCEEDED")
    finally:
        migration_engine.dispose()
    counts_0012, triggers_0012, indexes_0012 = sqlite_state(True)
    assert counts_0012["workflow_runs"] == 2 and counts_0012["workflow_steps"] == 2
    assert {
        "trg_commerce_workflow_step_scope_insert",
        "trg_commerce_workflow_step_scope_update",
        "trg_workflow_steps_sync_commerce_status",
        "trg_commerce_workflow_link_delete",
        "trg_commerce_workflow_link_scope_insert",
        "trg_workflow_runs_commerce_link_scope",
        "trg_workflow_steps_commerce_identity_guard",
    }.issubset(triggers_0012)
    assert {
        "ix_commerce_workflow_links_story_run_id",
        "uq_commerce_workflow_step_attempt",
        "uq_active_commerce_workflow_step",
    }.issubset(indexes_0012)

    # 父 StoryRun 的直接删除需要先删除其步骤再删除父 WorkflowRun，不能留下孤儿。
    delete_story, delete_run, delete_step = str(uuid4()), str(uuid4()), str(uuid4())
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
            with connection.begin():
                connection.exec_driver_sql("INSERT INTO story_runs (id, project_id, topic_candidate_id, project_product_selection_id, product_asset_version_id, run_number, mode, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (delete_story, seed_ids["project"], seed_ids["topic"], seed_ids["selection"], seed_ids["product_version"], 2, "STEPWISE", now, now))
                connection.exec_driver_sql("INSERT INTO story_run_states (story_run_id, current_stage, status, stage_data, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)", (delete_story, "TOPIC", "PENDING", "{}", now, now))
                connection.exec_driver_sql("INSERT INTO workflow_runs (id, project_id, workflow_key, status, created_at) VALUES (?, ?, ?, ?, ?)", (delete_run, seed_ids["project"], "commerce_story_run", "PENDING", now))
                connection.exec_driver_sql("INSERT INTO commerce_workflow_links (workflow_run_id, story_run_id, created_at) VALUES (?, ?, ?)", (delete_run, delete_story, now))
                connection.exec_driver_sql("INSERT INTO workflow_steps (id, workflow_run_id, step_key, position, status, progress, attempt, input_payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (delete_step, delete_run, "OUTLINE", 2, "PENDING", 0, 1, "{}", now))
                connection.exec_driver_sql("INSERT INTO commerce_workflow_steps (workflow_step_id, workflow_run_id, story_run_id, stage, attempt, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (delete_step, delete_run, delete_story, "OUTLINE", 1, "PENDING", now))
                connection.exec_driver_sql("DELETE FROM story_runs WHERE id = ?", (delete_story,))
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM workflow_runs WHERE id = ?", (delete_run,)).scalar_one() == 0
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM workflow_steps WHERE id = ?", (delete_step,)).scalar_one() == 0
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    finally:
        migration_engine.dispose()

    migrate("0012_commerce_workflow_orchestration", downgrade=True)
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.connect() as connection:
            triggers_down_to_0012 = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger'")}
            tables_down_to_0012 = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
            assert "commerce_chapter_attempt_chapters" not in tables_down_to_0012
            assert "trg_commerce_workflow_link_scope_insert" not in triggers_down_to_0012
            assert "trg_commerce_workflow_step_scope_insert" in triggers_down_to_0012
    finally:
        migration_engine.dispose()
    migrate("0013_commerce_phase2_integrity_fixes")
    migrate("0011_commerce_domain_integrity_fixes", downgrade=True)
    counts_down, triggers_down, indexes_down = sqlite_state(False)
    assert counts_down["story_runs"] == 1 and not triggers_down.intersection(triggers_0012)
    assert "uq_commerce_workflow_step_attempt" not in indexes_down
    assert counts_down["workflow_runs"] == 1 and counts_down["workflow_steps"] == 1

    migrate("0013_commerce_phase2_integrity_fixes")
    counts_up_again, triggers_up_again, indexes_up_again = sqlite_state(True)
    assert counts_up_again["story_runs"] == 1 and counts_up_again["workflow_runs"] == 1
    assert triggers_0012 == triggers_up_again
    assert {
        "uq_commerce_workflow_step_attempt",
        "uq_active_commerce_workflow_step",
    }.issubset(indexes_up_again)


def test_0014_casts_workflow_step_enum_to_text_before_comparing_legacy_sidecar_status():
    """PostgreSQL 不能直接比较 runstatus 枚举和 0012 的 VARCHAR 状态列。"""

    server_root = Path(__file__).resolve().parents[1]
    path = server_root / "migrations" / "versions" / "0014_commerce_phase2_legacy_compatibility.py"
    spec = importlib.util.spec_from_file_location("lemonflow_migration_0014", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    metadata = MetaData()
    workflow_steps = Table(
        "workflow_steps",
        metadata,
        Column("status", Enum("PENDING", "SUCCEEDED", name="runstatus"), nullable=False),
    )
    commerce_steps = Table(
        "commerce_workflow_steps",
        metadata,
        Column("status", String(20), nullable=False),
    )
    expression = migration._workflow_step_status_mismatch(workflow_steps, commerce_steps)
    postgresql_sql = str(expression.compile(dialect=postgresql.dialect()))
    sqlite_sql = str(expression.compile(dialect=sqlite.dialect()))

    assert "CAST(workflow_steps.status AS TEXT)" in postgresql_sql
    assert "commerce_workflow_steps.status" in postgresql_sql
    assert "CAST(workflow_steps.status AS TEXT)" in sqlite_sql


def test_0019_repairs_postgresql_scope_guard_without_relaxing_its_predicates(tmp_path):
    """0019 只能转换枚举侧，且 PostgreSQL 不允许回装已知坏函数。"""

    server_root = Path(__file__).resolve().parents[1]
    path = server_root / "migrations" / "versions" / "0019_commerce_step_scope_guard_enum_compatibility.py"
    spec = importlib.util.spec_from_file_location("lemonflow_migration_0019", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    function_sql = migration.POSTGRES_SCOPE_GUARD_FUNCTION_SQL
    assert "CREATE OR REPLACE FUNCTION commerce_workflow_step_scope_guard() RETURNS trigger" in function_sql
    assert "CAST(step.status AS TEXT) = NEW.status" in function_sql
    assert "NEW.status::runstatus" not in function_sql
    # 所有既有 Commerce 作用域谓词和异常语义都必须原样存在。
    for predicate in (
        "link.workflow_run_id = NEW.workflow_run_id",
        "link.story_run_id = NEW.story_run_id",
        "run.workflow_key = 'commerce_story_run'",
        "run.project_id = story.project_id",
        "step.workflow_run_id = NEW.workflow_run_id",
        "step.step_key = NEW.stage",
        "step.attempt = NEW.attempt",
        "RAISE EXCEPTION 'commerce workflow step scope invalid'",
    ):
        assert predicate in function_sql

    database_url = f"sqlite:///{tmp_path / 'commerce-0019-roundtrip.db'}"
    config = Config(str(server_root / "alembic.ini"))
    config.set_main_option("script_location", str(server_root / "migrations"))
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
            config.attributes["connection"] = connection
            command.upgrade(config, "0018_commerce_storyrun_production_slice2")
            command.upgrade(config, "0019_commerce_step_scope_guard_enum_compatibility")
            assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == (
                "0019_commerce_step_scope_guard_enum_compatibility"
            )
            # ``SELECT`` starts SQLAlchemy's implicit SQLite transaction; the
            # migration environment intentionally refuses to toggle foreign
            # keys while it is still open.
            connection.commit()
            command.downgrade(config, "0018_commerce_storyrun_production_slice2")
            assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == (
                "0018_commerce_storyrun_production_slice2"
            )
            config.attributes.pop("connection", None)
    finally:
        migration_engine.dispose()


def test_0014_backfills_published_phase2_data_and_rejects_unrepairable_sidecars(tmp_path):
    """0014 为非空 0012/0013 数据补齐 attempt 与审核语义，且拒绝坏 sidecar。"""

    database_url = f"sqlite:///{tmp_path / 'commerce-0014-compatibility.db'}"
    server_root = Path(__file__).resolve().parents[1]
    now = datetime.now(timezone.utc)
    ids = {name: str(uuid4()) for name in (
        "project", "legacy_run", "legacy_step", "topic", "product", "analysis", "product_version", "selection",
        "story_run", "outline", "chapter", "segment", "commerce_run", "chapters_step",
        "storyboard_step", "segment_placement",
        "prompt_draft_step", "prompt_approved_step", "prompt_rejected_step",
        "prompt_draft", "prompt_approved", "prompt_rejected", "approved_review", "rejected_review",
        "auto_topic", "auto_story_run", "auto_commerce_run", "auto_prompt_step", "auto_prompt",
    )}

    def migrate(revision: str, *, downgrade: bool = False) -> None:
        config = Config(str(server_root / "alembic.ini"))
        config.set_main_option("script_location", str(server_root / "migrations"))
        migration_engine = create_engine(database_url)
        try:
            with migration_engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()
                config.attributes["connection"] = connection
                (command.downgrade if downgrade else command.upgrade)(config, revision)
                config.attributes.pop("connection", None)
        finally:
            migration_engine.dispose()

    migrate("0012_commerce_workflow_orchestration")
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.begin() as connection:
            metadata = MetaData()
            metadata.reflect(bind=connection)
            table = metadata.tables
            connection.execute(table["projects"].insert(), {
                "id": ids["project"], "title": "0014 兼容项目", "description": None,
                "created_at": now, "updated_at": now,
            })
            connection.execute(table["workflow_runs"].insert(), {
                "id": ids["legacy_run"], "project_id": ids["project"], "workflow_key": "legacy-seed",
                "workflow_definition_id": None, "workflow_version": None, "idempotency_key": None,
                "input_snapshot": {}, "status": "SUCCEEDED", "created_at": now,
                "started_at": now, "finished_at": now,
            })
            connection.execute(table["workflow_steps"].insert(), {
                "id": ids["legacy_step"], "workflow_run_id": ids["legacy_run"], "step_key": "LEGACY",
                "position": 1, "status": "SUCCEEDED", "progress": 100, "attempt": 1,
                "input_payload": {}, "output_payload": {}, "error_message": None,
                "model_profile_snapshot": None, "idempotency_key": None, "shot_plan_id": None,
                "video_clip_id": None, "provider_task_id": None, "created_at": now,
                "started_at": now, "finished_at": now,
            })
            connection.execute(table["topic_candidates"].insert(), {
                "id": ids["topic"], "project_id": ids["project"], "generation_run_id": ids["legacy_run"],
                "position": 1, "title": "选题", "opening_hook": "开头", "synopsis": "摘要",
                "score": None, "scoring_notes": None, "status": "DRAFT", "created_at": now, "updated_at": now,
            })
            connection.execute(table["product_assets"].insert(), {
                "id": ids["product"], "name": "产品", "description": None, "created_at": now, "updated_at": now,
            })
            connection.execute(table["product_analysis_versions"].insert(), {
                "id": ids["analysis"], "product_asset_id": ids["product"], "source_media_asset_id": None,
                "version": 1, "raw_analysis": {}, "analysis_status": "SUCCEEDED", "created_at": now,
                "updated_at": now, "product_identification": {}, "package_ocr": {},
                "candidate_reference_images": [], "appearance_description_candidates": [],
                "selling_point_candidates": [], "user_pain_point_candidates": [], "usage_scenario_candidates": [],
            })
            connection.execute(table["product_asset_versions"].insert(), {
                "id": ids["product_version"], "product_asset_id": ids["product"],
                "source_analysis_version_id": ids["analysis"], "version": 1, "product_name": "产品 v1",
                "appearance_description": "包装", "selling_points": [], "user_pain_points": [],
                "usage_scenarios": [], "package_ocr": {}, "reference_images": [], "status": "CONFIRMED",
                "frozen_at": now, "created_at": now,
            })
            connection.execute(table["project_product_selections"].insert(), {
                "id": ids["selection"], "project_id": ids["project"], "product_asset_id": ids["product"],
                "product_asset_version_id": ids["product_version"], "selected_at": now, "created_at": now,
            })
            connection.execute(table["story_runs"].insert(), {
                "id": ids["story_run"], "project_id": ids["project"], "topic_candidate_id": ids["topic"],
                "project_product_selection_id": ids["selection"], "product_asset_version_id": ids["product_version"],
                "run_number": 1, "mode": "STEPWISE", "created_at": now, "updated_at": now,
            })
            connection.execute(table["story_run_states"].insert(), {
                "story_run_id": ids["story_run"], "current_stage": "VIDEO_PROMPTS", "status": "PAUSED",
                "stage_data": {}, "created_at": now, "updated_at": now,
            })
            connection.execute(table["topic_candidates"].insert(), {
                "id": ids["auto_topic"], "project_id": ids["project"], "generation_run_id": ids["legacy_run"],
                "position": 2, "title": "AUTO 选题", "opening_hook": "开头", "synopsis": "摘要",
                "score": None, "scoring_notes": None, "status": "DRAFT", "created_at": now, "updated_at": now,
            })
            connection.execute(table["story_runs"].insert(), {
                "id": ids["auto_story_run"], "project_id": ids["project"], "topic_candidate_id": ids["auto_topic"],
                "project_product_selection_id": ids["selection"], "product_asset_version_id": ids["product_version"],
                "run_number": 1, "mode": "AUTO", "created_at": now, "updated_at": now,
            })
            connection.execute(table["story_run_states"].insert(), {
                "story_run_id": ids["auto_story_run"], "current_stage": "SEGMENT_RENDER", "status": "PENDING",
                "stage_data": {}, "created_at": now, "updated_at": now,
            })
            connection.execute(table["story_outline_versions"].insert(), {
                "id": ids["outline"], "story_run_id": ids["story_run"], "version": 1, "title": "大纲",
                "premise": "前提", "story_beats": [], "product_placement_strategy": {}, "status": "LOCKED", "created_at": now,
            })
            connection.execute(table["chapter_plans"].insert(), {
                "id": ids["chapter"], "story_run_id": ids["story_run"], "outline_version_id": ids["outline"],
                "chapter_number": 1, "title": "章节", "narrative_purpose": "目的", "content_summary": "内容",
                "product_plan": {}, "created_at": now, "updated_at": now,
            })
            connection.execute(table["video_segment_plans"].insert(), {
                "id": ids["segment"], "story_run_id": ids["story_run"], "chapter_id": ids["chapter"],
                "segment_number": 1, "target_duration_ms": 4000, "narrative_target": "片段", "status": "DRAFT",
                "video_prompt_version": None, "video_prompt_trace": {}, "created_at": now, "updated_at": now,
            })
            connection.execute(table["workflow_runs"].insert(), {
                "id": ids["commerce_run"], "project_id": ids["project"], "workflow_key": "commerce_story_run",
                "workflow_definition_id": None, "workflow_version": "LemonFlow_Commerce_V1", "idempotency_key": "0014-compat",
                "input_snapshot": {}, "status": "PENDING", "created_at": now, "started_at": None, "finished_at": None,
            })
            connection.execute(table["commerce_workflow_links"].insert(), {
                "workflow_run_id": ids["commerce_run"], "story_run_id": ids["story_run"], "created_at": now,
            })
            connection.execute(table["workflow_runs"].insert(), {
                "id": ids["auto_commerce_run"], "project_id": ids["project"], "workflow_key": "commerce_story_run",
                "workflow_definition_id": None, "workflow_version": "LemonFlow_Commerce_V1", "idempotency_key": "0014-auto",
                "input_snapshot": {}, "status": "PENDING", "created_at": now, "started_at": None, "finished_at": None,
            })
            connection.execute(table["commerce_workflow_links"].insert(), {
                "workflow_run_id": ids["auto_commerce_run"], "story_run_id": ids["auto_story_run"], "created_at": now,
            })
            for step_id, stage, position, attempt, output in (
                (
                    ids["chapters_step"], "CHAPTERS", 3, 1,
                    {"artifact_references": {"chapter_ids": [ids["chapter"]], "outline_id": ids["outline"]}},
                ),
                (
                    ids["storyboard_step"], "STORYBOARD", 4, 1,
                    {"artifact_references": {"video_segment_ids": [ids["segment"]]}},
                ),
                (ids["prompt_draft_step"], "VIDEO_PROMPTS", 6, 1, {"artifact_references": {}}),
                (ids["prompt_approved_step"], "VIDEO_PROMPTS", 6, 2, {"artifact_references": {}}),
                (ids["prompt_rejected_step"], "VIDEO_PROMPTS", 6, 3, {"artifact_references": {}}),
            ):
                connection.execute(table["workflow_steps"].insert(), {
                    "id": step_id, "workflow_run_id": ids["commerce_run"], "step_key": stage,
                    "position": position, "status": "SUCCEEDED", "progress": 100, "attempt": attempt,
                    "input_payload": {}, "output_payload": output, "error_message": None,
                    "model_profile_snapshot": {}, "idempotency_key": f"{step_id}-key", "shot_plan_id": None,
                    "video_clip_id": None, "provider_task_id": None, "created_at": now,
                    "started_at": now, "finished_at": now,
                })
                connection.execute(table["commerce_workflow_steps"].insert(), {
                    "workflow_step_id": step_id, "workflow_run_id": ids["commerce_run"],
                    "story_run_id": ids["story_run"], "stage": stage, "attempt": attempt,
                    "status": "SUCCEEDED", "created_at": now,
                })
            connection.execute(table["workflow_steps"].insert(), {
                "id": ids["auto_prompt_step"], "workflow_run_id": ids["auto_commerce_run"],
                "step_key": "VIDEO_PROMPTS", "position": 6, "status": "SUCCEEDED", "progress": 100,
                "attempt": 1, "input_payload": {}, "output_payload": {"artifact_references": {}},
                "error_message": None, "model_profile_snapshot": {}, "idempotency_key": "0014-auto-step",
                "shot_plan_id": None, "video_clip_id": None, "provider_task_id": None,
                "created_at": now, "started_at": now, "finished_at": now,
            })
            connection.execute(table["commerce_workflow_steps"].insert(), {
                "workflow_step_id": ids["auto_prompt_step"], "workflow_run_id": ids["auto_commerce_run"],
                "story_run_id": ids["auto_story_run"], "stage": "VIDEO_PROMPTS", "attempt": 1,
                "status": "SUCCEEDED", "created_at": now,
            })
            connection.execute(table["product_placement_plans"].insert(), {
                "id": ids["segment_placement"], "story_run_id": ids["story_run"],
                "product_asset_version_id": ids["product_version"], "chapter_id": None,
                "video_segment_id": ids["segment"], "sub_shot_id": None,
                "placement_method": "SOFT_PROP", "placement_strength": "LIGHT",
                "pain_point_trigger": "痛点", "product_action": "动作", "ad_entry_point": "进入",
                "story_recovery_point": "恢复", "planned_duration_ms": 1000,
                "created_at": now, "updated_at": now,
            })
            for prompt_id, step_id, version in (
                (ids["prompt_draft"], ids["prompt_draft_step"], 1),
                (ids["prompt_approved"], ids["prompt_approved_step"], 2),
                (ids["prompt_rejected"], ids["prompt_rejected_step"], 3),
            ):
                connection.execute(table["video_prompt_versions"].insert(), {
                    "id": prompt_id, "video_segment_id": ids["segment"], "workflow_step_id": step_id,
                    "version": version, "prompt": f"提示词 {version}", "trace": {}, "status": "LOCKED",
                    "created_at": now, "locked_at": now,
                })
            for review_id, step_id, decision in (
                (ids["approved_review"], ids["prompt_approved_step"], "APPROVED"),
                (ids["rejected_review"], ids["prompt_rejected_step"], "REJECTED"),
            ):
                connection.execute(table["review_decisions"].insert(), {
                    "id": review_id, "project_id": ids["project"], "target_type": "COMMERCE_STAGE_VIDEO_PROMPTS",
                    "target_id": step_id, "decision": decision, "reviewer_label": "兼容审核", "note": None,
                    "quality_score": 8, "created_at": now,
                })
            connection.execute(table["video_prompt_versions"].insert(), {
                "id": ids["auto_prompt"], "video_segment_id": ids["segment"],
                "workflow_step_id": ids["auto_prompt_step"], "version": 4, "prompt": "AUTO 锁定提示词",
                "trace": {}, "status": "LOCKED", "created_at": now, "locked_at": now,
            })
    finally:
        migration_engine.dispose()

    migrate("0013_commerce_phase2_integrity_fixes")
    migrate("0014_commerce_phase2_legacy_compatibility")
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT workflow_step_id, chapter_plan_id, position FROM commerce_chapter_attempt_chapters"
            ).one() == (ids["chapters_step"], ids["chapter"], 1)
            prompt_statuses = dict(connection.exec_driver_sql(
                "SELECT id, status FROM video_prompt_versions"
            ).fetchall())
            assert prompt_statuses == {
                ids["prompt_draft"]: "DRAFT",
                ids["prompt_approved"]: "LOCKED",
                ids["prompt_rejected"]: "REJECTED",
                ids["auto_prompt"]: "LOCKED",
            }
            legacy_board = connection.exec_driver_sql(
                "SELECT output_payload FROM workflow_steps WHERE id = ?", (ids["storyboard_step"],)
            ).scalar_one()
            if isinstance(legacy_board, str):
                legacy_board = json.loads(legacy_board)
            assert legacy_board["artifact_references"]["chapter_ids"] == [ids["chapter"]]
            assert legacy_board["artifact_references"]["product_placement_ids"] == [ids["segment_placement"]]
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    finally:
        migration_engine.dispose()

    # 0014 -> 0013 keeps the exact schema, then 0013 -> 0012 drops only the
    # association metadata.  Upgrading head again deterministically rebuilds it.
    migrate("0013_commerce_phase2_integrity_fixes", downgrade=True)
    migrate("0014_commerce_phase2_legacy_compatibility")
    migrate("0012_commerce_workflow_orchestration", downgrade=True)
    migrate("0014_commerce_phase2_legacy_compatibility")
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT COUNT(*) FROM commerce_chapter_attempt_chapters WHERE workflow_step_id = ?",
                (ids["chapters_step"],),
            ).scalar_one() == 1
            # Simulate an old 0012/0013 database whose historical DML bypassed
            # SQLite triggers.  0014 must fail rather than blessing the bad row.
            connection.exec_driver_sql("DROP TRIGGER trg_commerce_workflow_step_scope_update")
            connection.exec_driver_sql(
                "UPDATE commerce_workflow_steps SET attempt = 99 WHERE workflow_step_id = ?",
                (ids["prompt_draft_step"],),
            )
            connection.commit()
    finally:
        migration_engine.dispose()
    migrate("0013_commerce_phase2_integrity_fixes", downgrade=True)
    with pytest.raises(RuntimeError, match="sidecar parent/stage/attempt/status scope"):
        migrate("0014_commerce_phase2_legacy_compatibility")


def _sqlite_schema_fingerprint(database_url: str) -> tuple[tuple, ...]:
    """返回可比较的 SQLite schema 指纹；不包含 alembic 版本和业务行。"""

    migration_engine = create_engine(database_url)
    try:
        with migration_engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
            tables = sorted(
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
                )
            )
            table_entries = []
            for table_name in tables:
                columns = tuple(
                    (row[1], row[2], row[3], row[4], row[5])
                    for row in connection.exec_driver_sql(f"PRAGMA table_info('{table_name}')").fetchall()
                )
                foreign_keys = tuple(sorted(
                    (row[2], row[3], row[4], row[5], row[6], row[7])
                    for row in connection.exec_driver_sql(f"PRAGMA foreign_key_list('{table_name}')").fetchall()
                ))
                indexes = []
                for index in sorted(
                    connection.exec_driver_sql(f"PRAGMA index_list('{table_name}')").fetchall(), key=lambda row: row[1]
                ):
                    index_name = index[1]
                    index_columns = tuple(
                        (row[1], row[2])
                        for row in connection.exec_driver_sql(f"PRAGMA index_info('{index_name}')").fetchall()
                    )
                    index_sql = connection.exec_driver_sql(
                        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (index_name,)
                    ).scalar_one_or_none()
                    indexes.append((index_name, index[2], index[3], index[4], index_columns, index_sql))
                table_entries.append((table_name, columns, foreign_keys, tuple(indexes)))
            triggers = tuple(
                connection.exec_driver_sql(
                    "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
                ).fetchall()
            )
            temporary = tuple(
                connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp_%'").fetchall()
            )
            return tuple(table_entries) + (("__triggers__", triggers), ("__temporary__", temporary))
    finally:
        migration_engine.dispose()


def test_0013_downgrade_schema_matches_fresh_0012_and_reupgrades(tmp_path):
    """0013 downgrade 必须精确恢复已发布 0012 的表、索引、外键和 trigger。"""

    server_root = Path(__file__).resolve().parents[1]
    fresh_url = f"sqlite:///{tmp_path / 'fresh-0012.db'}"
    round_trip_url = f"sqlite:///{tmp_path / 'round-trip-0012.db'}"

    def migrate(database_url: str, revision: str, *, downgrade: bool = False) -> None:
        config = Config(str(server_root / "alembic.ini"))
        config.set_main_option("script_location", str(server_root / "migrations"))
        migration_engine = create_engine(database_url)
        try:
            with migration_engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()
                config.attributes["connection"] = connection
                (command.downgrade if downgrade else command.upgrade)(config, revision)
                config.attributes.pop("connection", None)
        finally:
            migration_engine.dispose()

    migrate(fresh_url, "0012_commerce_workflow_orchestration")
    migrate(round_trip_url, "0012_commerce_workflow_orchestration")
    fingerprint_0012 = _sqlite_schema_fingerprint(fresh_url)
    migrate(round_trip_url, "0013_commerce_phase2_integrity_fixes")
    migrate(round_trip_url, "0012_commerce_workflow_orchestration", downgrade=True)
    assert _sqlite_schema_fingerprint(round_trip_url) == fingerprint_0012
    migrate(round_trip_url, "0013_commerce_phase2_integrity_fixes")
    migration_engine = create_engine(round_trip_url)
    try:
        with migration_engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='commerce_chapter_attempt_chapters'"
            ).scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    finally:
        migration_engine.dispose()


def test_0012_downgrade_schema_matches_fresh_0011_and_reupgrades(tmp_path):
    """非空 0012 降级不得保留任何 0012 schema 痕迹。"""

    server_root = Path(__file__).resolve().parents[1]
    fresh_url = f"sqlite:///{tmp_path / 'fresh-0011.db'}"
    round_trip_url = f"sqlite:///{tmp_path / 'round-trip-0011.db'}"

    def migrate(database_url: str, revision: str, *, downgrade: bool = False) -> None:
        config = Config(str(server_root / "alembic.ini"))
        config.set_main_option("script_location", str(server_root / "migrations"))
        migration_engine = create_engine(database_url)
        try:
            with migration_engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()
                config.attributes["connection"] = connection
                (command.downgrade if downgrade else command.upgrade)(config, revision)
                config.attributes.pop("connection", None)
        finally:
            migration_engine.dispose()

    migrate(fresh_url, "0011_commerce_domain_integrity_fixes")
    migrate(round_trip_url, "0011_commerce_domain_integrity_fixes")
    fresh_fingerprint = _sqlite_schema_fingerprint(fresh_url)
    migrate(round_trip_url, "0012_commerce_workflow_orchestration")
    migrate(round_trip_url, "0011_commerce_domain_integrity_fixes", downgrade=True)
    assert _sqlite_schema_fingerprint(round_trip_url) == fresh_fingerprint

    migration_engine = create_engine(round_trip_url)
    try:
        with migration_engine.connect() as connection:
            columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info('workflow_runs')")}
            tables = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")}
            triggers = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
            assert "story_run_id" not in columns
            assert not {"commerce_workflow_links", "commerce_workflow_steps", "video_prompt_versions"}.intersection(tables)
            assert not any("commerce_workflow" in name for name in triggers)
    finally:
        migration_engine.dispose()
    migrate(round_trip_url, "0012_commerce_workflow_orchestration")
    migration_engine = create_engine(round_trip_url)
    try:
        with migration_engine.connect() as connection:
            tables = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")}
            assert {"commerce_workflow_links", "commerce_workflow_steps", "video_prompt_versions"}.issubset(tables)
    finally:
        migration_engine.dispose()


def test_0012_keeps_v1_parallel_video_steps_outside_commerce_indexes(tmp_path):
    """V1 的同一父运行并行 VIDEO_SHOT、attempt=0 必须在往返迁移后保持原样。"""

    database_url = f"sqlite:///{tmp_path / 'v1-parallel-round-trip.db'}"
    server_root = Path(__file__).resolve().parents[1]

    def migrate(revision: str, *, downgrade: bool = False) -> None:
        config = Config(str(server_root / "alembic.ini"))
        config.set_main_option("script_location", str(server_root / "migrations"))
        migration_engine = create_engine(database_url)
        try:
            with migration_engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()
                config.attributes["connection"] = connection
                (command.downgrade if downgrade else command.upgrade)(config, revision)
                config.attributes.pop("connection", None)
        finally:
            migration_engine.dispose()

    migrate("0011_commerce_domain_integrity_fixes")
    project_id, v1_run_id = str(uuid4()), str(uuid4())
    v1_steps = [str(uuid4()), str(uuid4()), str(uuid4())]
    now = datetime.now(timezone.utc)
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO projects (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (project_id, "V1 并行镜头", now, now),
            )
            connection.exec_driver_sql(
                "INSERT INTO workflow_runs (id, project_id, workflow_key, status, created_at) VALUES (?, ?, ?, ?, ?)",
                (v1_run_id, project_id, "v1_video_generation", "RUNNING", now),
            )
            for position, step_id in enumerate(v1_steps, start=1):
                connection.exec_driver_sql(
                    "INSERT INTO workflow_steps (id, workflow_run_id, step_key, position, status, progress, attempt, input_payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (step_id, v1_run_id, "VIDEO_SHOT", position, "PENDING", 0, 0, "{}", now),
                )
    finally:
        migration_engine.dispose()
    migrate("0012_commerce_workflow_orchestration")
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.begin() as connection:
            stored = connection.exec_driver_sql(
                "SELECT position, attempt, status FROM workflow_steps WHERE workflow_run_id = ? ORDER BY position", (v1_run_id,)
            ).fetchall()
            assert stored == [(1, 0, "PENDING"), (2, 0, "PENDING"), (3, 0, "PENDING")]
            # 0012 下继续写入同一父运行的并行活动 V1 子任务仍合法。
            fourth = str(uuid4())
            connection.exec_driver_sql(
                "INSERT INTO workflow_steps (id, workflow_run_id, step_key, position, status, progress, attempt, input_payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (fourth, v1_run_id, "VIDEO_SHOT", 4, "RUNNING", 0, 0, "{}", now),
            )
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM commerce_workflow_steps").scalar_one() == 0
    finally:
        migration_engine.dispose()
    migrate("0011_commerce_domain_integrity_fixes", downgrade=True)
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT COUNT(*) FROM workflow_steps WHERE workflow_run_id = ?", (v1_run_id,)
            ).scalar_one() == 4
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    finally:
        migration_engine.dispose()
