"""Model Lab 的无供应商回归测试。

所有执行都使用仓库既有 ``mock_v1`` Profile；测试显式断言没有真实 Adapter、
HTTP 或 provider create POST 被调用。实验审计仍使用 WorkflowRun/Step/Invocation。
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select

from app.core.database import SessionLocal, init_database
from app.main import app
from app.models import (
    ModelExperiment,
    ModelExperimentEvaluation,
    ModelExperimentStatus,
    ModelExperimentVariant,
    ModelInvocation,
    ModelProfile,
    ModelSlot,
    ModelSlotProfileBinding,
    CommerceWorkflowPresetDefinition,
    CharacterDefinition,
    CharacterReferenceImage,
    DesignStatus,
    DirectorPlan,
    DirectorPlanStatus,
    Project,
    PromptTemplateDefinition,
    PromptTemplateVersion,
    PromptTemplateVersionStatus,
    RunStatus,
    ReviewStatus,
    ReferenceAnalysis,
    ShotKeyframe,
    ShotPlan,
    StoryGenerationBatch,
    StoryProposal,
    StoryProposalStatus,
    WorkflowRun,
    WorkflowStep,
)
from app.services.model_lab_service import (
    MODEL_LAB_WORKFLOW_KEY,
    create_experiment,
    execute_model_lab_workflow,
    get_experiment,
    preflight_experiment,
    preflight_existing_experiment,
    promote_winner_to_production,
    resume_provider_task_variant,
    response_payload,
    start_experiment,
    upsert_evaluation,
)
from app.services.prompt_template_service import ensure_prompt_template_foundation
from app.services.storage import local_asset_storage
from app.services.v1_configuration_service import bind_profile_to_slot, ensure_v1_foundation


def _seed() -> tuple[str, dict[str, str]]:
    """建立含已有 Mock Profile/Published Prompt 的最小安全测试上下文。"""

    init_database()
    db = SessionLocal()
    try:
        ensure_v1_foundation(db)
        ensure_prompt_template_foundation(db)
        project = Project(title=f"Model Lab fixture {uuid4().hex[:8]}")
        db.add(project)
        db.commit()
        pairs = {
            "text": ("STORY_GENERATE", "v1.story_generate", "V1_STORY_GENERATE"),
            "image": ("CHARACTER_IMAGE_GENERATE", "v1.character_image_prompt", "V1_CHARACTER_IMAGE"),
            "video": ("VIDEO_GENERATE", "v1.video_prompt_generate", "V1_VIDEO_PROMPT"),
        }
        values: dict[str, str] = {}
        for kind, (slot_key, prompt_key, operation_key) in pairs.items():
            profile_ids = list(
                db.scalars(
                    select(ModelProfile.id)
                    .where(
                        ModelProfile.step_key == slot_key,
                        ModelProfile.adapter_key == "mock_v1",
                    )
                    .order_by(ModelProfile.created_at)
                ).all()
            )
            while len(profile_ids) < 2:
                next_version = (db.scalar(select(ModelProfile.version).where(ModelProfile.step_key == slot_key).order_by(ModelProfile.version.desc())) or 0) + 1
                extra = ModelProfile(
                    step_key=slot_key,
                    provider_key="mock_v1",
                    adapter_key="mock_v1",
                    model_key=f"model-lab-{kind}-mock-{uuid4().hex[:8]}",
                    display_name=f"Model Lab {kind} Mock B",
                    version=next_version,
                    profile_status="DRAFT",
                    provider_config={},
                    parameter_config={},
                    is_active=False,
                )
                db.add(extra)
                db.flush()
                profile_ids.append(extra.id)
            definition = db.scalar(select(PromptTemplateDefinition).where(PromptTemplateDefinition.prompt_key == prompt_key))
            assert definition is not None
            prompt = db.get(PromptTemplateVersion, definition.active_version_id)
            assert prompt is not None
            values[f"{kind}_profile_a"] = profile_ids[0]
            values[f"{kind}_profile_b"] = profile_ids[1] if len(profile_ids) > 1 else profile_ids[0]
            values[f"{kind}_prompt"] = prompt.id
            values[f"{kind}_slot"] = slot_key
            values[f"{kind}_operation"] = operation_key
        db.commit()
        return project.id, values
    finally:
        db.close()


def _payload(kind: str = "text", *, mode: str = "MODEL_ONLY") -> dict:
    project_id, values = _seed()
    assets = _controlled_keyframe_assets(project_id)
    if kind == "text":
        input_source_type, input_payload, variables = "text", {"text": "一段受控实验文本"}, {"locked_reference_analysis": "一段受控实验文本"}
    elif kind == "image":
        input_source_type, input_payload, variables = "image_prompt", {"prompt": "黄色机器人，纯色背景", "reference_assets": [assets["reference"]]}, {"image_subject": "黄色机器人，纯色背景"}
    else:
        input_source_type, input_payload, variables = "locked_keyframe", {"video_prompt": "机器人挥手", "keyframe_asset": assets["keyframe"]}, {"shot": "机器人挥手"}
    first = {
        "label": "候选 A", "model_profile_id": values[f"{kind}_profile_a"],
        "prompt_template_version_id": values[f"{kind}_prompt"], "parameter_preset": "standard", "requested_overrides": {},
    }
    second = {
        "label": "候选 B", "model_profile_id": values[f"{kind}_profile_b"],
        "prompt_template_version_id": values[f"{kind}_prompt"], "parameter_preset": "standard", "requested_overrides": {},
    }
    return {
        "project_id": project_id, "name": f"{kind} Model Lab", "description": "测试", "operation_key": values[f"{kind}_operation"],
        "model_slot_key": values[f"{kind}_slot"], "capability": kind, "comparison_mode": mode,
        "input_source_type": input_source_type, "input_payload": input_payload, "prompt_variables": variables,
        "variants": [first, second], "repeat": 1, "max_create_calls": 2,
    }


def _controlled_keyframe_assets(project_id: str) -> dict[str, dict[str, object]]:
    """Create controlled locked V1 image/keyframe records; no external image is read."""

    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c6360f8cfc0000004010100b51c0c020000000049454e44ae426082"
    )
    keyframe_id = str(uuid4())
    reference_id = str(uuid4())
    db = SessionLocal()
    try:
        workflow = WorkflowRun(
            project_id=project_id,
            workflow_key="model_lab_fixture",
            status=RunStatus.SUCCEEDED,
        )
        db.add(workflow)
        db.flush()
        analysis = ReferenceAnalysis(
            project_id=project_id,
            workflow_run_id=workflow.id,
            version=1,
            generation_status=RunStatus.SUCCEEDED,
            review_status=ReviewStatus.LOCKED,
        )
        db.add(analysis)
        db.flush()
        batch = StoryGenerationBatch(
            project_id=project_id,
            reference_analysis_id=analysis.id,
            workflow_run_id=workflow.id,
            status=RunStatus.SUCCEEDED,
        )
        db.add(batch)
        db.flush()
        proposal = StoryProposal(
            batch_id=batch.id,
            project_id=project_id,
            candidate_number=1,
            content={},
            status=StoryProposalStatus.SELECTED,
        )
        db.add(proposal)
        db.flush()
        character = CharacterDefinition(
            story_proposal_id=proposal.id,
            project_id=project_id,
            character_code="model-lab-fixture",
            name="Model Lab fixture",
            age_description="成人",
            appearance="受控图片夹具",
            costume="无",
            temperament="稳定",
            design_status=DesignStatus.LOCKED,
        )
        db.add(character)
        db.flush()
        reference_url = local_asset_storage.save_generated_image_bytes(
            project_id=project_id,
            asset_kind="model-lab-fixture",
            asset_id=character.id,
            version=1,
            content=png,
            content_type="image/png",
        )
        db.add(
            CharacterReferenceImage(
                id=reference_id,
                character_id=character.id,
                project_id=project_id,
                generation_run_id=workflow.id,
                version=1,
                prompt_snapshot="受控 Model Lab 角色图片夹具",
                image_url=reference_url,
                generation_status=RunStatus.SUCCEEDED,
                review_status=ReviewStatus.LOCKED,
            )
        )
        plan = DirectorPlan(
            project_id=project_id,
            story_proposal_id=proposal.id,
            workflow_run_id=workflow.id,
            visual_bible={},
            status=DirectorPlanStatus.READY,
        )
        db.add(plan)
        db.flush()
        shot = ShotPlan(
            director_plan_id=plan.id,
            project_id=project_id,
            shot_number=1,
            action_description="Model Lab fixture",
            camera_description="固定机位",
            duration_seconds=5,
            video_action_prompt="fixture",
        )
        db.add(shot)
        db.flush()
        keyframe_url = local_asset_storage.save_generated_image_bytes(
            project_id=project_id,
            asset_kind="model-lab-fixture",
            asset_id=shot.id,
            version=1,
            content=png,
            content_type="image/png",
        )
        db.add(
            ShotKeyframe(
                id=keyframe_id,
                shot_id=shot.id,
                project_id=project_id,
                generation_run_id=workflow.id,
                version=1,
                prompt_snapshot="受控 Model Lab 关键帧夹具",
                image_url=keyframe_url,
                input_asset_snapshot={},
                generation_status=RunStatus.SUCCEEDED,
                review_status=ReviewStatus.LOCKED,
            )
        )
        db.commit()
    finally:
        db.close()
    metadata = {"sha256": sha256(png).hexdigest(), "mime_type": "image/png", "width": 1, "height": 1}
    return {
        "reference": {"asset_id": reference_id, **metadata},
        "keyframe": {"asset_id": keyframe_id, **metadata},
    }


def _created(kind: str = "text", *, mode: str = "MODEL_ONLY") -> tuple[dict, str]:
    payload = _payload(kind, mode=mode)
    db = SessionLocal()
    try:
        return payload, create_experiment(db, payload).id
    finally:
        db.close()


def _start(db, experiment_id: str, confirmed_create_calls: int = 2):
    experiment = get_experiment(db, experiment_id)
    assert experiment.preflight_hash
    return start_experiment(
        db,
        experiment_id=experiment_id,
        confirmed_create_calls=confirmed_create_calls,
        preflight_hash=experiment.preflight_hash,
    )


def test_model_only_preflight_freezes_inputs_without_workflow_or_invocation() -> None:
    payload = _payload()
    db = SessionLocal()
    try:
        before_runs = db.scalar(select(WorkflowRun).limit(1))
        preflight = preflight_experiment(db, payload)
        assert preflight["valid"] is True
        assert preflight["estimated_create_calls"] == 2
        assert preflight["differing_dimensions"] == ["model_profile"]
        assert db.scalar(select(WorkflowRun).limit(1)) == before_runs
    finally:
        db.close()


def test_start_creates_one_workflow_and_duplicate_start_cannot_create_more_steps() -> None:
    _, experiment_id = _created()
    db = SessionLocal()
    try:
        started = _start(db, experiment_id)
        assert started.workflow_run_id
        first_steps = list(db.scalars(select(WorkflowStep).where(WorkflowStep.workflow_run_id == started.workflow_run_id)).all())
        assert len(first_steps) == 2
        with pytest.raises(HTTPException) as exc:
            _start(db, experiment_id)
        assert exc.value.status_code == 409
        assert len(list(db.scalars(select(WorkflowStep).where(WorkflowStep.workflow_run_id == started.workflow_run_id)).all())) == 2
    finally:
        db.close()


@pytest.mark.parametrize("mode", ["PROMPT_ONLY", "PARAMETER_ONLY"])
def test_strict_modes_reject_non_matching_variant_dimensions(mode: str) -> None:
    payload = _payload(mode=mode)
    db = SessionLocal()
    try:
        with pytest.raises(HTTPException) as exc:
            preflight_experiment(db, payload)
        assert exc.value.status_code == 422
    finally:
        db.close()


def test_custom_marks_differing_dimensions_and_budget_enforced() -> None:
    payload = _payload(mode="CUSTOM")
    payload["max_create_calls"] = 1
    db = SessionLocal()
    try:
        with pytest.raises(HTTPException) as exc:
            preflight_experiment(db, payload)
        assert exc.value.status_code == 422
        payload["max_create_calls"] = 2
        assert "model_profile" in preflight_experiment(db, payload)["differing_dimensions"]
    finally:
        db.close()


def _text_profile(db, *, label: str, supported: dict, defaults: dict) -> ModelProfile:
    """构造只用于预检的本地 Profile；绝不进入真实 Adapter。"""

    profile = ModelProfile(
        step_key="STORY_GENERATE", provider_key="openai_compatible", adapter_key="openai_compatible",
        model_key=f"model-lab-{label}-{uuid4().hex[:8]}", version=10_000 + len(label),
        profile_status="DRAFT",
        provider_config={"api_base_url": "https://example.invalid", "secret_env_name": "MODEL_LAB_TEST_KEY"},
        parameter_config={
            "schema_version": 1, "capability": "text", "supported_parameters": supported,
            "defaults": defaults,
            "presets": {"preview": defaults, "standard": defaults, "high": defaults},
        },
        is_active=False,
    )
    db.add(profile)
    db.flush()
    return profile


def test_model_only_uses_parameter_intersection_and_native_preset_is_explicitly_unfair() -> None:
    payload = _payload(mode="MODEL_ONLY")
    db = SessionLocal()
    try:
        common = _text_profile(
            db, label="common", defaults={"temperature": 0.0, "max_tokens": 64},
            supported={"temperature": {"kind": "number", "minimum": 0.0, "maximum": 2.0}, "max_tokens": {"kind": "integer", "minimum": 1, "maximum": 256}},
        )
        narrow = _text_profile(
            db, label="narrow", defaults={"temperature": 0.0},
            supported={"temperature": {"kind": "number", "minimum": 0.0, "maximum": 2.0}},
        )
        payload["variants"][0]["model_profile_id"] = common.id
        payload["variants"][1]["model_profile_id"] = narrow.id
        result = preflight_experiment(db, payload)
        assert [item["effective_parameters"] for item in result["parameters"]] == [
            {"temperature": 0.0}, {"temperature": 0.0}
        ]
        assert any(item["parameter"] == "max_tokens" for item in result["parameters"][0]["omitted_parameters"])
        payload["comparison_mode"] = "NATIVE_PRESET"
        assert "model_profile" in preflight_experiment(db, payload)["differing_dimensions"]
    finally:
        db.close()


def test_model_only_rejects_profiles_without_a_common_parameter_capability() -> None:
    payload = _payload(mode="MODEL_ONLY")
    db = SessionLocal()
    try:
        temperature = _text_profile(
            db, label="temperature", defaults={"temperature": 0.0},
            supported={"temperature": {"kind": "number", "minimum": 0.0, "maximum": 2.0}},
        )
        token = _text_profile(
            db, label="tokens", defaults={"max_tokens": 64},
            supported={"max_tokens": {"kind": "integer", "minimum": 1, "maximum": 256}},
        )
        payload["variants"][0]["model_profile_id"] = temperature.id
        payload["variants"][1]["model_profile_id"] = token.id
        with pytest.raises(HTTPException) as exc:
            preflight_experiment(db, payload)
        assert exc.value.status_code == 422
    finally:
        db.close()


def test_prompt_only_and_parameter_only_have_valid_strict_paths_without_provider_execution() -> None:
    payload = _payload(mode="PROMPT_ONLY")
    db = SessionLocal()
    try:
        source = db.get(PromptTemplateVersion, payload["variants"][0]["prompt_template_version_id"])
        assert source is not None
        second = PromptTemplateVersion(
            prompt_template_id=source.prompt_template_id,
            version=source.version + 100,
            status=PromptTemplateVersionStatus.PUBLISHED,
            system_template=source.system_template,
            user_template=source.user_template,
            allowed_variables=deepcopy(source.allowed_variables),
            output_contract_key=source.output_contract_key,
            content_hash="c" * 64,
            change_summary="Model Lab Prompt 对比测试版本",
        )
        db.add(second)
        db.commit()
        payload["variants"][1]["model_profile_id"] = payload["variants"][0]["model_profile_id"]
        payload["variants"][1]["prompt_template_version_id"] = second.id
        assert preflight_experiment(db, payload)["differing_dimensions"] == ["prompt_version"]

        profile = ModelProfile(
            step_key="STORY_GENERATE", provider_key="openai_compatible", adapter_key="openai_compatible",
            model_key="model-lab-parameter-fixture", version=888, profile_status="DRAFT",
            provider_config={"api_base_url": "https://example.invalid", "secret_env_name": "MODEL_LAB_TEST_KEY"},
            parameter_config={
                "schema_version": 1, "capability": "text",
                "supported_parameters": {
                    "temperature": {"kind": "number", "minimum": 0.0, "maximum": 2.0},
                    "max_tokens": {"kind": "integer", "minimum": 1, "maximum": 32768},
                },
                "defaults": {"temperature": 0.2, "max_tokens": 64},
                "presets": {"preview": {"temperature": 0.0, "max_tokens": 32}, "standard": {"temperature": 0.2, "max_tokens": 64}, "high": {"temperature": 0.8, "max_tokens": 128}},
            },
            is_active=False,
        )
        db.add(profile)
        db.commit()
        parameter_payload = _payload(mode="PARAMETER_ONLY")
        for variant, preset in zip(parameter_payload["variants"], ("preview", "high")):
            variant["model_profile_id"] = profile.id
            variant["parameter_preset"] = preset
        assert preflight_experiment(db, parameter_payload)["differing_dimensions"] == ["parameters"]
    finally:
        db.close()


@pytest.mark.parametrize("kind", ["text", "image", "video"])
def test_fake_execution_reuses_workflow_step_and_invocation_without_provider_calls(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    payload, experiment_id = _created(kind)
    # 任意真实 Adapter 进入即失败；Mock 执行不应触发它。
    monkeypatch.setattr("app.services.v1_model_adapter_service.generate_structured_text", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("真实文本 Adapter 被调用")))
    db = SessionLocal()
    try:
        started = _start(db, experiment_id)
        assert started.workflow_run_id
        run_id = started.workflow_run_id
    finally:
        db.close()
    execute_model_lab_workflow(run_id)
    db = SessionLocal()
    try:
        experiment = get_experiment(db, experiment_id)
        variants = list(db.scalars(select(ModelExperimentVariant).where(ModelExperimentVariant.experiment_id == experiment.id)).all())
        assert experiment.status == ModelExperimentStatus.COMPLETED
        assert len(variants) == 2
        assert all(item.status == RunStatus.SUCCEEDED for item in variants)
        assert all(item.workflow_step_id and item.model_invocation_id for item in variants)
        assert all(item.provider_create_post_count == 0 and item.provider_task_id is None for item in variants)
        assert db.scalar(select(WorkflowStep).where(WorkflowStep.id == variants[0].workflow_step_id)) is not None
        assert db.scalar(select(WorkflowRun).where(WorkflowRun.id == experiment.workflow_run_id)) is not None
        if kind in {"image", "video"}:
            output = variants[0].output_reference or {}
            assert str(output.get("local_media_url", "")).startswith("/media/generated/")
    finally:
        db.close()


def test_variant_freeze_does_not_follow_new_profile_or_prompt_changes() -> None:
    payload, experiment_id = _created()
    db = SessionLocal()
    try:
        variant = db.scalar(select(ModelExperimentVariant).where(ModelExperimentVariant.experiment_id == experiment_id))
        assert variant is not None
        original_profile = deepcopy(variant.model_profile_snapshot)
        original_prompt = deepcopy(variant.prompt_snapshot)
        profile = db.get(ModelProfile, variant.model_profile_id)
        assert profile is not None
        original_model_key = profile.model_key
        profile.model_key = "changed-only-in-test-db"
        prompt = db.get(PromptTemplateVersion, variant.prompt_template_version_id)
        assert prompt is not None
        original_system_template = prompt.system_template
        prompt.system_template = "changed only after variant freeze"
        db.commit()
        db.refresh(variant)
        assert variant.model_profile_snapshot == original_profile
        assert variant.prompt_snapshot == original_prompt
        profile.model_key = original_model_key
        prompt.system_template = original_system_template
        db.commit()
    finally:
        db.close()


def test_rejects_draft_prompt_external_url_data_url_and_excess_variants() -> None:
    payload = _payload()
    db = SessionLocal()
    try:
        version = db.get(PromptTemplateVersion, payload["variants"][0]["prompt_template_version_id"])
        assert version is not None
        version.status = PromptTemplateVersionStatus.DRAFT
        db.commit()
        with pytest.raises(HTTPException):
            preflight_experiment(db, payload)
        version.status = PromptTemplateVersionStatus.PUBLISHED
        db.commit()
        payload["input_payload"] = {"text": "https://not-allowed.example"}
        with pytest.raises(HTTPException):
            preflight_experiment(db, payload)
        payload["input_payload"] = {"text": "/private/host-only-file"}
        with pytest.raises(HTTPException):
            preflight_experiment(db, payload)
        payload = _payload()
        payload["variants"] = payload["variants"] * 3
        with pytest.raises(HTTPException):
            preflight_experiment(db, payload)
    finally:
        db.close()


def test_image_input_requires_locked_project_asset_with_matching_header_and_digest() -> None:
    payload = _payload("image")
    db = SessionLocal()
    try:
        before_experiments = len(list(db.scalars(select(ModelExperiment)).all()))
        result = preflight_experiment(db, payload)
        reference = result["parameters"]  # Preflight remains metadata-only.
        assert reference and "data_url" not in json.dumps(result, ensure_ascii=False, default=str)

        bad_digest = deepcopy(payload)
        bad_digest["input_payload"]["reference_assets"][0]["sha256"] = "0" * 64
        with pytest.raises(HTTPException) as digest_error:
            preflight_experiment(db, bad_digest)
        assert digest_error.value.status_code == 409

        reference_id = payload["input_payload"]["reference_assets"][0]["asset_id"]
        image = db.get(CharacterReferenceImage, reference_id)
        assert image is not None and image.image_url
        image.review_status = ReviewStatus.PENDING_REVIEW
        db.commit()
        with pytest.raises(HTTPException) as lock_error:
            preflight_experiment(db, payload)
        assert lock_error.value.status_code == 409
        image.review_status = ReviewStatus.LOCKED
        db.commit()

        source = local_asset_storage.generated_media_path(image.image_url)
        original = source.read_bytes()
        try:
            source.write_bytes(b"not-an-image")
            with pytest.raises(HTTPException) as header_error:
                preflight_experiment(db, payload)
            assert header_error.value.status_code == 409
        finally:
            source.write_bytes(original)

        other_project = Project(title="Model Lab asset isolation")
        db.add(other_project)
        db.commit()
        cross_project = deepcopy(payload)
        cross_project["project_id"] = other_project.id
        with pytest.raises(HTTPException) as project_error:
            preflight_experiment(db, cross_project)
        assert project_error.value.status_code == 409
        assert len(list(db.scalars(select(ModelExperiment)).all())) == before_experiments
    finally:
        db.close()


def test_evaluation_winner_and_mock_profile_cannot_be_promoted() -> None:
    _, experiment_id = _created("image")
    db = SessionLocal()
    try:
        started = _start(db, experiment_id)
        run_id = started.workflow_run_id
    finally:
        db.close()
    assert run_id
    execute_model_lab_workflow(run_id)
    db = SessionLocal()
    try:
        experiment = get_experiment(db, experiment_id)
        variant = db.scalar(select(ModelExperimentVariant).where(ModelExperimentVariant.experiment_id == experiment.id))
        assert variant is not None
        scores = {"prompt_alignment": 5, "character_consistency": 5, "scene_consistency": 4, "product_fidelity": 4, "visual_quality": 5}
        evaluation = upsert_evaluation(db, experiment_id=experiment.id, variant_id=variant.id, scores=scores, notes="可采用", is_winner=True)
        assert evaluation.is_winner
        with pytest.raises(HTTPException):
            promote_winner_to_production(db, experiment_id=experiment.id, variant_id=variant.id, confirmed=False, replace_profile_id=None)
        # 即使 Mock Variant 已成功、已评分且已选为 Winner，后端也必须拒绝提升。
        slot = db.scalar(select(ModelSlot).where(ModelSlot.slot_key == experiment.model_slot_key))
        assert slot is not None
        before = list(db.scalars(select(ModelProfile.id).where(ModelProfile.step_key == experiment.model_slot_key)).all())
        with pytest.raises(HTTPException) as exc:
            promote_winner_to_production(db, experiment_id=experiment.id, variant_id=variant.id, confirmed=True, replace_profile_id=None)
        assert exc.value.status_code == 409
        assert db.get(ModelExperiment, experiment.id).promotion_metadata is None
        assert list(db.scalars(select(ModelProfile.id).where(ModelProfile.step_key == experiment.model_slot_key)).all()) == before
        assert db.scalar(select(ModelExperimentEvaluation).where(ModelExperimentEvaluation.variant_id == variant.id)) is not None
    finally:
        db.close()


def test_fixture_named_profile_cannot_be_promoted_even_with_non_mock_adapter() -> None:
    """A test-only identity is rejected at the service boundary, not just by UI labels."""

    _, experiment_id = _created()
    db = SessionLocal()
    try:
        experiment = get_experiment(db, experiment_id)
        variant = db.scalar(select(ModelExperimentVariant).where(ModelExperimentVariant.experiment_id == experiment.id))
        assert variant is not None
        fixture = _text_profile(
            db,
            label="fixture-candidate",
            defaults={"temperature": 0.0, "max_tokens": 64},
            supported={
                "temperature": {"kind": "number", "minimum": 0.0, "maximum": 2.0},
                "max_tokens": {"kind": "integer", "minimum": 1, "maximum": 256},
            },
        )
        snapshot = deepcopy(variant.model_profile_snapshot)
        snapshot["profile_snapshot"].update(
            {
                "profile_id": fixture.id,
                "adapter_key": fixture.adapter_key,
                "model_key": fixture.model_key,
                "model_version": fixture.model_key,
                "version": fixture.version,
            }
        )
        variant.model_profile_id = fixture.id
        variant.model_profile_version = fixture.version
        variant.model_profile_snapshot = snapshot
        variant.status = RunStatus.SUCCEEDED
        variant.output_reference = {"kind": "fixture", "mock": False}
        db.commit()
        scores = {"instruction_following": 5, "structure": 5, "story_quality": 4, "commerce_integration": 4, "executability": 5}
        upsert_evaluation(db, experiment_id=experiment.id, variant_id=variant.id, scores=scores, notes="测试夹具", is_winner=True)
        with pytest.raises(HTTPException) as error:
            promote_winner_to_production(
                db,
                experiment_id=experiment.id,
                variant_id=variant.id,
                confirmed=True,
                replace_profile_id=None,
            )
        assert error.value.status_code == 409
    finally:
        db.close()


def test_non_mock_winner_promotion_only_changes_slot_binding_not_prompt_or_preset() -> None:
    """Promotion is a deliberate profile-binding action, never a configuration cascade."""

    _, experiment_id = _created()
    db = SessionLocal()
    slot_key = ""
    original_profile_id: str | None = None
    original_priority = 100
    candidate_id: str | None = None
    try:
        experiment = get_experiment(db, experiment_id)
        variant = db.scalar(select(ModelExperimentVariant).where(ModelExperimentVariant.experiment_id == experiment_id))
        assert variant is not None
        candidate = _text_profile(
            db,
            label="candidate",
            defaults={"temperature": 0.0, "max_tokens": 64},
            supported={
                "temperature": {"kind": "number", "minimum": 0.0, "maximum": 2.0},
                "max_tokens": {"kind": "integer", "minimum": 1, "maximum": 256},
            },
        )
        candidate_id = candidate.id
        snapshot = deepcopy(variant.model_profile_snapshot)
        profile_snapshot = snapshot["profile_snapshot"]
        profile_snapshot.update(
            {
                "profile_id": candidate.id,
                "adapter_key": "openai_compatible",
                "model_key": candidate.model_key,
                "model_version": candidate.model_key,
                "version": candidate.version,
                "provider_config": {"api_base_url": "https://example.invalid", "secret_env_name": "MODEL_LAB_TEST_KEY"},
            }
        )
        variant.model_profile_id = candidate.id
        variant.model_profile_version = candidate.version
        variant.model_profile_snapshot = snapshot
        variant.status = RunStatus.SUCCEEDED
        variant.output_reference = {"kind": "isolated_non_mock_fixture", "mock": False}
        slot = db.scalar(select(ModelSlot).where(ModelSlot.slot_key == experiment.model_slot_key))
        assert slot is not None
        old_binding = db.scalar(
            select(ModelSlotProfileBinding).where(
                ModelSlotProfileBinding.slot_id == slot.id,
                ModelSlotProfileBinding.is_enabled.is_(True),
            ).order_by(ModelSlotProfileBinding.created_at)
        )
        assert old_binding is not None
        slot_key = slot.slot_key
        original_profile_id = old_binding.model_profile_id
        original_priority = old_binding.priority
        prompt_before = list(db.execute(select(PromptTemplateDefinition.id, PromptTemplateDefinition.active_version_id)).all())
        preset_before = list(db.execute(select(CommerceWorkflowPresetDefinition.id, CommerceWorkflowPresetDefinition.active_version_id)).all())
        db.commit()
        scores = {"instruction_following": 5, "structure": 5, "story_quality": 4, "commerce_integration": 4, "executability": 5}
        upsert_evaluation(db, experiment_id=experiment.id, variant_id=variant.id, scores=scores, notes="已审核", is_winner=True)
        promoted = promote_winner_to_production(
            db,
            experiment_id=experiment.id,
            variant_id=variant.id,
            confirmed=True,
            replace_profile_id=old_binding.model_profile_id,
        )
        binding = db.scalar(
            select(ModelSlotProfileBinding).where(
                ModelSlotProfileBinding.slot_id == slot.id,
                ModelSlotProfileBinding.model_profile_id == candidate.id,
            )
        )
        assert binding is not None and binding.is_enabled is True
        assert promoted.promotion_metadata and promoted.promotion_metadata["new_profile_id"] == candidate.id
        assert list(db.execute(select(PromptTemplateDefinition.id, PromptTemplateDefinition.active_version_id)).all()) == prompt_before
        assert list(db.execute(select(CommerceWorkflowPresetDefinition.id, CommerceWorkflowPresetDefinition.active_version_id)).all()) == preset_before
    finally:
        if slot_key and original_profile_id and candidate_id:
            bind_profile_to_slot(
                db,
                slot_key=slot_key,
                model_profile_id=original_profile_id,
                enabled=True,
                priority=original_priority,
                weight=None,
                replace_existing=True,
                replace_profile_id=candidate_id,
                commit=False,
            )
            db.commit()
        db.close()


def test_start_requires_current_preflight_hash_and_exact_confirmed_call_count() -> None:
    _, experiment_id = _created()
    db = SessionLocal()
    try:
        experiment = get_experiment(db, experiment_id)
        assert experiment.preflight_hash
        before_runs = len(list(db.scalars(select(WorkflowRun)).all()))
        with pytest.raises(HTTPException) as count_error:
            start_experiment(
                db, experiment_id=experiment_id, confirmed_create_calls=1, preflight_hash=experiment.preflight_hash
            )
        assert count_error.value.status_code == 409
        with pytest.raises(HTTPException) as hash_error:
            start_experiment(
                db, experiment_id=experiment_id, confirmed_create_calls=2, preflight_hash="0" * 64
            )
        assert hash_error.value.status_code == 409
        assert len(list(db.scalars(select(WorkflowRun)).all())) == before_runs
        variant = db.scalar(select(ModelExperimentVariant).where(ModelExperimentVariant.experiment_id == experiment_id))
        assert variant is not None
        variant.requested_overrides = {"tampered": True}
        db.commit()
        with pytest.raises(HTTPException) as stale_error:
            start_experiment(
                db, experiment_id=experiment_id, confirmed_create_calls=2, preflight_hash=experiment.preflight_hash
            )
        assert stale_error.value.status_code == 409
        assert len(list(db.scalars(select(WorkflowRun)).all())) == before_runs
    finally:
        db.close()


def test_start_rechecks_profile_and_published_prompt_versions_before_creating_work() -> None:
    _, experiment_id = _created()
    db = SessionLocal()
    try:
        experiment = get_experiment(db, experiment_id)
        assert experiment.preflight_hash
        variant = db.scalar(select(ModelExperimentVariant).where(ModelExperimentVariant.experiment_id == experiment_id))
        assert variant is not None
        prompt = db.get(PromptTemplateVersion, variant.prompt_template_version_id)
        assert prompt is not None
        prompt.status = PromptTemplateVersionStatus.DRAFT
        db.commit()
        with pytest.raises(HTTPException) as prompt_error:
            start_experiment(
                db, experiment_id=experiment_id, confirmed_create_calls=2, preflight_hash=experiment.preflight_hash
            )
        assert prompt_error.value.status_code == 409
        assert db.scalar(
            select(WorkflowRun).where(
                WorkflowRun.project_id == experiment.project_id,
                WorkflowRun.workflow_key == MODEL_LAB_WORKFLOW_KEY,
            )
        ) is None
        prompt.status = PromptTemplateVersionStatus.PUBLISHED
        profile = db.get(ModelProfile, variant.model_profile_id)
        assert profile is not None
        profile.version += 1
        db.commit()
        with pytest.raises(HTTPException) as profile_error:
            start_experiment(
                db, experiment_id=experiment_id, confirmed_create_calls=2, preflight_hash=experiment.preflight_hash
            )
        assert profile_error.value.status_code == 409
        assert db.scalar(
            select(WorkflowRun).where(
                WorkflowRun.project_id == experiment.project_id,
                WorkflowRun.workflow_key == MODEL_LAB_WORKFLOW_KEY,
            )
        ) is None
    finally:
        db.close()


def test_existing_experiment_preflight_returns_bound_hash_without_workflow_or_invocation() -> None:
    _, experiment_id = _created()
    db = SessionLocal()
    try:
        before_runs = len(list(db.scalars(select(WorkflowRun)).all()))
        before_invocations = len(list(db.scalars(select(ModelInvocation)).all()))
        result = preflight_existing_experiment(db, experiment_id)
        assert result["experiment_id"] == experiment_id
        assert result["preflight_hash"] and len(result["preflight_hash"]) == 64
        assert result["variant_config_hash"] and len(result["variant_config_hash"]) == 64
        assert result["expected_create_call_count"] == 2
        assert result["max_create_calls"] == 2
        assert len(list(db.scalars(select(WorkflowRun)).all())) == before_runs
        assert len(list(db.scalars(select(ModelInvocation)).all())) == before_invocations
    finally:
        db.close()


def test_provider_task_recovery_reuses_variant_and_never_creates_a_second_step_or_invocation() -> None:
    _, experiment_id = _created()
    db = SessionLocal()
    try:
        started = _start(db, experiment_id)
        variant = db.scalar(select(ModelExperimentVariant).where(ModelExperimentVariant.experiment_id == experiment_id))
        assert variant is not None
        original_step_id, original_invocation_id = variant.workflow_step_id, variant.model_invocation_id
        variant.status = RunStatus.FAILED
        variant.provider_task_id = "existing-provider-task-only-for-test"
        variant.provider_create_post_count = 1
        step = db.get(WorkflowStep, variant.workflow_step_id)
        invocation = db.get(ModelInvocation, variant.model_invocation_id)
        assert step is not None and invocation is not None
        step.status = RunStatus.FAILED
        invocation.status = RunStatus.FAILED
        db.commit()
        resumed = resume_provider_task_variant(db, experiment_id=experiment_id, variant_id=variant.id)
        assert resumed.workflow_step_id == original_step_id
        assert resumed.model_invocation_id == original_invocation_id
        assert resumed.provider_task_id == "existing-provider-task-only-for-test"
        assert resumed.provider_create_post_count == 0
        assert db.get(WorkflowStep, original_step_id).input_payload["provider_task_recovery"]["provider_create_post_count"] == 0
    finally:
        db.close()


def test_api_does_not_accept_provider_task_id_and_catalog_hides_adapter_connection_fields() -> None:
    payload = _payload()
    with TestClient(app) as client:
        response = client.post("/api/v1/model-lab/preflight", json=payload)
        assert response.status_code == 200, response.text
        catalog = client.get("/api/v1/model-lab/catalog", params={"operation_key": payload["operation_key"], "model_slot_key": payload["model_slot_key"], "capability": "text"})
        assert catalog.status_code == 200, catalog.text
        serialized = catalog.text.casefold()
        assert "secret_env_name" not in serialized and "api_base_url" not in serialized and "authorization" not in serialized
        created = client.post("/api/v1/model-lab/experiments", json=payload)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["workflow_run_id"] is None
        assert "production_profiles" in body
        assert "provider_config" not in created.text.casefold()
        assert client.post("/api/v1/model-lab/experiments/not-a-real/variants/nope/resume-provider-task", json={"provider_task_id": "forbidden"}).status_code in {404, 422}


def test_0025_sqlite_upgrade_downgrade_and_reupgrade(tmp_path: Path) -> None:
    """0025 不回填历史数据；空库可安全往返，存在实验时 downgrade 明确拒绝。"""

    server_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "model-lab-migration.db"
    config = Config(str(server_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    config.set_main_option("script_location", str(server_root / "migrations"))
    script = ScriptDirectory.from_config(config)
    assert script.get_current_head() == "0025_model_lab"

    def migrate(action: str, revision: str) -> None:
        engine = create_engine(f"sqlite:///{database_path}")
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()
                config.attributes["connection"] = connection
                getattr(command, action)(config, revision)
                config.attributes.pop("connection", None)
        finally:
            engine.dispose()

    migrate("upgrade", "0024_commerce_workflow_presets")
    migrate("upgrade", "head")
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert {"model_experiments", "model_experiment_variants", "model_experiment_evaluations"}.issubset(set(inspector.get_table_names()))
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    migrate("downgrade", "0024_commerce_workflow_presets")
    migrate("upgrade", "head")
