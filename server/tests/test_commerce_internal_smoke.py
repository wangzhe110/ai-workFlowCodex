"""内部静态技术验收入口：不触发模型也可安全建立最小关键帧/视频前置。"""

from __future__ import annotations

from base64 import b64decode
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.main import app
from app.models import (
    CommerceCharacterDesignVersion,
    CommerceCharacterReferenceImage,
    CommerceSceneDesignVersion,
    CommerceSceneReferenceImage,
    CommerceShotKeyframeVersion,
    CommerceStoryboardVersion,
    CommerceVideoPromptVersion,
    ModelInvocation,
    ModelProfile,
    ModelSlot,
    OutlineVersionStatus,
    RunStatus,
    StoryOutlineVersion,
    StoryRun,
    StoryRunStatus,
    WorkflowRun,
    WorkflowStep,
)
from app.services import commerce_internal_smoke_service as smoke
from app.services import storage
from app.services.commerce_production_service import _keyframe_assets, create_production_run
from app.services.commerce_workflow_service import rerun_story_run


def _video() -> bytes:
    return b64decode((Path(__file__).parent / "fixtures" / "real-video.mp4.base64").read_text(encoding="ascii"))


def _jpeg(*, width: int = 2048, height: int = 2048, byte_size: int = 330372, seed: int = 0) -> bytes:
    """构造可被本地 JPEG 尺寸校验识别的固定大小测试图，不依赖 Pillow。"""

    sof = (
        b"\xff\xd8\xff\xc0\x00\x11\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    )
    assert byte_size > len(sof) + 2
    return sof + bytes([seed]) * (byte_size - len(sof) - 2) + b"\xff\xd9"


def _source_story_run(client: TestClient) -> tuple[str, str]:
    """通过既有 Slice 1 API 获得可重跑的真实冻结输入，不直接伪造主线。"""

    project_id = client.post("/api/v1/projects", json={"title": f"Internal smoke {uuid4().hex[:8]}"}).json()["id"]
    uploaded = client.post(
        f"/api/v1/projects/{project_id}/source-video",
        files={"file": ("reference.mp4", _video(), "video/mp4")},
    )
    assert uploaded.status_code == 201, uploaded.text
    analysis = client.post(
        f"/api/v1/production/projects/{project_id}/generation-runs/reference_analysis",
        json={"source_asset_id": uploaded.json()["id"]},
    )
    assert analysis.status_code == 202, analysis.text
    reference = client.get(f"/api/v1/production/projects/{project_id}/reference-analyses").json()[0]
    assert client.post(f"/api/v1/production/reference-analyses/{reference['id']}/lock", json={}).status_code == 200
    intake = client.get(f"/api/v1/production/projects/{project_id}/commerce-reference-intakes").json()[0]
    confirmed = client.post(
        f"/api/v1/production/commerce-reference-intakes/{intake['id']}/confirm-product",
        json={"product_name": "内部测试商品", "appearance_description": "固定包装", "selling_points": [{"claim": "冻结卖点"}]},
    )
    assert confirmed.status_code == 200, confirmed.text
    generated = client.post(f"/api/v1/production/projects/{project_id}/generation-runs/commerce_creative_generation", json={})
    assert generated.status_code == 202, generated.text
    batch = client.get(f"/api/v1/production/projects/{project_id}/commerce-creative-batches").json()[0]
    selected = client.post(f"/api/v1/production/commerce-creative-ideas/{batch['ideas'][0]['id']}/select", json={})
    assert selected.status_code == 201, selected.text
    return project_id, selected.json()["story_run_id"]


@pytest.fixture()
def smoke_scope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """把固定受控图写到临时媒体根，再构造来源 Run 与隔离 rerun。"""

    monkeypatch.setattr(storage, "settings", replace(storage.settings, local_storage_path=tmp_path))
    robot = _jpeg(seed=1)
    room = _jpeg(byte_size=2048, seed=2)
    robot_url = storage.local_asset_storage.save_generated_image_bytes(
        project_id="seedream-smoke-20260815", asset_kind="single-image-smoke", asset_id="robot-source", version=1,
        content=robot, content_type="image/jpeg",
    )
    monkeypatch.setattr(smoke, "ROBOT_SOURCE_URL", robot_url)
    monkeypatch.setattr(smoke, "ROBOT_SOURCE_SHA256", sha256(robot).hexdigest())
    monkeypatch.setattr(smoke, "ROOM_SOURCE_SHA256", sha256(room).hexdigest())
    monkeypatch.setattr(smoke, "ROOM_SOURCE_ASSET_ID", str(uuid4()))

    with TestClient(app) as client:
        project_id, source_id = _source_story_run(client)
        db = SessionLocal()
        try:
            source = db.get(StoryRun, source_id)
            assert source is not None
            outline = db.scalars(
                select(StoryOutlineVersion)
                .where(StoryOutlineVersion.story_run_id == source.id, StoryOutlineVersion.status == OutlineVersionStatus.LOCKED)
                .order_by(StoryOutlineVersion.version.desc())
            ).first()
            if outline is None:
                outline = StoryOutlineVersion(
                    story_run_id=source.id,
                    version=int(db.scalar(select(func.max(StoryOutlineVersion.version)).where(StoryOutlineVersion.story_run_id == source.id)) or 0) + 1,
                    title="来源", premise="来源场景", story_beats=[], product_placement_strategy={},
                    status=OutlineVersionStatus.LOCKED,
                )
                db.add(outline); db.flush()
            character = CommerceCharacterDesignVersion(
                story_run_id=source.id, source_outline_version_id=outline.id,
                source_product_asset_version_id=source.product_asset_version_id, version=1, status="LOCKED",
                content={"roles": [{"role_id": "source-role"}]}, locked_at=smoke._utcnow(),
            )
            db.add(character); db.flush()
            scene = CommerceSceneDesignVersion(
                story_run_id=source.id, source_outline_version_id=outline.id, character_design_version_id=character.id,
                source_product_asset_version_id=source.product_asset_version_id, version=1, status="LOCKED",
                content={"scenes": [{"scene_id": "source-scene"}]}, locked_at=smoke._utcnow(),
            )
            db.add(scene); db.flush()
            room_url = storage.local_asset_storage.save_generated_image_bytes(
                project_id=project_id, asset_kind="source-room", asset_id="room-source", version=1,
                content=room, content_type="image/jpeg",
            )
            db.add(
                CommerceSceneReferenceImage(
                    id=smoke.ROOM_SOURCE_ASSET_ID, story_run_id=source.id, scene_design_version_id=scene.id,
                    scene_id="source-room", version=1, image_url=room_url, status="LOCKED", locked_at=smoke._utcnow(),
                )
            )
            db.commit()
            source_snapshot = {
                "state": (source.state.current_stage.value, source.state.status.value),
                "outline": outline.id,
                "character": character.id,
                "scene": scene.id,
                "room": room_url,
                "room_sha": sha256(room).hexdigest(),
            }
            rerun = rerun_story_run(db, source_story_run_id=source_id)
            target_id = rerun[0].id
        finally:
            db.close()
        yield client, project_id, source_id, target_id, source_snapshot


def _counts(db, story_run_id: str) -> dict[str, int]:
    models = {
        "outlines": StoryOutlineVersion,
        "characters": CommerceCharacterDesignVersion,
        "scenes": CommerceSceneDesignVersion,
        "storyboards": CommerceStoryboardVersion,
        "keyframes": CommerceShotKeyframeVersion,
        "prompts": CommerceVideoPromptVersion,
    }
    return {name: int(db.scalar(select(func.count(model.id)).where(model.story_run_id == story_run_id)) or 0) for name, model in models.items()}


def test_internal_smoke_bootstrap_is_atomic_idempotent_isolated_and_keyframe_ready(smoke_scope) -> None:
    client, project_id, source_id, target_id, source_snapshot = smoke_scope
    session = SessionLocal()
    try:
        before_invocations = session.scalar(select(func.count(ModelInvocation.id)))
    finally:
        session.close()
    try:
        created = client.post(
            f"/api/v1/commerce/story-runs/{target_id}/internal-smoke/bootstrap",
            json={"confirm": smoke.INTERNAL_CONFIRMATION},
        )
        assert created.status_code == 201, created.text
        payload = created.json()
        assert payload["idempotent"] is False
        assert payload["current_stage"] == "VISUAL_ASSETS"
        assert payload["current_status"] == "PENDING"

        repeated = client.post(
            f"/api/v1/commerce/story-runs/{target_id}/internal-smoke/bootstrap",
            json={"confirm": smoke.INTERNAL_CONFIRMATION},
        )
        assert repeated.status_code == 201 and repeated.json()["idempotent"] is True
        assert repeated.json()["character_image_id"] == payload["character_image_id"]

        db = SessionLocal()
        try:
            target = db.get(StoryRun, target_id)
            source = db.get(StoryRun, source_id)
            assert target is not None and source is not None
            assert _counts(db, target_id) == {"outlines": 1, "characters": 1, "scenes": 1, "storyboards": 1, "keyframes": 0, "prompts": 0}
            assert (source.state.current_stage.value, source.state.status.value) == source_snapshot["state"]
            assert db.get(CommerceSceneReferenceImage, smoke.ROOM_SOURCE_ASSET_ID).image_url == source_snapshot["room"]
            assert db.scalar(select(func.count(ModelInvocation.id))) == before_invocations

            outline = db.get(StoryOutlineVersion, payload["outline_id"])
            character = db.get(CommerceCharacterDesignVersion, payload["character_design_id"])
            scene = db.get(CommerceSceneDesignVersion, payload["scene_design_id"])
            assert outline is not None and character is not None and scene is not None
            static_rows = [
                outline.product_placement_strategy,
                character.content,
                character.input_snapshot,
                scene.content,
                scene.input_snapshot,
                db.get(CommerceCharacterReferenceImage, payload["character_image_id"]).input_snapshot,
                db.get(CommerceSceneReferenceImage, payload["scene_image_id"]).input_snapshot,
            ]
            static_serialized = json.dumps(static_rows, ensure_ascii=False)
            assert "ARK_API_KEY" not in static_serialized
            assert "Authorization" not in static_serialized
            assert "data:image" not in static_serialized
            assert "base64" not in static_serialized.lower()

            run, generated = create_production_run(db, story_run_id=target_id, operation="SHOT_KEYFRAME", target_id=smoke.SHOT_ID)
            assert generated is True
            shot = run.input_snapshot["commerce_production"]["storyboard"]["content"]["shots"][0]
            _urls, local_assets, snapshot = _keyframe_assets(db, run.input_snapshot["commerce_production"], shot)
            assert [row["role"] for row in local_assets] == ["character", "scene"]
            assert snapshot["character_reference_image_ids"] == [payload["character_image_id"]]
            assert snapshot["scene_reference_image_id"] == payload["scene_image_id"]
            assert "data:image" not in repr(snapshot)
        finally:
            db.close()
    finally:
        db = SessionLocal()
        try:
            assert db.scalar(select(func.count(ModelInvocation.id))) == before_invocations
        finally:
            db.close()


def test_internal_smoke_rejects_bad_confirmation_non_rerun_conflicts_bad_source_and_cancelled(smoke_scope, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _project_id, source_id, target_id, _source_snapshot = smoke_scope
    assert client.post(
        f"/api/v1/commerce/story-runs/{target_id}/internal-smoke/bootstrap", json={"confirm": "no"}
    ).status_code == 422
    assert client.post(
        f"/api/v1/commerce/story-runs/{source_id}/internal-smoke/bootstrap", json={"confirm": smoke.INTERNAL_CONFIRMATION}
    ).status_code == 409

    original_robot_url = smoke.ROBOT_SOURCE_URL
    original_robot_sha = smoke.ROBOT_SOURCE_SHA256
    monkeypatch.setattr(smoke, "ROBOT_SOURCE_URL", "/media/generated/../outside.jpg")
    rejected = client.post(
        f"/api/v1/commerce/story-runs/{target_id}/internal-smoke/bootstrap", json={"confirm": smoke.INTERNAL_CONFIRMATION}
    )
    assert rejected.status_code == 422
    monkeypatch.setattr(smoke, "ROBOT_SOURCE_URL", original_robot_url)
    monkeypatch.setattr(smoke, "ROBOT_SOURCE_SHA256", "0" * 64)
    assert client.post(
        f"/api/v1/commerce/story-runs/{target_id}/internal-smoke/bootstrap",
        json={"confirm": smoke.INTERNAL_CONFIRMATION},
    ).status_code == 422
    monkeypatch.setattr(smoke, "ROBOT_SOURCE_SHA256", original_robot_sha)

    # 任意既有生产资产都属于冲突状态，入口不能把静态结果混入已有结果或覆盖它。
    db = SessionLocal()
    try:
        db.add(
            StoryOutlineVersion(
                story_run_id=target_id,
                version=1,
                title="非静态内容",
                premise="冲突测试",
                story_beats=[],
                product_placement_strategy={},
                status=OutlineVersionStatus.DRAFT,
            )
        )
        db.commit()
    finally:
        db.close()
    assert client.post(
        f"/api/v1/commerce/story-runs/{target_id}/internal-smoke/bootstrap",
        json={"confirm": smoke.INTERNAL_CONFIRMATION},
    ).status_code == 409

    db = SessionLocal()
    try:
        target = db.get(StoryRun, target_id)
        assert target is not None
        conflict_outline = db.scalar(
            select(StoryOutlineVersion).where(StoryOutlineVersion.story_run_id == target_id)
        )
        db.delete(conflict_outline)
        target.state.status = StoryRunStatus.CANCELLED
        db.commit()
    finally:
        db.close()
    assert client.post(
        f"/api/v1/commerce/story-runs/{target_id}/internal-smoke/bootstrap",
        json={"confirm": smoke.INTERNAL_CONFIRMATION},
    ).status_code == 409


def test_internal_smoke_rejects_rerun_that_already_has_real_model_invocation(smoke_scope) -> None:
    """入口不得把静态资产混入已经开始真实模型生产的 rerun。"""

    client, project_id, source_id, _target_id, _source_snapshot = smoke_scope
    db = SessionLocal()
    try:
        rerun, _parent = rerun_story_run(db, source_story_run_id=source_id)
        slot = db.scalars(select(ModelSlot).order_by(ModelSlot.created_at)).first()
        profile = db.scalars(select(ModelProfile).order_by(ModelProfile.created_at)).first()
        assert slot is not None and profile is not None
        run = WorkflowRun(
            project_id=project_id,
            workflow_key="commerce_production_internal_test",
            input_snapshot={"commerce_production": {"story_run_id": rerun.id}},
            status=RunStatus.SUCCEEDED,
        )
        step = WorkflowStep(
            workflow_run=run,
            step_key="COMMERCE_TEST",
            position=1,
            attempt=1,
            status=RunStatus.SUCCEEDED,
            input_payload={},
            model_profile_snapshot={},
        )
        db.add_all((run, step))
        db.flush()
        db.add(
            ModelInvocation(
                project_id=project_id,
                workflow_run_id=run.id,
                workflow_step_id=step.id,
                model_slot_id=slot.id,
                model_profile_id=profile.id,
                task_type="INTERNAL_TEST",
                model_profile_snapshot={},
                prompt_snapshot={},
                input_snapshot={},
                status=RunStatus.SUCCEEDED,
            )
        )
        db.commit()
        rerun_id = rerun.id
    finally:
        db.close()

    rejected = client.post(
        f"/api/v1/commerce/story-runs/{rerun_id}/internal-smoke/bootstrap",
        json={"confirm": smoke.INTERNAL_CONFIRMATION},
    )
    assert rejected.status_code == 409
    db = SessionLocal()
    try:
        assert _counts(db, rerun_id) == {
            "outlines": 0,
            "characters": 0,
            "scenes": 0,
            "storyboards": 0,
            "keyframes": 0,
            "prompts": 0,
        }
    finally:
        db.close()


def test_internal_smoke_rolls_back_partial_bundle_and_video_prompt_requires_locked_keyframe(smoke_scope, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _project_id, _source_id, target_id, _source_snapshot = smoke_scope
    original_clone = smoke.local_asset_storage.clone_generated_image
    calls = 0

    def fail_second_clone(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated controlled media failure")
        return original_clone(**kwargs)

    monkeypatch.setattr(smoke.local_asset_storage, "clone_generated_image", fail_second_clone)
    failed = client.post(
        f"/api/v1/commerce/story-runs/{target_id}/internal-smoke/bootstrap", json={"confirm": smoke.INTERNAL_CONFIRMATION}
    )
    assert failed.status_code == 422
    db = SessionLocal()
    try:
        assert _counts(db, target_id) == {"outlines": 0, "characters": 0, "scenes": 0, "storyboards": 0, "keyframes": 0, "prompts": 0}
    finally:
        db.close()

    monkeypatch.setattr(smoke.local_asset_storage, "clone_generated_image", original_clone)
    bootstrap = client.post(
        f"/api/v1/commerce/story-runs/{target_id}/internal-smoke/bootstrap", json={"confirm": smoke.INTERNAL_CONFIRMATION}
    ).json()
    no_keyframe = client.post(
        f"/api/v1/commerce/story-runs/{target_id}/internal-smoke/video-prompt",
        json={"confirm": smoke.INTERNAL_CONFIRMATION, "keyframe_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert no_keyframe.status_code == 422

    db = SessionLocal()
    try:
        frame = CommerceShotKeyframeVersion(
            story_run_id=target_id, storyboard_version_id=bootstrap["storyboard_id"], shot_id=smoke.SHOT_ID,
            shot_number=1, version=1, image_url="/media/generated/projects/placeholder/commerce-keyframe/none/v1.jpg",
            input_asset_snapshot={"internal_smoke": {"fixture_version": smoke.FIXTURE_VERSION}}, status="LOCKED", locked_at=smoke._utcnow(),
        )
        db.add(frame); db.commit(); frame_id = frame.id
    finally:
        db.close()
    prompt = client.post(
        f"/api/v1/commerce/story-runs/{target_id}/internal-smoke/video-prompt",
        json={"confirm": smoke.INTERNAL_CONFIRMATION, "keyframe_id": frame_id},
    )
    assert prompt.status_code == 201, prompt.text
    repeated = client.post(
        f"/api/v1/commerce/story-runs/{target_id}/internal-smoke/video-prompt",
        json={"confirm": smoke.INTERNAL_CONFIRMATION, "keyframe_id": frame_id},
    )
    assert repeated.status_code == 201 and repeated.json()["idempotent"] is True

    db = SessionLocal()
    try:
        row = db.get(CommerceVideoPromptVersion, prompt.json()["video_prompt_id"])
        assert row is not None and row.status == "LOCKED"
        assert row.model_invocation_id is None
        serialized = repr(row.trace)
        assert "data:image" not in serialized and "Authorization" not in serialized and "ARK_API_KEY" not in serialized
    finally:
        db.close()
