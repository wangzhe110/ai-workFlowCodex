"""Commerce 已选创意重跑：0021 约束、事务和 StoryRun 作用域回归。"""

from __future__ import annotations

from base64 import b64decode
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
from subprocess import run
import sys
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal
from app.main import app
from app.models import (
    CommerceCharacterDesignVersion,
    CommerceCharacterReferenceImage,
    CommerceCreativeIdea,
    CommerceSceneDesignVersion,
    CommerceSceneReferenceImage,
    CommerceShotKeyframeVersion,
    CommerceStoryboardVersion,
    CommerceStoryRunInput,
    CommerceVideoClipVersion,
    CommerceVideoPromptVersion,
    CommerceWorkflowLink,
    ModelInvocation,
    OutlineVersionStatus,
    RunStatus,
    StoryOutlineVersion,
    StoryRun,
    WorkflowRun,
    WorkflowStep,
)
from app.services import commerce_workflow_service
from app.services import commerce_production_service
from app.services.commerce_production_service import (
    _current_storyboard,
    _latest_locked_image,
    _mark_stale,
    create_production_run,
    lock_character_design,
    lock_image,
    lock_scene_design,
    lock_storyboard,
)
from app.services.commerce_workflow_service import cancel_story_run, rerun_story_run
from app.services.commerce_workflow_preset_service import (
    activate_preset_version,
    copy_preset_draft,
    get_preset_definition,
    publish_preset_draft,
    update_preset_draft,
)


def _video() -> bytes:
    return b64decode((Path(__file__).parent / "fixtures" / "real-video.mp4.base64").read_text(encoding="ascii"))


