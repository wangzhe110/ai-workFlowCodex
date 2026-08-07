"""V1 模型中心的候选创建、人工切换和密钥边界测试。"""

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.main import app
from app.models import RunStatus, WorkflowRun
from conftest import real_video_bytes


def test_v1_model_profile_listing_is_safe_after_repeated_foundation_initialization() -> None:
    """模型中心的重复 GET 会反复调用初始化，但必须稳定返回 200。"""

    with TestClient(app) as client:
        first = client.get("/api/v1/production/v1-model-profiles")
        second = client.get("/api/v1/production/v1-model-profiles")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert len(second.json()) == len(first.json())


def test_v1_model_profile_can_manually_replace_single_slot_mock() -> None:
    """用户明确确认后才能替换单模型槽位，默认本地模拟会被停用但保留历史。"""

    with TestClient(app) as client:
        try:
            created = client.post(
                "/api/v1/production/v1-model-profiles",
                json={
                    "slot_key": "VIDEO_ANALYSIS",
                    "adapter_key": "openai_compatible_vision",
                    "model_key": "vision-model-from-relay",
                    "display_name": "参考视频分析模型（候选）",
                    "model_version": "preview",
                    "provider_config": {
                        "api_base_url": "https://relay.example/v1",
                        "secret_env_name": "YUNWU_API_KEY",
                        "result_contract": "V1_REFERENCE_ANALYSIS",
                        "frame_sample_count": 6,
                    },
                    "enable_in_slot": True,
                    "replace_existing": True,
                },
            )

            assert created.status_code == 201, created.text
            assert created.json()["is_enabled_in_slot"] is True
            profiles = client.get("/api/v1/production/v1-model-profiles")
            assert profiles.status_code == 200
            analysis_profiles = [item for item in profiles.json() if item["slot_key"] == "VIDEO_ANALYSIS"]
            assert len([item for item in analysis_profiles if item["is_enabled_in_slot"]]) == 1
            assert any(item["model_key"] == "mock-v1-video-analysis" for item in analysis_profiles)
        finally:
            # 测试环境与其他完整闭环测试共用数据库，恢复本地模拟默认值避免测试顺序影响。
            profiles = client.get("/api/v1/production/v1-model-profiles").json()
            mock = next(item for item in profiles if item["model_key"] == "mock-v1-video-analysis")
            restored = client.post(
                "/api/v1/production/model-slots/VIDEO_ANALYSIS/bindings",
                json={
                    "model_profile_id": mock["id"],
                    "enabled": True,
                    "replace_existing": True,
                },
            )
            assert restored.status_code == 201, restored.text


def test_v1_model_profile_rejects_api_key_values_in_configuration() -> None:
    """浏览器/API 都不能把真实密钥混入可审计的数据库配置。"""

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/production/v1-model-profiles",
            json={
                "slot_key": "STORY_GENERATE",
                "adapter_key": "openai_compatible",
                "model_key": "text-model-from-relay",
                "display_name": "故事模型（不安全测试）",
                "provider_config": {
                    "api_base_url": "https://relay.example/v1",
                    "secret_env_name": "YUNWU_API_KEY",
                    "api_key": "must-never-be-stored",
                },
            },
        )

        assert response.status_code == 422
        assert "不能填" in response.json()["detail"]


def test_unused_unenabled_model_profile_can_be_deleted() -> None:
    """未启用、未调用的候选配置可直接删除，并从模型中心列表消失。"""

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/production/v1-model-profiles",
            json={
                "slot_key": "VIDEO_ANALYSIS",
                "adapter_key": "mock_v1",
                "model_key": "deletable-candidate",
                "display_name": "可删除候选模型",
                "provider_config": {"local_only": True},
                "enable_in_slot": False,
            },
        )
        assert created.status_code == 201, created.text
        profile = created.json()
        assert profile["is_enabled_in_slot"] is False
        assert profile["can_delete"] is True
        assert profile["active_run_count"] == 0

        deleted = client.delete(f"/api/v1/production/v1-model-profiles/{profile['id']}")
        assert deleted.status_code == 204, deleted.text
        listed = client.get("/api/v1/production/v1-model-profiles")
        assert listed.status_code == 200, listed.text
        assert all(item["id"] != profile["id"] for item in listed.json())


