"""Commerce Slice 1：V1 分析、商品冻结、十创意和 StoryRun 大纲的回归测试。"""

from __future__ import annotations

from base64 import b64decode
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm.attributes import flag_modified

from app.core.database import SessionLocal
from app.main import app
from app.models import (
    CommerceCreativeBatch,
    CommerceCreativeIdea,
    CommerceReferenceIntake,
    CommerceStoryRunInput,
    ModelInvocation,
    ModelProfile,
    ModelSlot,
    ModelSlotProfileBinding,
    ProductAssetVersion,
    ProductAssetVersionStatus,
    ScriptAnalysisVersion,
    StoryOutlineVersion,
    StoryRun,
    WorkflowRun,
)


def _video() -> bytes:
    return b64decode((Path(__file__).parent / "fixtures" / "real-video.mp4.base64").read_text(encoding="ascii"))


def _create_project_and_analysis(client: TestClient) -> tuple[str, dict]:
    project_id = client.post("/api/v1/projects", json={"title": "Commerce Slice 1"}).json()["id"]
    uploaded = client.post(
        f"/api/v1/projects/{project_id}/source-video",
        files={"file": ("reference.mp4", _video(), "video/mp4")},
    )
    assert uploaded.status_code == 201, uploaded.text
    generated = client.post(
        f"/api/v1/production/projects/{project_id}/generation-runs/reference_analysis",
        json={"source_asset_id": uploaded.json()["id"]},
    )
    assert generated.status_code == 202, generated.text
    analysis = client.get(f"/api/v1/production/projects/{project_id}/reference-analyses").json()[0]
    assert client.post(
        f"/api/v1/production/reference-analyses/{analysis['id']}/lock",
        json={"reviewer_label": "测试制作人"},
    ).status_code == 200
    intake = client.get(f"/api/v1/production/projects/{project_id}/commerce-reference-intakes").json()[0]
    return project_id, intake