def _source_story_run(client: TestClient) -> tuple[str, str]:
    """经过 Slice 1 正式入口创建一条可重跑的已选创意来源。"""

    project_id = client.post("/api/v1/projects", json={"title": f"Rerun source {uuid4().hex[:8]}"}).json()["id"]
    uploaded = client.post(
        f"/api/v1/projects/{project_id}/source-video",
        files={"file": ("reference.mp4", _video(), "video/mp4")},
    )
    assert uploaded.status_code == 201, uploaded.text
    analysis_run = client.post(
        f"/api/v1/production/projects/{project_id}/generation-runs/reference_analysis",
        json={"source_asset_id": uploaded.json()["id"]},
    )
    assert analysis_run.status_code == 202, analysis_run.text
    analysis = client.get(f"/api/v1/production/projects/{project_id}/reference-analyses").json()[0]
    assert client.post(f"/api/v1/production/reference-analyses/{analysis['id']}/lock", json={}).status_code == 200
    intake = client.get(f"/api/v1/production/projects/{project_id}/commerce-reference-intakes").json()[0]
    confirmed = client.post(
        f"/api/v1/production/commerce-reference-intakes/{intake['id']}/confirm-product",
        json={
            "product_name": "可重跑测试商品",
            "appearance_description": "白色包装",
            "selling_points": [{"claim": "已冻结卖点"}],
            "usage_scenarios": [{"scene": "家庭桌面"}],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    generated = client.post(
        f"/api/v1/production/projects/{project_id}/generation-runs/commerce_creative_generation", json={}
    )
    assert generated.status_code == 202, generated.text
    batch = client.get(f"/api/v1/production/projects/{project_id}/commerce-creative-batches").json()[0]
    selected = client.post(
        f"/api/v1/production/commerce-creative-ideas/{batch['ideas'][0]['id']}/select",
        json={"reviewer_label": "重跑测试"},
    )
    assert selected.status_code == 201, selected.text
    return project_id, selected.json()["story_run_id"]


def _source_snapshot(db, story_run_id: str) -> dict:
    """比较来源运行不能改变的实体和下游资产，不读取媒体内容。"""

    source = db.get(StoryRun, story_run_id)
    assert source is not None
    source_input = db.get(CommerceStoryRunInput, story_run_id)
    assert source_input is not None
    models = (
        CommerceCharacterDesignVersion,
        CommerceSceneDesignVersion,
        CommerceStoryboardVersion,
        CommerceCharacterReferenceImage,
        CommerceSceneReferenceImage,
        CommerceShotKeyframeVersion,
        CommerceVideoPromptVersion,
        CommerceVideoClipVersion,
    )
    return {
        "story": (source.id, source.run_number, source.state.current_stage.value, source.state.status.value),
        "input": (source_input.creative_idea_id, source_input.run_number, source_input.input_snapshot),
        "idea_status": db.get(CommerceCreativeIdea, source_input.creative_idea_id).status.value,
        "assets": {
            model.__tablename__: [
                (item.id, item.status, getattr(item, "stale_at", None), getattr(item, "locked_at", None))
                for item in db.scalars(select(model).where(model.story_run_id == story_run_id)).all()
            ]
            for model in models
        },
        "workflow": [
            (item.id, item.status.value, tuple(step.id for step in item.steps))
            for item in db.scalars(
                select(WorkflowRun).join(CommerceWorkflowLink).where(CommerceWorkflowLink.story_run_id == story_run_id)
            ).all()
        ],
    }


def test_rerun_api_creates_independent_run_and_preserves_source_without_dispatch() -> None:
    with TestClient(app) as client:
        project_id, source_id = _source_story_run(client)
        db = SessionLocal()
        try:
            before = _source_snapshot(db, source_id)
            before_invocations = db.scalar(select(func.count(ModelInvocation.id)).where(ModelInvocation.project_id == project_id))
        finally:
            db.close()

        response = client.post(f"/api/v1/commerce/story-runs/{source_id}/rerun")
        assert response.status_code == 201, response.text
        result = response.json()
        assert result["source_story_run_id"] == source_id
        assert result["creative_idea_id"] == before["input"][0]
        assert result["run_number"] == 2
        assert result["current_stage"] == "TOPIC"
        assert result["current_status"] == "PENDING"

        db = SessionLocal()
        try:
            rerun = db.get(StoryRun, result["id"])
            rerun_input = db.get(CommerceStoryRunInput, result["id"])
            assert rerun is not None and rerun_input is not None
            assert rerun.project_id == project_id
            assert rerun_input.creative_idea_id == before["input"][0]
            assert rerun_input.run_number == rerun.run_number == 2
            assert rerun_input.input_snapshot["rerun"]["source_story_run_id"] == source_id
            assert rerun_input.input_snapshot["creative_idea"] == before["input"][2]["creative_idea"]
            parent = db.scalar(
                select(WorkflowRun).join(CommerceWorkflowLink).where(CommerceWorkflowLink.story_run_id == rerun.id)
            )
            assert parent is not None and parent.workflow_key == "commerce_story_run"
            assert result["workflow_run_id"] == parent.id
            assert db.scalar(select(func.count(WorkflowStep.id)).where(WorkflowStep.workflow_run_id == parent.id)) == 0
            assert db.scalar(select(func.count(ModelInvocation.id)).where(ModelInvocation.workflow_run_id == parent.id)) == 0
            for model in (
                CommerceCharacterDesignVersion, CommerceSceneDesignVersion, CommerceStoryboardVersion,
                CommerceCharacterReferenceImage, CommerceSceneReferenceImage, CommerceShotKeyframeVersion,
                CommerceVideoPromptVersion, CommerceVideoClipVersion,
            ):
                assert db.scalar(select(func.count(model.id)).where(model.story_run_id == rerun.id)) == 0
            assert _source_snapshot(db, source_id) == before
            assert db.scalar(select(func.count(ModelInvocation.id)).where(ModelInvocation.project_id == project_id)) == before_invocations
        finally:
            db.close()

        second = client.post(f"/api/v1/commerce/story-runs/{source_id}/rerun")
        assert second.status_code == 201, second.text
        assert second.json()["run_number"] == 3


def test_rerun_copies_frozen_config_by_default_and_can_explicitly_use_current_preset() -> None:
    """默认重跑可复现；显式 current 才读取后来激活的预设版本。"""

    with TestClient(app) as client:
        _project_id, source_id = _source_story_run(client)
        db = SessionLocal()
        try:
            source = db.get(StoryRun, source_id)
            assert source is not None and source.workflow_config_freeze is not None
            source_config = source.workflow_config_freeze
            original_preset_id = source_config.preset_version_id
            assert isinstance(original_preset_id, str)
            definition = get_preset_definition(db, "standard")
            assert definition.active_version_id == original_preset_id
            draft = copy_preset_draft(db, preset_key="standard", source_version_id=original_preset_id)
            current_config = deepcopy(draft.config)
            current_config["target_duration_seconds"] = 45
            update_preset_draft(
                db,
                version_id=draft.id,
                config=current_config,
                change_summary="rerun current preset fixture",
            )
            publish_preset_draft(db, version_id=draft.id)
            activate_preset_version(db, preset_key="standard", version_id=draft.id)
        finally:
            db.close()

        try:
            copied = client.post(f"/api/v1/commerce/story-runs/{source_id}/rerun")
            assert copied.status_code == 201, copied.text
            current = client.post(
                f"/api/v1/commerce/story-runs/{source_id}/rerun",
                json={"use_current_preset": True, "preset_key": "standard"},
            )
            assert current.status_code == 201, current.text
            db = SessionLocal()
            try:
                copied_run = db.get(StoryRun, copied.json()["id"])
                current_run = db.get(StoryRun, current.json()["id"])
                assert copied_run is not None and copied_run.workflow_config_freeze is not None
                assert current_run is not None and current_run.workflow_config_freeze is not None
                assert copied_run.workflow_config_freeze.preset_version_id == original_preset_id
                assert copied_run.workflow_config_freeze.effective_workflow_config == source_config.effective_workflow_config
                assert current_run.workflow_config_freeze.preset_version_id == draft.id
                assert current_run.workflow_config_freeze.effective_workflow_config["target_duration_seconds"] == 45
            finally:
                db.close()
        finally:
            db = SessionLocal()
            try:
                activate_preset_version(db, preset_key="standard", version_id=original_preset_id)
            finally:
                db.close()


def test_rerun_api_rejects_missing_or_incomplete_source_and_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        missing = client.post("/api/v1/commerce/story-runs/00000000-0000-0000-0000-000000000000/rerun")
        assert missing.status_code == 404

        _project_id, source_id = _source_story_run(client)
        db = SessionLocal()
        try:
            source_input = db.get(CommerceStoryRunInput, source_id)
            assert source_input is not None
            original = source_input.input_snapshot
            source_input.input_snapshot = {}
            db.commit()
        finally:
            db.close()
        invalid = client.post(f"/api/v1/commerce/story-runs/{source_id}/rerun")
        assert invalid.status_code == 409

        db = SessionLocal()
        try:
            source_input = db.get(CommerceStoryRunInput, source_id)
            assert source_input is not None
            source_input.input_snapshot = original
            db.commit()
        finally:
            db.close()

        monkeypatch.setattr(
            commerce_workflow_service,
            "_ensure_commerce_workflow",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fixture parent failure")),
        )
        failed = client.post(f"/api/v1/commerce/story-runs/{source_id}/rerun")
        assert failed.status_code == 503
        db = SessionLocal()
        try:
            assert db.scalar(select(func.count(StoryRun.id)).where(StoryRun.project_id == _project_id)) == 1
            assert db.scalar(
                select(func.count(CommerceStoryRunInput.story_run_id))
                .join(StoryRun, StoryRun.id == CommerceStoryRunInput.story_run_id)
                .where(StoryRun.project_id == _project_id, CommerceStoryRunInput.story_run_id != source_id)
            ) == 0
        finally:
            db.close()


def test_concurrent_reruns_allocate_unique_numbers_and_one_parent_each() -> None:
    with TestClient(app) as client:
        _project_id, source_id = _source_story_run(client)

    barrier = Barrier(2)

    def create_one() -> tuple[str, int, str]:
        db = SessionLocal()
        try:
            barrier.wait(timeout=5)
            run, parent = rerun_story_run(db, source_story_run_id=source_id)
            return run.id, run.run_number, parent.id
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        created = list(pool.map(lambda _value: create_one(), range(2)))
    assert sorted(item[1] for item in created) == [2, 3]
    assert len({item[0] for item in created}) == len({item[2] for item in created}) == 2

    db = SessionLocal()
    try:
        inputs = list(
            db.scalars(
                select(CommerceStoryRunInput)
                .where(CommerceStoryRunInput.creative_idea_id == db.get(CommerceStoryRunInput, source_id).creative_idea_id)
                .order_by(CommerceStoryRunInput.run_number)
            ).all()
        )
        assert [item.run_number for item in inputs] == [1, 2, 3]
        assert len({item.story_run_id for item in inputs}) == 3
        for item in inputs[1:]:
            parent = db.scalar(
                select(WorkflowRun).join(CommerceWorkflowLink).where(CommerceWorkflowLink.story_run_id == item.story_run_id)
            )
            assert parent is not None
            assert db.scalar(select(func.count(WorkflowStep.id)).where(WorkflowStep.workflow_run_id == parent.id)) == 0
    finally:
        db.close()


def _seed_locked_scope_assets(db, *, story_run: StoryRun, marker: str) -> dict[str, str]:
    """仅建本地结构化资产，用来验证两个 Run 的服务层查询绝不串线。

    不创建 WorkflowStep、ModelInvocation 或任何媒体生成任务；图片 URL 只是锁定行为
    的最小元数据，测试不会下载或访问它们。
    """

    outline = db.scalars(
        select(StoryOutlineVersion)
        .where(
            StoryOutlineVersion.story_run_id == story_run.id,
            StoryOutlineVersion.status == OutlineVersionStatus.LOCKED,
        )
        .order_by(StoryOutlineVersion.version.desc())
    ).first()
    if outline is None:
        outline = StoryOutlineVersion(
            story_run_id=story_run.id,
            version=int(
                db.scalar(
                    select(func.max(StoryOutlineVersion.version)).where(
                        StoryOutlineVersion.story_run_id == story_run.id
                    )
                )
                or 0
            )
            + 1,
            title=f"{marker} 大纲",
            premise="用于 StoryRun 作用域回归的本地大纲",
            story_beats=[],
            product_placement_strategy={},
            status=OutlineVersionStatus.LOCKED,
        )
        db.add(outline)
        db.flush()

    character = CommerceCharacterDesignVersion(
        story_run_id=story_run.id,
        source_outline_version_id=outline.id,
        source_product_asset_version_id=story_run.product_asset_version_id,
        version=1,
        status="READY",
        content={"roles": [{"role_id": "role-1", "name": marker}]},
    )
    db.add(character)
    db.flush()
    lock_character_design(
        db, story_run_id=story_run.id, version_id=character.id, reviewer_label="scope test", note=None
    )

    scene = CommerceSceneDesignVersion(
        story_run_id=story_run.id,
        source_outline_version_id=outline.id,
        character_design_version_id=character.id,
        source_product_asset_version_id=story_run.product_asset_version_id,
        version=1,
        status="READY",
        content={"scenes": [{"scene_id": "scene-1", "name": marker}]},
    )
    db.add(scene)
    db.flush()
    lock_scene_design(
        db, story_run_id=story_run.id, version_id=scene.id, reviewer_label="scope test", note=None
    )

    character_image = CommerceCharacterReferenceImage(
        story_run_id=story_run.id,
        character_design_version_id=character.id,
        role_id="role-1",
        version=1,
        image_url=f"https://example.invalid/{marker}-character.png",
        status="READY",
    )
    scene_image = CommerceSceneReferenceImage(
        story_run_id=story_run.id,
        scene_design_version_id=scene.id,
        scene_id="scene-1",
        version=1,
        image_url=f"https://example.invalid/{marker}-scene.png",
        status="READY",
    )
    db.add_all((character_image, scene_image))
    db.flush()
    lock_image(
        db, story_run_id=story_run.id, image_id=character_image.id, kind="CHARACTER", reviewer_label="scope test", note=None
    )
    lock_image(
        db, story_run_id=story_run.id, image_id=scene_image.id, kind="SCENE", reviewer_label="scope test", note=None
    )

    storyboard = CommerceStoryboardVersion(
        story_run_id=story_run.id,
        source_outline_version_id=outline.id,
        character_design_version_id=character.id,
        scene_design_version_id=scene.id,
        source_product_asset_version_id=story_run.product_asset_version_id,
        version=1,
        status="READY",
        content={"shots": [{"shot_id": "shot-1", "shot_number": 1, "segment_summary": marker}]},
    )
    db.add(storyboard)
    db.flush()
    lock_storyboard(
        db, story_run_id=story_run.id, version_id=storyboard.id, reviewer_label="scope test", note=None
    )

    keyframe = CommerceShotKeyframeVersion(
        story_run_id=story_run.id,
        storyboard_version_id=storyboard.id,
        shot_id="shot-1",
        shot_number=1,
        version=1,
        image_url=f"https://example.invalid/{marker}-keyframe.png",
        input_asset_snapshot={"marker": marker},
        status="LOCKED",
    )
    db.add(keyframe)
    db.flush()
    prompt = CommerceVideoPromptVersion(
        story_run_id=story_run.id,
        storyboard_version_id=storyboard.id,
        shot_id="shot-1",
        shot_number=1,
        keyframe_version_id=keyframe.id,
        version=1,
        prompt=f"{marker} video prompt",
        status="LOCKED",
    )
    db.add(prompt)
    db.commit()
    return {
        "outline": outline.id,
        "character": character.id,
        "scene": scene.id,
        "character_image": character_image.id,
        "scene_image": scene_image.id,
        "storyboard": storyboard.id,
        "keyframe": keyframe.id,
        "prompt": prompt.id,
    }


def test_rerun_story_run_asset_scope_stays_isolated_without_model_or_queue_work() -> None:
    """Run 2 的锁定、替换、失效和取消都不能影响 Run 1。"""

    with TestClient(app) as client:
        _project_id, source_id = _source_story_run(client)
        response = client.post(f"/api/v1/commerce/story-runs/{source_id}/rerun")
        assert response.status_code == 201, response.text
        rerun_id = response.json()["id"]

    db = SessionLocal()
    try:
        source = db.get(StoryRun, source_id)
        rerun = db.get(StoryRun, rerun_id)
        assert source is not None and rerun is not None
        source_assets = _seed_locked_scope_assets(db, story_run=source, marker="run-1")
        rerun_assets = _seed_locked_scope_assets(db, story_run=rerun, marker="run-2")
        invocations_before = db.scalar(select(func.count(ModelInvocation.id)))

        # 锁定图像查询通过当前 Run 的设计版本选择：即使 role_id/scene_id 相同，绝不
        # 返回另一个 Run 的图片。
        selected_character = _latest_locked_image(
            db,
            CommerceCharacterReferenceImage,
            design_column="character_design_version_id",
            design_id=rerun_assets["character"],
            logical_id_column="role_id",
            logical_id="role-1",
        )
        selected_scene = _latest_locked_image(
            db,
            CommerceSceneReferenceImage,
            design_column="scene_design_version_id",
            design_id=rerun_assets["scene"],
            logical_id_column="scene_id",
            logical_id="scene-1",
        )
        assert selected_character is not None and selected_character.id == rerun_assets["character_image"]
        assert selected_scene is not None and selected_scene.id == rerun_assets["scene_image"]

        # Run 2 新分镜锁定时，只会 supersede 自己的旧分镜；Run 1 的已锁定分镜保持采用。
        rerun_board_v2 = CommerceStoryboardVersion(
            story_run_id=rerun.id,
            source_outline_version_id=rerun_assets["outline"],
            character_design_version_id=rerun_assets["character"],
            scene_design_version_id=rerun_assets["scene"],
            source_product_asset_version_id=rerun.product_asset_version_id,
            version=2,
            status="READY",
            content={"shots": [{"shot_id": "shot-2", "shot_number": 1, "segment_summary": "run-2 replacement"}]},
        )
        db.add(rerun_board_v2)
        db.flush()
        lock_storyboard(
            db, story_run_id=rerun.id, version_id=rerun_board_v2.id, reviewer_label="scope test", note=None
        )
        assert db.get(CommerceStoryboardVersion, source_assets["storyboard"]).status == "LOCKED"
        assert db.get(CommerceStoryboardVersion, rerun_assets["storyboard"]).status == "SUPERSEDED"
        assert _current_storyboard(db, source.id).id == source_assets["storyboard"]
        assert _current_storyboard(db, rerun.id).id == rerun_board_v2.id

        # Run 2 重生关键帧只会标记它自己的 prompt/video 为失效，不会污染 Run 1。
        _mark_stale(db, rerun.id, source="KEYFRAME", shot_id="shot-1")
        db.commit()
        assert db.get(CommerceVideoPromptVersion, source_assets["prompt"]).status == "LOCKED"
        assert db.get(CommerceVideoPromptVersion, rerun_assets["prompt"]).status == "STALE"

        # 仅创建 Run 2 的视频任务冻结快照（不投递 Worker）：它只能使用 Run 2 当前
        # 分镜，不能偷读 Run 1 已锁定关键帧、Prompt 或图片。
        frozen_run, created = create_production_run(
            db, story_run_id=rerun.id, operation="VIDEO_RENDER", target_id="shot-2"
        )
        assert created is True
        frozen = frozen_run.input_snapshot["commerce_production"]
        assert frozen["story_run_id"] == rerun.id
        assert frozen["storyboard"]["id"] == rerun_board_v2.id
        assert source_assets["storyboard"] not in repr(frozen)
        assert db.scalar(select(func.count(ModelInvocation.id))) == invocations_before

        source_parent = db.scalar(
            select(WorkflowRun).join(CommerceWorkflowLink).where(CommerceWorkflowLink.story_run_id == source.id)
        )
        assert source_parent is not None
        source_parent_state = source_parent.status
        cancel_story_run(db, rerun.id)
        db.refresh(source)
        db.refresh(source.state)
        db.refresh(source_parent)
        assert source.state.status.value != "CANCELLED"
        assert source_parent.status == source_parent_state
        assert db.scalar(select(func.count(ModelInvocation.id))) == invocations_before
    finally:
        db.close()


def _alembic(database_url: str, *arguments: str, check: bool = True):
    """独立进程运行 Alembic，避免测试进程已加载的 Settings 覆盖临时 URL。"""

    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    environment["PYTHONPYCACHEPREFIX"] = "/private/tmp/lemonflow-rerun-migration-pycache"
    result = run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


def _seed_0020_input(database_url: str) -> dict[str, str]:
    """按 0020 的真实外键关系写入一个历史 Commerce 输入。

    这里刻意不用关闭 SQLite 外键：0021 的升级会运行 ``foreign_key_check``，因此
    迁移回归测试本身必须代表一个能在生产中存在的 0020 数据库，而不是用悬空 ID
    掩盖回填逻辑。
    """

    engine = create_engine(database_url)
    ids = {
        name: str(uuid4())
        for name in (
            "project",
            "analysis_run",
            "creative_run",
            "topic",
            "media",
            "script_asset",
            "script_analysis",
            "product_asset",
            "product_analysis",
            "product_version",
            "selection",
            "reference_analysis",
            "intake",
            "batch",
            "idea",
            "story",
        )
    }
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects (id, title, created_at, updated_at) "
                "VALUES (:project, '0021 migration fixture', :now, :now)"
            ),
            {**ids, "now": now},
        )
        for run_id, workflow_key in (
            (ids["analysis_run"], "v1_reference_analysis"),
            (ids["creative_run"], "commerce_creative_generation"),
        ):
            connection.execute(
                text(
                    "INSERT INTO workflow_runs "
                    "(id, project_id, workflow_key, status, created_at) "
                    "VALUES (:id, :project, :workflow_key, 'SUCCEEDED', :now)"
                ),
                {"id": run_id, "project": ids["project"], "workflow_key": workflow_key, "now": now},
            )
        connection.execute(
            text(
                "INSERT INTO topic_candidates "
                "(id, project_id, generation_run_id, position, title, opening_hook, synopsis, status, created_at, updated_at) "
                "VALUES (:topic, :project, :analysis_run, 1, '候选', '钩子', '简介', 'SELECTED', :now, :now)"
            ),
            {**ids, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO media_assets "
                "(id, project_id, kind, original_filename, content_type, byte_size, storage_key, created_at) "
                "VALUES (:media, :project, 'SOURCE_VIDEO', 'fixture.mp4', 'video/mp4', 1, 'fixture/0021.mp4', :now)"
            ),
            {**ids, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO script_assets (id, project_id, media_asset_id, name, created_at, updated_at) "
                "VALUES (:script_asset, :project, :media, '脚本', :now, :now)"
            ),
            {**ids, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO script_analysis_versions "
                "(id, script_asset_id, version, analysis_status, created_at, updated_at) "
                "VALUES (:script_analysis, :script_asset, 1, 'SUCCEEDED', :now, :now)"
            ),
            {**ids, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO product_assets (id, name, created_at, updated_at) "
                "VALUES (:product_asset, '测试产品', :now, :now)"
            ),
            {**ids, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO product_analysis_versions "
                "(id, product_asset_id, version, analysis_status, created_at, updated_at) "
                "VALUES (:product_analysis, :product_asset, 1, 'SUCCEEDED', :now, :now)"
            ),
            {**ids, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO product_asset_versions "
                "(id, product_asset_id, source_analysis_version_id, version, product_name, status, frozen_at, created_at) "
                "VALUES (:product_version, :product_asset, :product_analysis, 1, '测试产品', 'CONFIRMED', :now, :now)"
            ),
            {**ids, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO project_product_selections "
                "(id, project_id, product_asset_id, product_asset_version_id, selected_at, created_at) "
                "VALUES (:selection, :project, :product_asset, :product_version, :now, :now)"
            ),
            {**ids, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO reference_analyses "
                "(id, project_id, workflow_run_id, version, video_script_structure, opening_analysis, viral_elements, scene_analysis, creative_brief, generation_status, review_status, locked_snapshot, locked_at, created_at, updated_at) "
                "VALUES (:reference_analysis, :project, :analysis_run, 1, '{}', '{}', '[]', '[]', '{}', 'SUCCEEDED', 'LOCKED', '{}', :now, :now, :now)"
            ),
            {**ids, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO commerce_reference_intakes "
                "(id, project_id, reference_analysis_id, script_asset_id, script_analysis_version_id, product_asset_id, product_analysis_version_id, product_asset_version_id, created_at, updated_at) "
                "VALUES (:intake, :project, :reference_analysis, :script_asset, :script_analysis, :product_asset, :product_analysis, :product_version, :now, :now)"
            ),
            {**ids, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO commerce_creative_batches "
                "(id, project_id, reference_intake_id, workflow_run_id, batch_number, status, created_at) "
                "VALUES (:batch, :project, :intake, :creative_run, 1, 'SUCCEEDED', :now)"
            ),
            {**ids, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO commerce_creative_ideas "
                "(id, batch_id, project_id, topic_candidate_id, candidate_number, status, selected_at, created_at) "
                "VALUES (:idea, :batch, :project, :topic, 1, 'SELECTED', :now, :now)"
            ),
            {**ids, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO story_runs "
                "(id, project_id, topic_candidate_id, project_product_selection_id, product_asset_version_id, run_number, mode, created_at, updated_at) "
                "VALUES (:story, :project, :topic, :selection, :product_version, 1, 'STEPWISE', :now, :now)"
            ),
            {**ids, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO commerce_story_run_inputs "
                "(story_run_id, creative_batch_id, creative_idea_id, reference_analysis_id, script_analysis_version_id, product_asset_version_id, input_snapshot, created_at) "
                "VALUES (:story, :batch, :idea, :reference_analysis, :script_analysis, :product_version, '{}', :now)"
            ),
            {**ids, "now": now},
        )
    return ids


