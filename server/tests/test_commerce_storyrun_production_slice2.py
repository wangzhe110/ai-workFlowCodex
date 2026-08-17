"""Commerce Slice 2：同一 StoryRun 的导演、视觉、视频与成片版本闭环。"""

from __future__ import annotations

from base64 import b64decode, b64encode
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from subprocess import CalledProcessError

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal
from app.main import app
from app.services.analysis_provider import ImageTaskResult, VideoTaskResult
from app.services.storage import LocalImageReference, local_asset_storage
from app.models import (
    CommerceCharacterDesignVersion,
    CommerceFinalVideo,
    CommerceShotKeyframeVersion,
    CommerceStoryboardVersion,
    CommerceVideoClipVersion,
    CommerceVideoPromptVersion,
    ModelInvocation,
    ModelProfile,
    ModelSlot,
    ModelSlotProfileBinding,
    ProductAsset,
    ProductAssetVersion,
    ProductAssetVersionStatus,
    RunStatus,
    StoryRun,
    WorkflowRun,
    WorkflowStep,
)
from app.services import commerce_production_service
from app.services.commerce_production_service import (
    create_production_run,
    resume_video_clip_provider_task,
)


def _video() -> bytes:
    return b64decode((Path(__file__).parent / "fixtures" / "real-video.mp4.base64").read_text(encoding="ascii"))


def _png_bytes() -> bytes:
    """构造最小 PNG 供非网络方舟参考图分支测试转存。"""

    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR\x00\x00\x00\x02\x00\x00\x00\x03\x08\x02\x00\x00\x00"


def _make_story_run(client: TestClient) -> tuple[str, dict]:
    """走 Slice 1 正式入口并锁定大纲，绝不手工伪造 StoryRun 输入。"""

    project_id = client.post("/api/v1/projects", json={"title": "Slice 2 production"}).json()["id"]
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
            "product_name": "Slice 2 测试商品",
            "appearance_description": "白色简洁包装",
            "selling_points": [{"claim": "已确认的日常使用卖点"}],
            "usage_scenarios": [{"scene": "店内整理"}],
            # Mock 图片可不使用该 URL；真实运行则以这一冻结 URL 作为商品参考。
            "reference_images": [{"url": "https://example.invalid/frozen-product.jpg", "angle": "front"}],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    ideas = client.post(f"/api/v1/production/projects/{project_id}/generation-runs/commerce_creative_generation", json={})
    assert ideas.status_code == 202, ideas.text
    batch = client.get(f"/api/v1/production/projects/{project_id}/commerce-creative-batches").json()[0]
    selected = client.post(
        f"/api/v1/production/commerce-creative-ideas/{batch['ideas'][0]['id']}/select",
        json={"mode": "STEPWISE"},
    )
    assert selected.status_code == 201, selected.text
    story_run_id = selected.json()["story_run_id"]
    locked_outline = client.post(
        f"/api/v1/commerce/story-runs/{story_run_id}/stages/OUTLINE/confirm", json={}
    )
    assert locked_outline.status_code == 202, locked_outline.text
    return story_run_id, intake


def _operation(client: TestClient, story_run_id: str, operation: str, *, target_id: str | None = None, retry: bool = False) -> dict:
    payload = {"target_id": target_id, "retry": retry}
    response = client.post(f"/api/v1/commerce/story-runs/{story_run_id}/production/{operation}", json=payload)
    assert response.status_code == 202, response.text
    # HTTP 202 的序列化发生在 inline BackgroundTask 之前；返回 PENDING 是正常的，
    # 必须用数据库重新读取终态，不能依赖响应对象的旧 identity map。
    run_id = response.json()["id"]
    db = SessionLocal()
    try:
        run = db.get(WorkflowRun, run_id)
        assert run is not None and run.status.value == "SUCCEEDED", run.steps[0].error_message if run is not None and run.steps else None
    finally:
        db.close()
    return response.json()


def _assets(client: TestClient, story_run_id: str) -> dict:
    response = client.get(f"/api/v1/commerce/story-runs/{story_run_id}/production-assets")
    assert response.status_code == 200, response.text
    return response.json()


def _lock(client: TestClient, path: str) -> dict:
    response = client.post(path, json={"reviewer_label": "Slice 2 测试"})
    assert response.status_code == 200, response.text
    return response.json()


def test_storyrun_slice2_mock_chain_preserves_frozen_links_and_requires_locks() -> None:
    """文本导演链、视觉链、视频审核与 Mock 成片均留在一个已冻结 StoryRun。"""

    with TestClient(app) as client:
        story_run_id, intake = _make_story_run(client)

        # 未锁定角色/场景时，导演分镜及关键帧不允许被浏览器跳过。
        blocked = client.post(f"/api/v1/commerce/story-runs/{story_run_id}/production/STORYBOARD", json={})
        assert blocked.status_code == 409
        _operation(client, story_run_id, "CHARACTER_DESIGN")
        assets = _assets(client, story_run_id)
        character = assets["character_designs"][0]
        assert character["input_snapshot"]["commerce_mainline"]["product_asset_version"]["id"] == intake["product_asset_version_id"]
        assert character["status"] == "READY"
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/character-designs/{character['id']}/lock")

        _operation(client, story_run_id, "SCENE_DESIGN")
        scene = _assets(client, story_run_id)["scene_designs"][0]
        assert scene["input_snapshot"]["character_design"]["id"] == character["id"]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/scene-designs/{scene['id']}/lock")

        _operation(client, story_run_id, "STORYBOARD")
        board = _assets(client, story_run_id)["storyboards"][0]
        shots = board["content"]["shots"]
        assert 3 <= len(shots) <= 5
        assert all(item["segment_summary"] and item["product_integration_node_id"] for item in shots)
        assert all(item["product_evidence"] for item in shots)
        # 图片锁定前关键帧被服务端阻止，而非由前端猜状态。
        blocked_keyframe = client.post(
            f"/api/v1/commerce/story-runs/{story_run_id}/production/SHOT_KEYFRAME",
            json={"target_id": shots[0]["shot_id"]},
        )
        assert blocked_keyframe.status_code == 409
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/storyboards/{board['id']}/lock")

        _operation(client, story_run_id, "CHARACTER_IMAGES")
        assets = _assets(client, story_run_id)
        for image in assets["character_images"]:
            _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/images/CHARACTER/{image['id']}/lock")
        _operation(client, story_run_id, "SCENE_IMAGES")
        assets = _assets(client, story_run_id)
        for image in assets["scene_images"]:
            _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/images/SCENE/{image['id']}/lock")

        for shot in shots:
            _operation(client, story_run_id, "SHOT_KEYFRAME", target_id=shot["shot_id"])
        assets = _assets(client, story_run_id)
        assert len(assets["keyframes"]) == len(shots)
        for frame in assets["keyframes"]:
            # 关键帧追溯只保存角色/场景资产 ID；本机路径和任何 Data URL 都不进入
            # API 响应、工作流快照或模型审计。
            assert frame["input_snapshot"]["character_reference_image_ids"]
            assert frame["input_snapshot"]["scene_reference_image_id"]
            _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/images/KEYFRAME/{frame['id']}/lock")

        for shot in shots:
            _operation(client, story_run_id, "VIDEO_PROMPT", target_id=shot["shot_id"])
            _operation(client, story_run_id, "VIDEO_RENDER", target_id=shot["shot_id"])
        assets = _assets(client, story_run_id)
        assert len(assets["video_prompts"]) == len(shots)
        assert len(assets["clips"]) == len(shots)
        for clip in assets["clips"]:
            assert clip["status"] == "SUCCEEDED"
            assert clip["video_url"].startswith("mock://")
            approved = client.post(
                f"/api/v1/commerce/story-runs/{story_run_id}/clips/{clip['id']}/review?decision=APPROVED", json={}
            )
            assert approved.status_code == 200, approved.text

        _operation(client, story_run_id, "FINAL_COMPOSE")
        final = _assets(client, story_run_id)["finals"][0]
        assert final["status"] == "SUCCEEDED"
        assert final["output_url"].startswith("mock://")
        assert final["download_url"] is None  # Mock 从不冒充真实 MP4。