def _confirm_product(client: TestClient, intake: dict) -> dict:
    response = client.post(
        f"/api/v1/production/commerce-reference-intakes/{intake['id']}/confirm-product",
        json={
            "reviewer_label": "测试制作人",
            "product_name": "已确认的测试商品",
            "appearance_description": "白色简洁包装",
            "selling_points": [{"claim": "仅测试确认的卖点"}],
            "usage_scenarios": [{"scene": "家庭桌面"}],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _generate_ideas(client: TestClient, project_id: str) -> dict:
    response = client.post(
        f"/api/v1/production/projects/{project_id}/generation-runs/commerce_creative_generation",
        json={},
    )
    assert response.status_code == 202, response.text
    batches = client.get(f"/api/v1/production/projects/{project_id}/commerce-creative-batches")
    assert batches.status_code == 200, batches.text
    return batches.json()[0]


def test_reference_analysis_automatically_creates_script_analysis_and_product_draft() -> None:
    with TestClient(app) as client:
        project_id, intake = _create_project_and_analysis(client)
        assert intake["project_id"] == project_id
        assert intake["product_status"] == "DRAFT"
        db = SessionLocal()
        try:
            script = db.get(ScriptAnalysisVersion, intake["script_analysis_version_id"])
            product = db.get(ProductAssetVersion, intake["product_asset_version_id"])
            assert script is not None and script.analysis_status.value == "SUCCEEDED"
            assert product is not None and product.status == ProductAssetVersionStatus.DRAFT
            assert script.raw_analysis["reference_analysis"]["id"] == intake["reference_analysis_id"]
        finally:
            db.close()


def test_unconfirmed_product_cannot_generate_ideas_then_fixed_batch_uses_frozen_versions() -> None:
    with TestClient(app) as client:
        project_id, intake = _create_project_and_analysis(client)
        blocked = client.post(
            f"/api/v1/production/projects/{project_id}/generation-runs/commerce_creative_generation", json={}
        )
        assert blocked.status_code == 409
        assert "确认并冻结" in blocked.json()["detail"]
        confirmed = _confirm_product(client, intake)
        batch = _generate_ideas(client, project_id)
        assert batch["status"] == "SUCCEEDED"
        assert len(batch["ideas"]) == 10
        assert [item["candidate_number"] for item in batch["ideas"]] == list(range(1, 11))
        frozen = batch["input_snapshot"]
        assert frozen["script_analysis"]["id"] == intake["script_analysis_version_id"]
        assert frozen["product_asset_version"]["id"] == confirmed["product_asset_version_id"]
        assert frozen["product_asset_version"]["frozen_at"]


def test_regeneration_preserves_old_batch_and_selecting_idea_creates_frozen_story_run_and_outline() -> None:
    with TestClient(app) as client:
        project_id, intake = _create_project_and_analysis(client)
        _confirm_product(client, intake)
        first = _generate_ideas(client, project_id)
        second = _generate_ideas(client, project_id)
        assert second["batch_number"] == first["batch_number"] + 1
        assert len(client.get(f"/api/v1/production/projects/{project_id}/commerce-creative-batches").json()) == 2

        selected = client.post(
            f"/api/v1/production/commerce-creative-ideas/{second['ideas'][0]['id']}/select",
            json={"reviewer_label": "测试制作人", "mode": "STEPWISE"},
        )
        assert selected.status_code == 201, selected.text
        story_run_id = selected.json()["story_run_id"]
        db = SessionLocal()
        try:
            story_run = db.get(StoryRun, story_run_id)
            linkage = db.get(CommerceStoryRunInput, story_run_id)
            assert story_run is not None and linkage is not None
            assert linkage.creative_idea_id == second["ideas"][0]["id"]
            assert linkage.product_asset_version_id == intake["product_asset_version_id"]
            assert linkage.input_snapshot["creative_idea"]["id"] == second["ideas"][0]["id"]
            # BackgroundTask 已完成 OUTLINE：真实 Commerce 工作流消费其冻结主链输入。
            outlines = list(db.scalars(select(StoryOutlineVersion).where(StoryOutlineVersion.story_run_id == story_run_id)))
            assert outlines
            assert outlines[0].product_placement_strategy["creative_idea_id"] == second["ideas"][0]["id"]
        finally:
            db.close()


def test_creative_selection_never_cross_binds_product_between_projects() -> None:
    with TestClient(app) as client:
        project_a, intake_a = _create_project_and_analysis(client)
        _confirm_product(client, intake_a)
        batch_a = _generate_ideas(client, project_a)
        project_b, intake_b = _create_project_and_analysis(client)
        _confirm_product(client, intake_b)
        _generate_ideas(client, project_b)
        db = SessionLocal()
        try:
            idea = db.get(CommerceCreativeIdea, batch_a["ideas"][0]["id"])
            batch = db.get(CommerceCreativeBatch, idea.batch_id)
            assert idea is not None and batch is not None
            # 人为破坏批次快照模拟跨产品请求，选择服务必须拒绝，而不是创建串线 StoryRun。
            batch.input_snapshot["product_asset_version"]["id"] = intake_b["product_asset_version_id"]
            flag_modified(batch, "input_snapshot")
            db.commit()
        finally:
            db.close()
        response = client.post(
            f"/api/v1/production/commerce-creative-ideas/{batch_a['ideas'][0]['id']}/select", json={}
        )
        assert response.status_code == 409
        # 生产服务不接受这个破坏后的快照；不能产生跨产品运行。
        db = SessionLocal()
        try:
            assert db.scalar(select(func.count(StoryRun.id)).where(StoryRun.project_id == project_a)) == 0
        finally:
            db.close()


def test_non_mock_creative_generation_uses_adapter_not_hardcoded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake Adapter 覆盖网络入口，验证十创意与正式大纲都走真实 Adapter 分支。"""

    from app.services import commerce_mainline_service, commerce_workflow_service

    with TestClient(app) as client:
        project_id, intake = _create_project_and_analysis(client)
        _confirm_product(client, intake)
        fake_profile_id: str | None = None
        db = SessionLocal()
        try:
            run = db.scalar(select(WorkflowRun).where(WorkflowRun.project_id == project_id, WorkflowRun.workflow_key == "v1_reference_analysis"))
            assert run is not None
            # 把冻结的 StoryGenerate profile 改成兼容真实 Adapter；新任务随后会固化它。
            from app.models import ModelProfile, ModelSlotProfileBinding
            # STORY_GENERATE 默认有三个并行 mock binding。测试只能改一个新建的
            # 高优先级绑定，否则会让旧 V1 测试把第四个真实配置也视为故事候选。
            slot = db.scalar(select(ModelSlot).where(ModelSlot.slot_key == "STORY_GENERATE"))
            assert slot is not None
            profile = ModelProfile(
                step_key="STORY_GENERATE",
                provider_key="openai_compatible",
                adapter_key="openai_compatible",
                model_key="slice1-fake-adapter",
                model_version="slice1-fake-adapter",
                display_name="Slice 1 Fake Adapter",
                version=999,
                provider_config={"base_url": "https://fake.invalid", "secret_env_name": "DECOY"},
                is_active=False,
                profile_status="ACTIVE",
            )
            db.add(profile)
            db.flush()
            binding = ModelSlotProfileBinding(
                slot_id=slot.id,
                model_profile_id=profile.id,
                is_enabled=True,
                priority=-100,
            )
            db.add(binding)
            db.commit()
            fake_profile_id = profile.id
        finally:
            db.close()
        captured: dict[str, dict] = {}

        def fake_creative_generate(snapshot, **kwargs):
            captured["creative"] = {"snapshot": snapshot, "payload": kwargs["user_payload"]}
            return {"ideas": [{"title": f"真实分支 {i}", "opening_hook": "hook", "synopsis": "synopsis", "product_integration": {"method": "SOFT_PROP", "evidence_rule": "冻结事实"}} for i in range(1, 11)]}

        def fake_outline_generate(snapshot, **kwargs):
            captured["outline"] = {"snapshot": snapshot, "payload": kwargs["user_payload"]}
            return {
                "title": "真实 Adapter 大纲",
                "premise": "只使用冻结输入生成的原创故事。",
                "story_beats": [{"beat": "hook", "content": "冻结创意开头"}],
                "product_placement_strategy": {"method": "SOFT_PROP"},
            }

        monkeypatch.setattr(commerce_mainline_service, "generate_structured_text", fake_creative_generate)
        monkeypatch.setattr(commerce_workflow_service, "generate_structured_text", fake_outline_generate)
        try:
            batch = _generate_ideas(client, project_id)
            assert batch["status"] == "SUCCEEDED"
            assert captured["creative"]["payload"]["required_idea_count"] == 10
            assert captured["creative"]["payload"]["frozen_input"]["product_asset_version"]["id"] == intake["product_asset_version_id"]
            selection = client.post(
                f"/api/v1/production/commerce-creative-ideas/{batch['ideas'][0]['id']}/select",
                json={"reviewer_label": "Fake Adapter 测试"},
            )
            assert selection.status_code == 201, selection.text
            frozen_outline = captured["outline"]["payload"]["frozen_input"]
            assert frozen_outline["reference_analysis"]["id"] == intake["reference_analysis_id"]
            assert frozen_outline["script_analysis"]["id"] == intake["script_analysis_version_id"]
            assert frozen_outline["product_asset_version"]["id"] == intake["product_asset_version_id"]
            assert frozen_outline["creative_idea"]["id"] == batch["ideas"][0]["id"]
        finally:
            # 共享测试数据库会在随后运行 V1/Commerce 回归。关闭仅供本例使用的
            # 临时 binding，证明本例不以改变默认模型中心换取“真实分支”覆盖。
            if fake_profile_id is not None:
                db = SessionLocal()
                try:
                    profile = db.get(ModelProfile, fake_profile_id)
                    assert profile is not None
                    binding = db.scalar(
                        select(ModelSlotProfileBinding).where(ModelSlotProfileBinding.model_profile_id == profile.id)
                    )
                    assert binding is not None
                    binding.is_enabled = False
                    db.commit()
                finally:
                    db.close()