def test_0021_migration_backfills_constraints_and_refuses_lossy_downgrade(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'rerun-migration.db'}"
    _alembic(database_url, "upgrade", "0020_phase4_asset_center_foreign_key_repair")
    ids = _seed_0020_input(database_url)
    _alembic(database_url, "upgrade", "head")
    engine = create_engine(database_url)
    inspector = inspect(engine)
    assert "run_number" in {column["name"] for column in inspector.get_columns("commerce_story_run_inputs")}
    assert RERUN_UNIQUE in {item["name"] for item in inspector.get_unique_constraints("commerce_story_run_inputs")}
    assert LEGACY_UNIQUE not in {item["name"] for item in inspector.get_unique_constraints("commerce_story_run_inputs")}
    assert IDEA_INDEX in {item["name"] for item in inspector.get_indexes("commerce_story_run_inputs")}
    assert next(column for column in inspector.get_columns("commerce_story_run_inputs") if column["name"] == "story_run_id")["primary_key"] == 1
    with engine.begin() as connection:
        assert connection.execute(text("SELECT run_number FROM commerce_story_run_inputs WHERE story_run_id=:id"), {"id": ids["story"]}).scalar_one() == 1
        with pytest.raises(IntegrityError):
            connection.execute(text("UPDATE commerce_story_run_inputs SET run_number=9 WHERE story_run_id=:id"), {"id": ids["story"]})
        with pytest.raises(IntegrityError):
            connection.execute(text("UPDATE story_runs SET run_number=9 WHERE id=:id"), {"id": ids["story"]})
    _alembic(database_url, "downgrade", "0020_phase4_asset_center_foreign_key_repair")
    inspector = inspect(engine)
    assert "run_number" not in {column["name"] for column in inspector.get_columns("commerce_story_run_inputs")}
    assert LEGACY_UNIQUE in {item["name"] for item in inspector.get_unique_constraints("commerce_story_run_inputs")}
    _alembic(database_url, "upgrade", "head")
    with engine.begin() as connection:
        second_story_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            text(
                "INSERT INTO story_runs "
                "(id, project_id, topic_candidate_id, project_product_selection_id, product_asset_version_id, run_number, mode, created_at, updated_at) "
                "VALUES (:id, :project, :topic, :selection, :product_version, 2, 'STEPWISE', :now, :now)"
            ),
            {"id": second_story_id, **ids, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO commerce_story_run_inputs "
                "(story_run_id, creative_batch_id, creative_idea_id, run_number, reference_analysis_id, script_analysis_version_id, product_asset_version_id, input_snapshot, created_at) "
                "VALUES (:story, :batch, :idea, 2, :reference_analysis, :script_analysis, :product_version, '{}', :now)"
            ),
            {**ids, "story": second_story_id, "now": now},
        )
    rejected = _alembic(database_url, "downgrade", "0020_phase4_asset_center_foreign_key_repair", check=False)
    assert rejected.returncode != 0
    assert "降级被拒绝" in rejected.stderr