def test_active_model_profile_cannot_be_deleted_while_v1_run_is_in_progress() -> None:
    """已启用模型若被排队/执行中的任务冻结，API 必须拒绝删除。"""

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/production/v1-model-profiles",
            json={
                "slot_key": "VIDEO_ANALYSIS",
                "adapter_key": "mock_v1",
                "model_key": "active-deletable-candidate",
                "display_name": "运行中删除保护模型",
                "provider_config": {"local_only": True},
                "enable_in_slot": False,
            },
        )
        assert created.status_code == 201, created.text
        profile = created.json()

        try:
            enabled = client.post(
                "/api/v1/production/model-slots/VIDEO_ANALYSIS/bindings",
                json={
                    "model_profile_id": profile["id"],
                    "enabled": True,
                    "replace_existing": True,
                },
            )
            assert enabled.status_code == 201, enabled.text

            project_id = client.post("/api/v1/projects", json={"title": "模型删除保护"}).json()["id"]
            db = SessionLocal()
            try:
                active_run = WorkflowRun(
                    project_id=project_id,
                    workflow_key="v1_reference_analysis",
                    status=RunStatus.PENDING,
                    input_snapshot={
                        "model_bindings": {
                            "VIDEO_ANALYSIS": [{"model_profile_id": profile["id"]}],
                        }
                    },
                )
                db.add(active_run)
                db.commit()
                active_run_id = active_run.id
            finally:
                db.close()

            profile_state = next(
                item for item in client.get("/api/v1/production/v1-model-profiles").json()
                if item["id"] == profile["id"]
            )
            assert profile_state["active_run_count"] == 1
            assert profile_state["can_delete"] is False
            assert "进行中" in profile_state["delete_block_reason"]

            blocked = client.delete(f"/api/v1/production/v1-model-profiles/{profile['id']}")
            assert blocked.status_code == 409
            assert "进行中" in blocked.json()["detail"]

            db = SessionLocal()
            try:
                db.get(WorkflowRun, active_run_id).status = RunStatus.CANCELLED
                db.commit()
            finally:
                db.close()
            # 任务结束且尚未发生模型调用后，启用中的候选也可显式删除；槽位绑定同步删除。
            deleted = client.delete(f"/api/v1/production/v1-model-profiles/{profile['id']}")
            assert deleted.status_code == 204, deleted.text
        finally:
            # 删除当前版本会同时删掉绑定，恢复本地模拟保证其余闭环测试稳定。
            profiles = client.get("/api/v1/production/v1-model-profiles").json()
            mock = next(item for item in profiles if item["model_key"] == "mock-v1-video-analysis")
            restored = client.post(
                "/api/v1/production/model-slots/VIDEO_ANALYSIS/bindings",
                json={
                    "model_profile_id": mock["id"],
                    "enabled": True,
                    "replace_existing": True,
                },
            )
            assert restored.status_code == 201, restored.text