def test_ark_keyframe_uses_locked_character_then_scene_assets_without_persisting_data_urls(monkeypatch) -> None:
    """真实图片 Adapter 分支只把经 Storage 解析的角色、场景 Data URL 交给方舟。"""

    with TestClient(app) as client:
        story_run_id, _ = _make_story_run(client)
        _operation(client, story_run_id, "CHARACTER_DESIGN")
        character = _assets(client, story_run_id)["character_designs"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/character-designs/{character['id']}/lock")
        _operation(client, story_run_id, "SCENE_DESIGN")
        scene = _assets(client, story_run_id)["scene_designs"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/scene-designs/{scene['id']}/lock")
        _operation(client, story_run_id, "STORYBOARD")
        board = _assets(client, story_run_id)["storyboards"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/storyboards/{board['id']}/lock")
        _operation(client, story_run_id, "CHARACTER_IMAGES")
        for image in _assets(client, story_run_id)["character_images"]:
            _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/images/CHARACTER/{image['id']}/lock")
        _operation(client, story_run_id, "SCENE_IMAGES")
        for image in _assets(client, story_run_id)["scene_images"]:
            _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/images/SCENE/{image['id']}/lock")

        db = SessionLocal()
        try:
            slot = db.scalar(select(ModelSlot).where(ModelSlot.slot_key == "SHOT_KEYFRAME_GENERATE"))
            assert slot is not None
            slot_id = slot.id
            original_bindings = list(db.scalars(select(ModelSlotProfileBinding).where(ModelSlotProfileBinding.slot_id == slot_id)).all())
            for binding in original_bindings:
                binding.is_enabled = False
            profile = ModelProfile(
                step_key="SHOT_KEYFRAME_GENERATE", provider_key="volcengine_ark_image", adapter_key="volcengine_ark_image",
                model_key="doubao-seedream-5-0-260128", model_version="seedream-test", display_name="Seedream reference test",
                version=994, provider_config={"api_base_url": "https://ark.cn-beijing.volces.com/api/v3", "secret_env_name": "ARK_API_KEY", "size": "2K", "sequential_image_generation": "disabled", "response_format": "url", "watermark": False},
                is_active=False, profile_status="ACTIVE",
            )
            db.add(profile); db.flush()
            test_binding = ModelSlotProfileBinding(slot_id=slot_id, model_profile_id=profile.id, is_enabled=True, priority=-100)
            db.add(test_binding); db.commit()
            test_binding_id = test_binding.id
        finally:
            db.close()

        loaded_roles: list[str] = []
        loaded_namespaces: list[str] = []
        captured: dict[str, object] = {}
        body = b"\x89PNG\r\n\x1a\n" + b"fixture"

        def fake_load(*, asset_id: str, role: str, **kwargs) -> LocalImageReference:
            loaded_roles.append(role)
            loaded_namespaces.append(str(kwargs["storage_namespace_id"]))
            return LocalImageReference(
                asset_id=asset_id, role=role, mime_type="image/png", width=2, height=3,
                sha256=sha256(f"{role}:{asset_id}".encode()).hexdigest(),
                data_url=f"data:image/png;base64,{b64encode(body).decode('ascii')}",
            )

        def fake_start(profile_snapshot, **kwargs):
            captured["start_calls"] = int(captured.get("start_calls", 0)) + 1
            captured["profile"] = profile_snapshot
            captured["legacy_urls"] = kwargs["reference_image_urls"]
            captured["references"] = kwargs["reference_images"]
            return None, ImageTaskResult(
                provider_task_id=None, status="SUCCEEDED", content_type="image/png", image_bytes=_png_bytes(),
                byte_size=len(_png_bytes()), sha256=sha256(_png_bytes()).hexdigest(), width=2, height=3,
            )

        monkeypatch.setattr(commerce_production_service.local_asset_storage, "load_generated_image_reference", fake_load)
        monkeypatch.setattr(commerce_production_service, "start_image_generation", fake_start)
        try:
            shot_id = board["content"]["shots"][0]["shot_id"]
            _operation(client, story_run_id, "SHOT_KEYFRAME", target_id=shot_id)
            assert loaded_roles[0] == "character" and loaded_roles[-1] == "scene"
            assert loaded_namespaces[0] == "role-1" and loaded_namespaces[-1] == "scene-1"
            assert captured["legacy_urls"] == []
            references = captured["references"]
            assert isinstance(references, list) and len(references) >= 2
            assert references[0].role == "character" and references[-1].role == "scene"
            frame = _assets(client, story_run_id)["keyframes"][0]
            serialized = repr(frame["input_snapshot"])
            assert "data:image" not in serialized and "/media/generated/" not in serialized
            assert frame["input_snapshot"]["reference_assets"]
            db = SessionLocal()
            try:
                frame_row = db.get(CommerceShotKeyframeVersion, frame["id"])
                assert frame_row is not None and frame_row.model_invocation_id
                invocation = db.get(ModelInvocation, frame_row.model_invocation_id)
                assert invocation is not None
                assert invocation.input_snapshot["reference_assets"]
                assert "data:image" not in repr(invocation.input_snapshot)
                run_row = db.get(WorkflowRun, frame_row.workflow_run_id)
                assert run_row is not None and run_row.steps
                repeated = commerce_production_service._execute_keyframe(
                    db, run_row, run_row.steps[0], run_row.input_snapshot["commerce_production"]
                )
                assert repeated["shot_keyframe_version_id"] == frame_row.id
                assert captured["start_calls"] == 1
            finally:
                db.close()
        finally:
            db = SessionLocal()
            try:
                db.query(ModelSlotProfileBinding).filter(ModelSlotProfileBinding.id == test_binding_id).delete()
                for binding in db.scalars(select(ModelSlotProfileBinding).where(ModelSlotProfileBinding.slot_id == slot_id)):
                    binding.is_enabled = True
                db.commit()
            finally:
                db.close()


def test_slice2_real_adapter_branch_is_not_hardcoded(monkeypatch) -> None:
    """通过 Fake 文本 Adapter 覆盖非 Mock 分支，且完整冻结输入进入模型调用。"""

    from app.services import commerce_production_service

    with TestClient(app) as client:
        story_run_id, _ = _make_story_run(client)
        db = SessionLocal()
        try:
            slot = db.scalar(select(ModelSlot).where(ModelSlot.slot_key == "CHARACTER_DESIGN"))
            assert slot is not None
            profile = ModelProfile(
                step_key="CHARACTER_DESIGN", provider_key="openai_compatible", adapter_key="openai_compatible",
                model_key="slice2-fake", model_version="slice2-fake", display_name="Slice2 Fake", version=995,
                provider_config={"api_base_url": "https://fake.invalid", "secret_env_name": "DECOY"}, is_active=False, profile_status="ACTIVE",
            )
            db.add(profile); db.flush()
            # CHARACTER_DESIGN 是单模型槽位。测试临时停用默认 mock 绑定，模拟模型
            # 中心人工切换到一个实际 Adapter；finally 中恢复，避免污染其余回归。
            defaults = list(db.scalars(select(ModelSlotProfileBinding).where(ModelSlotProfileBinding.slot_id == slot.id)).all())
            default_states = {binding.id: binding.is_enabled for binding in defaults}
            for default in defaults:
                default.is_enabled = False
            binding = ModelSlotProfileBinding(slot_id=slot.id, model_profile_id=profile.id, is_enabled=True, priority=-100)
            db.add(binding); db.commit()
            profile_id = profile.id
        finally:
            db.close()
        captured: dict[str, object] = {}

        def fake_generate(snapshot, **kwargs):
            captured["snapshot"] = snapshot
            captured["payload"] = kwargs["user_payload"]
            return {"roles": [{
                "role_id": "real-role", "name": "真实路径角色", "age_range": "25岁", "gender": "女", "identity_and_occupation": "店员", "personality": "细心", "dramatic_function": "推动剧情", "relationships": [], "appearance": "自然", "hairstyle": "低马尾", "costume": "浅色衬衫", "fixed_visual_features": ["低马尾"], "immutable_features": ["低马尾"], "product_relationship": "体验冻结商品", "buyer": True, "user": True, "decision_influencer": False, "image_prompt": "角色设定"
            }]}

        monkeypatch.setattr(commerce_production_service, "generate_structured_text", fake_generate)
        try:
            _operation(client, story_run_id, "CHARACTER_DESIGN")
            assert captured["snapshot"]["adapter_key"] == "openai_compatible"
            assert captured["payload"]["commerce_mainline"]["script_analysis"]["id"]
            assert captured["payload"]["commerce_mainline"]["product_asset_version"]["id"]
        finally:
            db = SessionLocal()
            try:
                binding = db.scalar(select(ModelSlotProfileBinding).where(ModelSlotProfileBinding.model_profile_id == profile_id))
                assert binding is not None
                binding.is_enabled = False
                for default in db.scalars(select(ModelSlotProfileBinding).where(ModelSlotProfileBinding.slot_id == binding.slot_id, ModelSlotProfileBinding.model_profile_id != profile_id)):
                    default.is_enabled = True
                db.commit()
            finally:
                db.close()


def test_slice2_versions_are_append_only_and_upstream_regeneration_marks_old_descendants_stale() -> None:
    with TestClient(app) as client:
        story_run_id, _ = _make_story_run(client)
        _operation(client, story_run_id, "CHARACTER_DESIGN")
        first = _assets(client, story_run_id)["character_designs"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/character-designs/{first['id']}/lock")
        _operation(client, story_run_id, "SCENE_DESIGN")
        scene = _assets(client, story_run_id)["scene_designs"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/scene-designs/{scene['id']}/lock")
        _operation(client, story_run_id, "STORYBOARD")
        board = _assets(client, story_run_id)["storyboards"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/storyboards/{board['id']}/lock")
        # 人工重生角色只创建 v2；旧角色/下游分镜留档并带可见的失效状态。
        _operation(client, story_run_id, "CHARACTER_DESIGN", retry=True)
        assets = _assets(client, story_run_id)
        assert {item["version"] for item in assets["character_designs"]} == {1, 2}
        assert next(item for item in assets["character_designs"] if item["id"] == first["id"])["status"] == "LOCKED"
        assert next(item for item in assets["storyboards"] if item["id"] == board["id"])["status"] == "STALE"


def test_slice2_duplicate_active_request_is_idempotent_and_product_versions_never_cross_story_runs() -> None:
    """重复点击不应重复调用模型，且另一冻结商品绝不能借用本 StoryRun 的任务。"""

    with TestClient(app) as client:
        story_run_id, _ = _make_story_run(client)
        db = SessionLocal()
        try:
            first, created_first = create_production_run(
                db, story_run_id=story_run_id, operation="CHARACTER_DESIGN"
            )
            second, created_second = create_production_run(
                db, story_run_id=story_run_id, operation="CHARACTER_DESIGN"
            )
            assert created_first is True
            assert created_second is False
            assert first.id == second.id
            # 服务层的提前查询只用于快速返回；两个请求恰好同时通过该查询时，0018 的
            # 部分唯一索引仍只允许一个活动的同语义生产任务进入数据库。
            duplicate = WorkflowRun(
                project_id=first.project_id,
                workflow_key=first.workflow_key,
                idempotency_key=first.idempotency_key,
                input_snapshot=first.input_snapshot,
                status=RunStatus.PENDING,
            )
            db.add(duplicate)
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()

            story_run = db.get(StoryRun, story_run_id)
            assert story_run is not None
            frozen_product = db.get(ProductAssetVersion, story_run.product_asset_version_id)
            assert frozen_product is not None
            other_product = ProductAsset(name="其他商品", description="隔离测试")
            db.add(other_product); db.flush()
            other_version = ProductAssetVersion(
                product_asset_id=other_product.id,
                source_analysis_version_id=frozen_product.source_analysis_version_id,
                version=1,
                product_name="其他商品", appearance_description="其他包装", status=ProductAssetVersionStatus.CONFIRMED,
            )
            db.add(other_version); db.commit()
            # 数据库存下游外键不会替业务重写冻结快照；服务必须在任务创建时阻止串线。
            story_run.product_asset_version_id = other_version.id
            db.commit()
            try:
                create_production_run(db, story_run_id=story_run_id, operation="CHARACTER_DESIGN", retry=True)
            except Exception as exc:  # FastAPI HTTPException，避免把错误凭空转换成成功任务。
                assert getattr(exc, "status_code", None) == 409
            else:  # pragma: no cover - 防止回归为“读取最新商品”。
                raise AssertionError("冻结商品版本不一致时不应创建角色任务")
        finally:
            db.close()


def test_slice2_rejects_unknown_product_effect_and_never_creates_storyboard(monkeypatch) -> None:
    """Fake 导演 Adapter 走真实执行分支时，未知功效会在结构化校验处失败。"""

    with TestClient(app) as client:
        story_run_id, _ = _make_story_run(client)
        _operation(client, story_run_id, "CHARACTER_DESIGN")
        character = _assets(client, story_run_id)["character_designs"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/character-designs/{character['id']}/lock")
        _operation(client, story_run_id, "SCENE_DESIGN")
        scene = _assets(client, story_run_id)["scene_designs"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/scene-designs/{scene['id']}/lock")

        original = commerce_production_service._run_text_model

        def toxic_storyboard(run_row, *, operation, system_suffix, output_contract, user_payload):
            if operation != "STORYBOARD":
                return original(run_row, operation=operation, system_suffix=system_suffix, output_contract=output_contract, user_payload=user_payload)
            shots = commerce_production_service._mock_shots(
                user_payload["character_design"]["content"]["roles"],
                user_payload["scene_design"]["content"]["scenes"],
                user_payload["outline"]["integration_nodes"],
            )
            shots[1]["product_action"] = "使用后立刻见效并治愈所有问题"
            return {"shots": shots}

        monkeypatch.setattr(commerce_production_service, "_run_text_model", toxic_storyboard)
        response = client.post(f"/api/v1/commerce/story-runs/{story_run_id}/production/STORYBOARD", json={})
        assert response.status_code == 202
        db = SessionLocal()
        try:
            run = db.get(WorkflowRun, response.json()["id"])
            assert run is not None and run.status.value == "FAILED"
            assert "未确认的商品功效" in (run.steps[0].error_message or "")
        finally:
            db.close()
        assert not _assets(client, story_run_id)["storyboards"]


def test_slice2_invalid_mp4_is_rejected_before_clip_can_be_reviewed_or_composed(monkeypatch) -> None:
    """供应商地址不是可读 MP4 时，绝不能被标记成功并流入 FFmpeg 成片。"""

    import shutil

    monkeypatch.setattr(shutil, "which", lambda command: "/fake/ffprobe" if command == "ffprobe" else None)

    def unreadable(*_args, **_kwargs):
        raise CalledProcessError(returncode=1, cmd=["ffprobe"])

    monkeypatch.setattr(commerce_production_service, "run", unreadable)
    with pytest.raises(RuntimeError, match="ffprobe 无法读取供应商返回的视频 MP4"):
        commerce_production_service._probe_remote_mp4("https://cdn.example.invalid/bad.mp4")


def test_slice2_restarted_video_worker_only_polls_saved_provider_task(monkeypatch) -> None:
    """供应商任务号已落库时，重启 Worker 只能 poll，绝不能再次 submit。"""

    with TestClient(app) as client:
        story_run_id, _ = _make_story_run(client)
        _operation(client, story_run_id, "CHARACTER_DESIGN")
        character = _assets(client, story_run_id)["character_designs"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/character-designs/{character['id']}/lock")
        _operation(client, story_run_id, "SCENE_DESIGN")
        scene = _assets(client, story_run_id)["scene_designs"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/scene-designs/{scene['id']}/lock")
        _operation(client, story_run_id, "STORYBOARD")
        board = _assets(client, story_run_id)["storyboards"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/storyboards/{board['id']}/lock")
        _operation(client, story_run_id, "CHARACTER_IMAGES")
        for image in _assets(client, story_run_id)["character_images"]:
            _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/images/CHARACTER/{image['id']}/lock")
        _operation(client, story_run_id, "SCENE_IMAGES")
        for image in _assets(client, story_run_id)["scene_images"]:
            _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/images/SCENE/{image['id']}/lock")
        shot = board["content"]["shots"][0]
        _operation(client, story_run_id, "SHOT_KEYFRAME", target_id=shot["shot_id"])
        frame = _assets(client, story_run_id)["keyframes"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/images/KEYFRAME/{frame['id']}/lock")
        _operation(client, story_run_id, "VIDEO_PROMPT", target_id=shot["shot_id"])

        db = SessionLocal()
        try:
            slot = db.scalar(select(ModelSlot).where(ModelSlot.slot_key == "VIDEO_GENERATE"))
            assert slot is not None
            defaults = list(db.scalars(select(ModelSlotProfileBinding).where(ModelSlotProfileBinding.slot_id == slot.id)).all())
            default_states = {binding.id: binding.is_enabled for binding in defaults}
            for binding in defaults:
                binding.is_enabled = False
            profile = ModelProfile(
                step_key="VIDEO_GENERATE", provider_key="configurable_async_video", adapter_key="configurable_async_video",
                model_key="restart-fake", model_version="restart-fake", display_name="Restart Fake", version=996,
                provider_config={"secret_env_name": "DECOY"}, is_active=False, profile_status="ACTIVE",
            )
            db.add(profile)
            db.flush()
            real_binding = ModelSlotProfileBinding(slot_id=slot.id, model_profile_id=profile.id, is_enabled=True, priority=-100)
            db.add(real_binding)
            db.commit()

            run, created = create_production_run(
                db, story_run_id=story_run_id, operation="VIDEO_RENDER", target_id=shot["shot_id"]
            )
            assert created is True
            step = run.steps[0]
            context = run.input_snapshot["commerce_production"]
            prompt = db.scalar(
                select(CommerceVideoPromptVersion).where(
                    CommerceVideoPromptVersion.storyboard_version_id == context["storyboard"]["id"],
                    CommerceVideoPromptVersion.shot_id == shot["shot_id"],
                    CommerceVideoPromptVersion.status == "LOCKED",
                )
            )
            assert prompt is not None
            clip = CommerceVideoClipVersion(
                story_run_id=story_run_id,
                storyboard_version_id=context["storyboard"]["id"],
                shot_id=shot["shot_id"],
                shot_number=shot["shot_number"],
                keyframe_version_id=prompt.keyframe_version_id,
                video_prompt_version_id=prompt.id,
                workflow_run_id=run.id,
                version=1,
                idempotency_key=f"clip:{step.idempotency_key}",
                provider_task_id="already-submitted-task",
                input_asset_snapshot={"restart_test": True},
                status="RUNNING",
            )
            db.add(clip)
            step.status = RunStatus.RUNNING
            step.provider_task_id = clip.provider_task_id
            run.status = RunStatus.RUNNING
            db.commit()
            run_id, profile_id, default_ids = run.id, profile.id, [item.id for item in defaults]
        finally:
            db.close()

        class ExistingTaskProvider:
            def submit(self, _request):  # pragma: no cover - 调用即代表会重复扣费。
                raise AssertionError("已有 provider_task_id 时不得重新提交")

            def poll(self, task_id: str) -> VideoTaskResult:
                assert task_id == "already-submitted-task"
                return VideoTaskResult(provider_task_id=task_id, status="SUCCEEDED", video_url="https://cdn.example.invalid/clip.mp4")

        monkeypatch.setattr(commerce_production_service, "video_provider", lambda _snapshot: ExistingTaskProvider())
        monkeypatch.setattr(
            commerce_production_service.local_asset_storage,
            "download_generated_video",
            lambda _url: ("video/mp4", b"\x00\x00\x00\x18ftypisomtest-mp4"),
        )
        monkeypatch.setattr(
            commerce_production_service.local_asset_storage,
            "save_generated_video_bytes",
            lambda **_kwargs: "/media/generated/projects/project-1/commerce-video/test/v1.mp4",
        )
        monkeypatch.setattr(
            commerce_production_service.local_asset_storage,
            "generated_media_path",
            lambda _url: __import__("pathlib").Path("/tmp/fake-restarted.mp4"),
        )
        monkeypatch.setattr(
            commerce_production_service,
            "_probe_mp4",
            lambda _path: {"duration_ms": 6000, "width": 854, "height": 480, "frame_rate": 24.0, "video_codec": "h264", "audio_track_count": 0},
        )
        try:
            commerce_production_service.execute_commerce_production_workflow(run_id)
            db = SessionLocal()
            try:
                run = db.get(WorkflowRun, run_id)
                clip = db.scalar(select(CommerceVideoClipVersion).where(CommerceVideoClipVersion.workflow_run_id == run_id))
                assert run is not None and run.status == RunStatus.SUCCEEDED
                assert clip is not None and clip.status == "SUCCEEDED"
                assert clip.provider_task_id == "already-submitted-task"
            finally:
                db.close()
        finally:
            db = SessionLocal()
            try:
                binding = db.scalar(select(ModelSlotProfileBinding).where(ModelSlotProfileBinding.model_profile_id == profile_id))
                assert binding is not None
                binding.is_enabled = False
                for default in db.scalars(select(ModelSlotProfileBinding).where(ModelSlotProfileBinding.id.in_(default_ids))):
                    default.is_enabled = True
                db.commit()
            finally:
                db.close()


def test_video_submit_failure_preserves_audit_without_persisting_data_url(monkeypatch) -> None:
    """供应商创建拒绝也必须保留可重试的失败片段与脱敏调用审计。"""

    with TestClient(app) as client:
        story_run_id, _ = _make_story_run(client)
        _operation(client, story_run_id, "CHARACTER_DESIGN")
        character = _assets(client, story_run_id)["character_designs"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/character-designs/{character['id']}/lock")
        _operation(client, story_run_id, "SCENE_DESIGN")
        scene = _assets(client, story_run_id)["scene_designs"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/scene-designs/{scene['id']}/lock")
        _operation(client, story_run_id, "STORYBOARD")
        board = _assets(client, story_run_id)["storyboards"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/storyboards/{board['id']}/lock")
        _operation(client, story_run_id, "CHARACTER_IMAGES")
        for image in _assets(client, story_run_id)["character_images"]:
            _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/images/CHARACTER/{image['id']}/lock")
        _operation(client, story_run_id, "SCENE_IMAGES")
        for image in _assets(client, story_run_id)["scene_images"]:
            _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/images/SCENE/{image['id']}/lock")
        shot = board["content"]["shots"][0]
        _operation(client, story_run_id, "SHOT_KEYFRAME", target_id=shot["shot_id"])
        frame = _assets(client, story_run_id)["keyframes"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/images/KEYFRAME/{frame['id']}/lock")
        _operation(client, story_run_id, "VIDEO_PROMPT", target_id=shot["shot_id"])

        db = SessionLocal()
        try:
            slot = db.scalar(select(ModelSlot).where(ModelSlot.slot_key == "VIDEO_GENERATE"))
            assert slot is not None
            defaults = list(db.scalars(select(ModelSlotProfileBinding).where(ModelSlotProfileBinding.slot_id == slot.id)).all())
            default_states = {binding.id: binding.is_enabled for binding in defaults}
            for binding in defaults:
                binding.is_enabled = False
            profile = ModelProfile(
                step_key="VIDEO_GENERATE", provider_key="volcengine_ark_video", adapter_key="volcengine_ark_video",
                model_key="doubao-seedance-2-5-260628", model_version="doubao-seedance-2-5-260628",
                display_name="Rejected Ark", version=997, is_active=False, profile_status="ACTIVE",
                provider_config={
                    "api_base_url": "https://ark.cn-beijing.volces.com/api/v3",
                    "secret_env_name": "ARK_API_KEY", "ratio": "16:9", "duration": 5,
                    "resolution": "480p", "generate_audio": False,
                },
            )
            db.add(profile); db.flush()
            db.add(ModelSlotProfileBinding(slot_id=slot.id, model_profile_id=profile.id, is_enabled=True, priority=-100))
            db.commit()
            profile_id = profile.id
        finally:
            db.close()

        reference = LocalImageReference(
            asset_id=frame["id"], role="first_frame", mime_type="image/jpeg", width=2848, height=1600,
            sha256="b" * 64, data_url="data:image/jpeg;base64,AAECAwQ=",
        )
        monkeypatch.setattr(local_asset_storage, "load_generated_image_reference", lambda **_kwargs: reference)
        calls: list[object] = []

        class RejectingProvider:
            def submit(self, request):
                calls.append(request)
                assert request.reference_images == [reference]
                raise RuntimeError("供应商拒绝创建：HTTP 404")

            def poll(self, _task_id):  # pragma: no cover - 未获得供应商任务号不应轮询。
                raise AssertionError("创建失败后不得轮询")

        monkeypatch.setattr(commerce_production_service, "video_provider", lambda _snapshot: RejectingProvider())
        try:
            response = client.post(
                f"/api/v1/commerce/story-runs/{story_run_id}/production/VIDEO_RENDER",
                json={"target_id": shot["shot_id"]},
            )
            assert response.status_code == 202, response.text
            run_id = response.json()["id"]
            db = SessionLocal()
            try:
                run = db.get(WorkflowRun, run_id)
                step = db.scalar(select(WorkflowStep).where(WorkflowStep.workflow_run_id == run_id))
                clip = db.scalar(select(CommerceVideoClipVersion).where(CommerceVideoClipVersion.workflow_run_id == run_id))
                invocation = db.scalar(select(ModelInvocation).where(ModelInvocation.workflow_run_id == run_id))
                assert calls and run is not None and run.status == RunStatus.FAILED
                assert step is not None and step.status == RunStatus.FAILED
                assert clip is not None and clip.status == "FAILED" and not clip.provider_task_id
                assert invocation is not None and invocation.status == RunStatus.FAILED and not invocation.provider_task_id
                assert invocation.input_snapshot["first_frame_asset"]["asset_id"] == frame["id"]
                assert "data:image" not in str(invocation.input_snapshot)
            finally:
                db.close()
        finally:
            db = SessionLocal()
            try:
                binding = db.scalar(select(ModelSlotProfileBinding).where(ModelSlotProfileBinding.model_profile_id == profile_id))
                assert binding is not None
                binding.is_enabled = False
                for default in db.scalars(select(ModelSlotProfileBinding).where(ModelSlotProfileBinding.id.in_(default_states))):
                    default.is_enabled = default_states[default.id]
                db.commit()
            finally:
                db.close()


def test_slice2_failed_shot_retries_only_that_shot_without_restarting_story_run(monkeypatch) -> None:
    """一个镜头失败后只为它创建新视频任务，不重跑其他已完成镜头。"""

    with TestClient(app) as client:
        story_run_id, _ = _make_story_run(client)
        _operation(client, story_run_id, "CHARACTER_DESIGN")
        character = _assets(client, story_run_id)["character_designs"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/character-designs/{character['id']}/lock")
        _operation(client, story_run_id, "SCENE_DESIGN")
        scene = _assets(client, story_run_id)["scene_designs"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/scene-designs/{scene['id']}/lock")
        _operation(client, story_run_id, "STORYBOARD")
        board = _assets(client, story_run_id)["storyboards"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/storyboards/{board['id']}/lock")
        _operation(client, story_run_id, "CHARACTER_IMAGES")
        for image in _assets(client, story_run_id)["character_images"]:
            _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/images/CHARACTER/{image['id']}/lock")
        _operation(client, story_run_id, "SCENE_IMAGES")
        for image in _assets(client, story_run_id)["scene_images"]:
            _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/images/SCENE/{image['id']}/lock")
        shot = board["content"]["shots"][0]
        _operation(client, story_run_id, "SHOT_KEYFRAME", target_id=shot["shot_id"])
        frame = _assets(client, story_run_id)["keyframes"][0]
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/images/KEYFRAME/{frame['id']}/lock")
        _operation(client, story_run_id, "VIDEO_PROMPT", target_id=shot["shot_id"])

        original = commerce_production_service._execute_video_render

        def fail_once(*_args, **_kwargs):
            raise RuntimeError("受控的单镜供应商失败")

        monkeypatch.setattr(commerce_production_service, "_execute_video_render", fail_once)
        failed = client.post(
            f"/api/v1/commerce/story-runs/{story_run_id}/production/VIDEO_RENDER",
            json={"target_id": shot["shot_id"]},
        )
        assert failed.status_code == 202, failed.text
        db = SessionLocal()
        try:
            failed_run = db.get(WorkflowRun, failed.json()["id"])
            assert failed_run is not None and failed_run.status == RunStatus.FAILED
            assert not db.scalars(select(CommerceVideoClipVersion).where(CommerceVideoClipVersion.story_run_id == story_run_id)).all()
        finally:
            db.close()

        monkeypatch.setattr(commerce_production_service, "_execute_video_render", original)
        retried = _operation(client, story_run_id, "VIDEO_RENDER", target_id=shot["shot_id"], retry=True)
        assert retried["id"] != failed.json()["id"]
        assets = _assets(client, story_run_id)
        clips = [item for item in assets["clips"] if item["shot_id"] == shot["shot_id"]]
        assert len(clips) == 1 and clips[0]["status"] == "SUCCEEDED"
        assert all(item["shot_id"] == shot["shot_id"] for item in clips)


def _prepare_failed_ark_video_clip(client: TestClient) -> dict[str, str]:
    """创建一条已有方舟任务号、但因本地轮询失败的历史片段，完全不调用真实供应商。"""

    story_run_id, _ = _make_story_run(client)
    _operation(client, story_run_id, "CHARACTER_DESIGN")
    character = _assets(client, story_run_id)["character_designs"][0]
    _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/character-designs/{character['id']}/lock")
    _operation(client, story_run_id, "SCENE_DESIGN")
    scene = _assets(client, story_run_id)["scene_designs"][0]
    _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/scene-designs/{scene['id']}/lock")
    _operation(client, story_run_id, "STORYBOARD")
    board = _assets(client, story_run_id)["storyboards"][0]
    _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/storyboards/{board['id']}/lock")
    _operation(client, story_run_id, "CHARACTER_IMAGES")
    for image in _assets(client, story_run_id)["character_images"]:
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/images/CHARACTER/{image['id']}/lock")
    _operation(client, story_run_id, "SCENE_IMAGES")
    for image in _assets(client, story_run_id)["scene_images"]:
        _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/images/SCENE/{image['id']}/lock")
    shot = board["content"]["shots"][0]
    _operation(client, story_run_id, "SHOT_KEYFRAME", target_id=shot["shot_id"])
    keyframe = _assets(client, story_run_id)["keyframes"][0]
    _lock(client, f"/api/v1/commerce/story-runs/{story_run_id}/images/KEYFRAME/{keyframe['id']}/lock")
    _operation(client, story_run_id, "VIDEO_PROMPT", target_id=shot["shot_id"])

    db = SessionLocal()
    try:
        story = db.get(StoryRun, story_run_id)
        slot = db.scalar(select(ModelSlot).where(ModelSlot.slot_key == "VIDEO_GENERATE"))
        assert story is not None and slot is not None
        profile = ModelProfile(
            step_key="VIDEO_GENERATE",
            provider_key="volcengine_ark_video",
            adapter_key="volcengine_ark_video",
            model_key="doubao-seedance-2-5-260628",
            model_version="resume-test",
            display_name="Resume existing Ark task",
            version=9100,
            provider_config={
                "api_base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "secret_env_name": "ARK_API_KEY",
                "duration": 5,
                "resolution": "480p",
                "generate_audio": False,
                "poll_interval_seconds": 1,
                "poll_network_retry_count": 0,
            },
            is_active=False,
            profile_status="ACTIVE",
        )
        db.add(profile)
        db.flush()
        source_run, created = create_production_run(
            db, story_run_id=story_run_id, operation="VIDEO_RENDER", target_id=shot["shot_id"]
        )
        assert created is True
        source_step = db.scalar(select(WorkflowStep).where(WorkflowStep.workflow_run_id == source_run.id))
        context = source_run.input_snapshot["commerce_production"]
        prompt = db.scalar(
            select(CommerceVideoPromptVersion).where(
                CommerceVideoPromptVersion.storyboard_version_id == context["storyboard"]["id"],
                CommerceVideoPromptVersion.shot_id == shot["shot_id"],
                CommerceVideoPromptVersion.status == "LOCKED",
            )
        )
        assert source_step is not None and prompt is not None
        binding = {
            "slot_id": slot.id,
            "slot_key": "VIDEO_GENERATE",
            "model_profile_id": profile.id,
            "profile_snapshot": {
                "profile_id": profile.id,
                "adapter_key": "volcengine_ark_video",
                "provider_key": "volcengine_ark_video",
                "model_key": "doubao-seedance-2-5-260628",
                "model_version": "resume-test",
                "version": profile.version,
                "provider_config": dict(profile.provider_config),
            },
        }
        source_snapshot = dict(source_run.input_snapshot)
        source_snapshot["model_binding"] = binding
        source_run.input_snapshot = source_snapshot
        source_step.model_profile_snapshot = {
            "binding": binding,
            "prompt_template": source_snapshot["prompt_template"],
        }
        source_run.status = RunStatus.FAILED
        source_step.status = RunStatus.FAILED
        source_step.provider_task_id = "existing-ark-task"
        invocation = ModelInvocation(
            project_id=story.project_id,
            workflow_run_id=source_run.id,
            workflow_step_id=source_step.id,
            model_slot_id=slot.id,
            model_profile_id=profile.id,
            prompt_template_id=source_snapshot["prompt_template"]["id"],
            task_type="VIDEO_GENERATE",
            model_profile_snapshot=dict(binding["profile_snapshot"]),
            prompt_snapshot=dict(source_snapshot["prompt_template"]),
            input_snapshot={"first_frame_asset": {"asset_id": keyframe["id"], "role": "first_frame"}},
            output_reference={"failure": {"code": "PROVIDER_POLL_NETWORK_ERROR"}},
            provider_task_id="existing-ark-task",
            idempotency_key=f"source-video-invocation:{source_step.id}",
            status=RunStatus.FAILED,
            error_code="PROVIDER_POLL_NETWORK_ERROR",
        )
        db.add(invocation)
        db.flush()
        clip = CommerceVideoClipVersion(
            story_run_id=story.id,
            storyboard_version_id=context["storyboard"]["id"],
            shot_id=shot["shot_id"],
            shot_number=shot["shot_number"],
            keyframe_version_id=prompt.keyframe_version_id,
            video_prompt_version_id=prompt.id,
            workflow_run_id=source_run.id,
            model_invocation_id=invocation.id,
            version=1,
            idempotency_key=f"source-video-clip:{source_step.id}",
            provider_task_id="existing-ark-task",
            input_asset_snapshot={"first_frame_asset": {"asset_id": keyframe["id"], "role": "first_frame"}},
            status="FAILED",
            error_message="供应商任务轮询暂时无法连接，可使用已保存的任务号恢复查询",
        )
        db.add(clip)
        db.commit()
        return {
            "story_run_id": story.id,
            "source_run_id": source_run.id,
            "source_step_id": source_step.id,
            "source_invocation_id": invocation.id,
            "source_clip_id": clip.id,
            "profile_id": profile.id,
        }
    finally:
        db.close()


def test_resume_existing_provider_task_never_submits_again_and_preserves_old_failure(monkeypatch) -> None:
    """恢复已有方舟成功任务只 GET/下载，且新旧本地 attempt 与审计清晰隔离。"""

    with TestClient(app) as client:
        source = _prepare_failed_ark_video_clip(client)
        db = SessionLocal()
        try:
            resumed, created = resume_video_clip_provider_task(
                db, story_run_id=source["story_run_id"], source_clip_id=source["source_clip_id"]
            )
            duplicate, duplicate_created = resume_video_clip_provider_task(
                db, story_run_id=source["story_run_id"], source_clip_id=source["source_clip_id"]
            )
            assert created is True
            assert duplicate_created is False
            assert duplicate.id == resumed.id
            recovery = resumed.input_snapshot["provider_task_recovery"]
            assert recovery["execution_mode"] == "resume_provider_task"
            assert recovery["recovered_provider_task_id"] == "existing-ark-task"
            assert recovery["provider_create_post_count"] == 0
            assert recovery["recovered_from_video_clip_id"] == source["source_clip_id"]
            assert recovery["source_workflow_run_id"] == source["source_run_id"]
            assert recovery["source_workflow_step_id"] == source["source_step_id"]
            assert recovery["source_model_invocation_id"] == source["source_invocation_id"]
            assert recovery["source_video_prompt_version_id"]
            assert recovery["source_keyframe_version_id"]
            assert recovery["source_storyboard_version_id"]
            assert recovery["source_shot_id"]
            assert recovery["recovery_attempt"] == 1
            resumed_id = resumed.id
        finally:
            db.close()

        calls: list[str] = []

        class ExistingTaskProvider:
            def submit(self, _request):  # pragma: no cover - 调用即代表违反“恢复不得 POST”。
                raise AssertionError("恢复已有 provider_task_id 时不得创建新视频任务")

            def poll(self, task_id: str) -> VideoTaskResult:
                calls.append(task_id)
                if len(calls) == 1:
                    return VideoTaskResult(provider_task_id=task_id, status="PENDING")
                return VideoTaskResult(
                    provider_task_id=task_id,
                    status="SUCCEEDED",
                    video_url="https://cdn.example.invalid/recovered.mp4?signature=temporary",
                )

        monkeypatch.setattr(commerce_production_service, "video_provider", lambda _snapshot: ExistingTaskProvider())
        monkeypatch.setattr("app.services.v1_model_adapter_service.time.sleep", lambda _seconds: None)
        monkeypatch.setattr(
            commerce_production_service.local_asset_storage,
            "download_generated_video",
            lambda _url: ("video/mp4", b"\x00\x00\x00\x18ftypisomtest-mp4"),
        )
        monkeypatch.setattr(
            commerce_production_service.local_asset_storage,
            "save_generated_video_bytes",
            lambda **_kwargs: "/media/generated/projects/project-1/commerce-video/recovered/v2.mp4",
        )
        monkeypatch.setattr(
            commerce_production_service.local_asset_storage,
            "generated_media_path",
            lambda _url: Path("/tmp/recovered-video.mp4"),
        )
        monkeypatch.setattr(
            commerce_production_service,
            "_probe_mp4",
            lambda _path: {"duration_ms": 5000, "width": 854, "height": 480, "frame_rate": 24.0, "video_codec": "h264", "audio_track_count": 0},
        )

        commerce_production_service.execute_commerce_production_workflow(resumed_id)
        assert calls == ["existing-ark-task", "existing-ark-task"]
        db = SessionLocal()
        try:
            old_run = db.get(WorkflowRun, source["source_run_id"])
            old_step = db.get(WorkflowStep, source["source_step_id"])
            old_invocation = db.get(ModelInvocation, source["source_invocation_id"])
            old_clip = db.get(CommerceVideoClipVersion, source["source_clip_id"])
            resumed_run = db.get(WorkflowRun, resumed_id)
            resumed_step = db.scalar(select(WorkflowStep).where(WorkflowStep.workflow_run_id == resumed_id))
            resumed_invocation = db.scalar(select(ModelInvocation).where(ModelInvocation.workflow_run_id == resumed_id))
            resumed_clip = db.scalar(select(CommerceVideoClipVersion).where(CommerceVideoClipVersion.workflow_run_id == resumed_id))
            assert old_run.status == RunStatus.FAILED
            assert old_step.status == RunStatus.FAILED
            assert old_invocation.status == RunStatus.FAILED
            assert old_clip.status == "FAILED" and old_clip.video_url is None
            assert resumed_run.status == RunStatus.SUCCEEDED
            assert resumed_step.status == RunStatus.SUCCEEDED
            assert resumed_invocation.status == RunStatus.SUCCEEDED
            assert resumed_clip.status == "SUCCEEDED"
            assert resumed_clip.id != old_clip.id
            assert resumed_clip.provider_task_id == old_clip.provider_task_id == "existing-ark-task"
            assert resumed_clip.input_asset_snapshot["provider_task_recovery"]["provider_create_post_count"] == 0
            assert resumed_invocation.input_snapshot["provider_task_recovery"]["recovered_from_video_clip_id"] == old_clip.id
            assert "data:image" not in str(resumed_run.input_snapshot)
            assert "signature=" not in str(resumed_clip.media_metadata)
            same, same_created = resume_video_clip_provider_task(
                db, story_run_id=source["story_run_id"], source_clip_id=source["source_clip_id"]
            )
            assert same_created is False and same.id == resumed_id
        finally:
            db.close()
        repeated_endpoint = client.post(
            f"/api/v1/commerce/story-runs/{source['story_run_id']}/clips/{source['source_clip_id']}/resume-provider-task"
        )
        assert repeated_endpoint.status_code == 202, repeated_endpoint.text
        assert repeated_endpoint.json()["id"] == resumed_id


def test_poll_network_failure_uses_recoverable_error_code_without_second_submit(monkeypatch) -> None:
    """全部 GET 重试失败时保留 task ID，标记可人工恢复的安全错误码。"""

    from app.services.analysis_provider import ProviderPollNetworkError

    with TestClient(app) as client:
        source = _prepare_failed_ark_video_clip(client)
        db = SessionLocal()
        try:
            resumed, created = resume_video_clip_provider_task(
                db, story_run_id=source["story_run_id"], source_clip_id=source["source_clip_id"]
            )
            assert created is True
            resumed_id = resumed.id
        finally:
            db.close()

        class NetworkFailureProvider:
            def submit(self, _request):  # pragma: no cover - 再次 POST 必须立即暴露。
                raise AssertionError("恢复不得调用 submit")

            def poll(self, task_id: str) -> VideoTaskResult:
                assert task_id == "existing-ark-task"
                raise ProviderPollNetworkError("temporary")

        monkeypatch.setattr(commerce_production_service, "video_provider", lambda _snapshot: NetworkFailureProvider())
        monkeypatch.setattr("app.services.v1_model_adapter_service.time.sleep", lambda _seconds: None)
        commerce_production_service.execute_commerce_production_workflow(resumed_id)

        db = SessionLocal()
        try:
            run = db.get(WorkflowRun, resumed_id)
            step = db.scalar(select(WorkflowStep).where(WorkflowStep.workflow_run_id == resumed_id))
            invocation = db.scalar(select(ModelInvocation).where(ModelInvocation.workflow_run_id == resumed_id))
            clip = db.scalar(select(CommerceVideoClipVersion).where(CommerceVideoClipVersion.workflow_run_id == resumed_id))
            assert run.status == RunStatus.FAILED
            assert step.status == RunStatus.FAILED and step.provider_task_id == "existing-ark-task"
            assert invocation.status == RunStatus.FAILED
            assert invocation.error_code == "PROVIDER_POLL_NETWORK_ERROR"
            assert invocation.provider_task_id == "existing-ark-task"
            assert clip.status == "FAILED" and clip.provider_task_id == "existing-ark-task"
            assert "恢复查询" in (step.error_message or "")
        finally:
            db.close()


@pytest.mark.parametrize(
    ("provider_result", "download_fails", "expected_code"),
    [
        (VideoTaskResult(provider_task_id="existing-ark-task", status="FAILED", error_message="supplier terminal failure"), False, "PROVIDER_TASK_FAILED"),
        (VideoTaskResult(provider_task_id="existing-ark-task", status="SUCCEEDED", video_url="https://cdn.example.invalid/result.mp4?signature=temporary"), True, "VIDEO_DOWNLOAD_FAILED"),
    ],
)
def test_resume_distinguishes_supplier_terminal_failure_from_download_failure(
    monkeypatch, provider_result: VideoTaskResult, download_fails: bool, expected_code: str
) -> None:
    """已提交任务的供应商终态失败与后续下载失败必须在审计中可区分。"""

    with TestClient(app) as client:
        source = _prepare_failed_ark_video_clip(client)
        db = SessionLocal()
        try:
            resumed, created = resume_video_clip_provider_task(
                db, story_run_id=source["story_run_id"], source_clip_id=source["source_clip_id"]
            )
            assert created is True
            resumed_id = resumed.id
        finally:
            db.close()

        class ResultProvider:
            def submit(self, _request):  # pragma: no cover - 恢复路径不允许 POST。
                raise AssertionError("恢复不得调用 submit")

            def poll(self, task_id: str) -> VideoTaskResult:
                assert task_id == "existing-ark-task"
                return provider_result

        monkeypatch.setattr(commerce_production_service, "video_provider", lambda _snapshot: ResultProvider())
        if download_fails:
            monkeypatch.setattr(
                commerce_production_service.local_asset_storage,
                "download_generated_video",
                lambda _url: (_ for _ in ()).throw(RuntimeError("download transport error")),
            )
        commerce_production_service.execute_commerce_production_workflow(resumed_id)

        db = SessionLocal()
        try:
            invocation = db.scalar(select(ModelInvocation).where(ModelInvocation.workflow_run_id == resumed_id))
            clip = db.scalar(select(CommerceVideoClipVersion).where(CommerceVideoClipVersion.workflow_run_id == resumed_id))
            assert invocation is not None and invocation.error_code == expected_code
            assert clip is not None and clip.status == "FAILED"
            assert clip.provider_task_id == "existing-ark-task"
        finally:
            db.close()


def test_concurrent_resume_requests_share_one_active_recovery_attempt() -> None:
    """两个并发恢复请求只能预留同一个本地 attempt，尚未执行时也不会有重复 Clip。"""

    with TestClient(app) as client:
        source = _prepare_failed_ark_video_clip(client)

        def request_resume() -> tuple[str, bool]:
            db = SessionLocal()
            try:
                run, created = resume_video_clip_provider_task(
                    db, story_run_id=source["story_run_id"], source_clip_id=source["source_clip_id"]
                )
                return run.id, created
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _index: request_resume(), range(2)))
        assert {run_id for run_id, _created in responses}.__len__() == 1
        assert sum(1 for _run_id, created in responses if created) == 1

        db = SessionLocal()
        try:
            recovery_runs = commerce_production_service._recovery_runs_for_clip(
                db, source_clip_id=source["source_clip_id"]
            )
            assert len(recovery_runs) == 1
            assert recovery_runs[0].status == RunStatus.PENDING
            assert not db.scalars(
                select(CommerceVideoClipVersion).where(
                    CommerceVideoClipVersion.workflow_run_id == recovery_runs[0].id
                )
            ).all()
        finally:
            db.close()