def test_0021_postgresql_offline_sql_contains_equivalent_constraints_and_guards() -> None:
    """在没有 PostgreSQL 服务的 CI 中检查 0021 的正式 PostgreSQL DDL 分支。"""

    database_url = "postgresql+psycopg://lemonflow:placeholder@localhost/lemonflow"
    upgraded = _alembic(database_url, "upgrade", "0020_phase4_asset_center_foreign_key_repair:0021_commerce_story_run_rerun", "--sql")
    assert "ALTER TABLE commerce_story_run_inputs ADD COLUMN run_number INTEGER" in upgraded.stdout
    assert f"DROP CONSTRAINT {LEGACY_UNIQUE}" in upgraded.stdout
    assert f"ADD CONSTRAINT {RERUN_UNIQUE} UNIQUE (creative_idea_id, run_number)" in upgraded.stdout
    assert f"CREATE INDEX {IDEA_INDEX}" in upgraded.stdout
    assert "CREATE OR REPLACE FUNCTION commerce_story_run_input_number_guard" in upgraded.stdout
    assert "CREATE OR REPLACE FUNCTION commerce_story_run_number_immutable_guard" in upgraded.stdout
    assert "FOREIGN KEY" not in upgraded.stdout  # 0021 不改既有外键或级联规则。

    downgraded = _alembic(database_url, "downgrade", "0021_commerce_story_run_rerun:0020_phase4_asset_center_foreign_key_repair", "--sql")
    assert "DROP TRIGGER IF EXISTS trg_commerce_story_run_input_number_insert ON commerce_story_run_inputs" in downgraded.stdout
    assert "DROP FUNCTION IF EXISTS commerce_story_run_input_number_guard()" in downgraded.stdout
    assert f"DROP CONSTRAINT {RERUN_UNIQUE}" in downgraded.stdout
    assert "DROP COLUMN run_number" in downgraded.stdout
    assert f"ADD CONSTRAINT {LEGACY_UNIQUE} UNIQUE (creative_idea_id)" in downgraded.stdout


# Stable names from 0021 are repeated here deliberately: tests validate the published
# database contract rather than importing the migration module as application code.
RERUN_UNIQUE = "uq_commerce_story_run_input_idea_run_number"
LEGACY_UNIQUE = "uq_commerce_story_run_input_idea"
IDEA_INDEX = "ix_commerce_story_run_inputs_creative_idea_id"