def test_v1_model_profile_editing_and_copy_keep_called_version_immutable() -> None:
    """草稿和未调用的启用版本可编辑；产生调用后只能复制下一版 Draft。"""

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/production/v1-model-profiles",
            json={
                "slot_key": "VIDEO_ANALYSIS",
                "adapter_key": "mock_v1",
                "model_key": "editable-mock-v1",
                "display_name": "可编辑本地分析模型",
                "provider_config": {"local_only": True},
                "enable_in_slot": False,
            },
        )
        assert created.status_code == 201, created.text
        draft = created.json()
        assert draft["profile_status"] == "DRAFT"
        assert draft["can_edit"] is True

        edited_draft = client.patch(
            f"/api/v1/production/v1-model-profiles/{draft['id']}",
            json={
                "adapter_key": "mock_v1",
                "model_key": "editable-mock-v1-draft",
                "display_name": "已编辑草稿模型",
                "model_version": "draft-test",
                "provider_config": {"local_only": True},
            },
        )
        assert edited_draft.status_code == 200, edited_draft.text
        assert edited_draft.json()["version"] == draft["version"]
        assert edited_draft.json()["model_key"] == "editable-mock-v1-draft"

        try:
            enabled = client.post(
                "/api/v1/production/model-slots/VIDEO_ANALYSIS/bindings",
                json={
                    "model_profile_id": draft["id"],
                    "enabled": True,
                    "replace_existing": True,
                },
            )
            assert enabled.status_code == 201, enabled.text

            # 已启用但还没生产调用的版本仍可原地编辑。
            edited_active = client.patch(
                f"/api/v1/production/v1-model-profiles/{draft['id']}",
                json={
                    "adapter_key": "mock_v1",
                    "model_key": "editable-mock-v1-active",
                    "display_name": "已启用未调用模型",
                    "provider_config": {"local_only": True},
                },
            )
            assert edited_active.status_code == 200, edited_active.text
            assert edited_active.json()["profile_status"] == "ACTIVE"
            assert edited_active.json()["can_edit"] is True

            project_id = client.post("/api/v1/projects", json={"title": "模型版本不可覆盖"}).json()["id"]
            upload = client.post(
                f"/api/v1/projects/{project_id}/source-video",
                files={"file": ("reference.mp4", real_video_bytes(), "video/mp4")},
            )
            assert upload.status_code == 201, upload.text
            run = client.post(
                f"/api/v1/production/projects/{project_id}/generation-runs/reference_analysis",
                json={"source_asset_id": upload.json()["id"]},
            )
            assert run.status_code == 202, run.text

            immutable = client.patch(
                f"/api/v1/production/v1-model-profiles/{draft['id']}",
                json={
                    "adapter_key": "mock_v1",
                    "model_key": "must-not-overwrite",
                    "display_name": "不可改",
                    "provider_config": {"local_only": True},
                },
            )
            assert immutable.status_code == 409
            assert "复制" in immutable.json()["detail"]

            historical_delete = client.delete(f"/api/v1/production/v1-model-profiles/{draft['id']}")
            assert historical_delete.status_code == 409
            assert "调用记录" in historical_delete.json()["detail"]

            copied = client.post(f"/api/v1/production/v1-model-profiles/{draft['id']}/copy")
            assert copied.status_code == 201, copied.text
            next_version = copied.json()
            assert next_version["version"] == draft["version"] + 1
            assert next_version["profile_status"] == "DRAFT"
            assert next_version["is_enabled_in_slot"] is False
            assert next_version["can_edit"] is True

            edited_copy = client.patch(
                f"/api/v1/production/v1-model-profiles/{next_version['id']}",
                json={
                    "adapter_key": "mock_v1",
                    "model_key": "editable-mock-v2",
                    "display_name": "下一版测试模型",
                    "provider_config": {"local_only": True},
                },
            )
            assert edited_copy.status_code == 200, edited_copy.text
            assert edited_copy.json()["version"] == next_version["version"]
        finally:
            # 恢复默认无密钥配置，避免影响其它 V1 闭环测试的运行顺序。
            profiles = client.get("/api/v1/production/v1-model-profiles").json()
            mock = next(item for item in profiles if item["model_key"] == "mock-v1-video-analysis")
            restored = client.post(
                "/api/v1/production/model-slots/VIDEO_ANALYSIS/bindings",
                json={
                    "model_profile_id": mock["id"],
                    "enabled": True,
                    "replace_existing": True,
                },
            )
            assert restored.status_code == 201, restored.text
