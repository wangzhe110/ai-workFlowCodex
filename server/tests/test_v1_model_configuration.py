"""V1 模型中心的候选创建、人工切换和密钥边界测试。"""

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import SessionLocal
from app.main import app
from app.models import ModelProfile, ModelSlot, RunStatus, WorkflowRun
from app.services.v1_execution_service import _invoke, _profile_snapshot
from conftest import real_video_bytes


def test_v1_real_adapter_preflight_uses_v1_slot_contract_without_generation(monkeypatch) -> None:
    """V1 槽位不能被旧工作流的步骤白名单误判为“未接入”。

    预检只读取 OpenAI 兼容的 ``/models``，不创建任何图片、视频或模型调用。
    """

    monkeypatch.setenv("V1_PREFLIGHT_TEST_KEY", "test-key-is-not-a-real-secret")
    seen: list[tuple[str, float]] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit: int) -> bytes:
            return b'{"data":[{"id":"vision-preflight-model"}]}'

    def fake_urlopen(request, timeout: float):
        seen.append((request.full_url, timeout))
        return FakeResponse()

    monkeypatch.setattr("app.services.v1_configuration_service.urlopen", fake_urlopen)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/production/v1-model-profiles",
            json={
                "slot_key": "VIDEO_ANALYSIS",
                "adapter_key": "openai_compatible_vision",
                "model_key": "vision-preflight-model",
                "display_name": "V1 真实预检测试候选",
                "provider_config": {
                    "api_base_url": "https://relay.example/v1",
                    "secret_env_name": "V1_PREFLIGHT_TEST_KEY",
                    "result_contract": "V1_REFERENCE_ANALYSIS",
                    "frame_sample_count": 2,
                },
            },
        )
        assert created.status_code == 201, created.text
        profile_id = created.json()["id"]
        try:
            response = client.post(f"/api/v1/production/v1-model-profiles/{profile_id}/preflight")
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["ready"] is True
            assert {check["key"] for check in payload["checks"]} == {"adapter", "config", "secret", "network"}
            assert all(check["status"] == "passed" for check in payload["checks"])
            assert seen == [("https://relay.example/v1/models", 10.0)]
        finally:
            deleted = client.delete(f"/api/v1/production/v1-model-profiles/{profile_id}")
            assert deleted.status_code == 204, deleted.text


def test_v1_preflight_checks_each_profile_own_secret_env_name(monkeypatch) -> None:
    """预检按 Profile 读取变量名；缺图片 Key 不影响已经配置的推理通道。"""

    monkeypatch.setenv("YUNWU_REASONING_API_KEY", "test-reasoning-key")
    monkeypatch.delenv("YUNWU_IMAGE_API_KEY", raising=False)
    requested_urls: list[str] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit: int) -> bytes:
            return b'{"data":[{"id":"gemini-reasoning-preview"}]}'

    def fake_urlopen(request, timeout: float):
        requested_urls.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr("app.services.v1_configuration_service.urlopen", fake_urlopen)
    with TestClient(app) as client:
        reasoning = client.post(
            "/api/v1/production/v1-model-profiles",
            json={
                "slot_key": "VIDEO_ANALYSIS",
                "adapter_key": "openai_compatible_vision",
                "model_key": "gemini-reasoning-preview",
                "display_name": "推理通道预检",
                "provider_config": {
                    "api_base_url": "https://reasoning-relay.example/v1",
                    "secret_env_name": "YUNWU_REASONING_API_KEY",
                    "result_contract": "V1_REFERENCE_ANALYSIS",
                },
            },
        )
        image = client.post(
            "/api/v1/production/v1-model-profiles",
            json={
                "slot_key": "CHARACTER_IMAGE_GENERATE",
                "adapter_key": "openai_compatible_image",
                "model_key": "banana-image-model",
                "display_name": "图片通道预检",
                "provider_config": {
                    "api_base_url": "https://image-relay.example/v1",
                    "secret_env_name": "YUNWU_IMAGE_API_KEY",
                },
            },
        )
        assert reasoning.status_code == 201, reasoning.text
        assert image.status_code == 201, image.text
        reasoning_id = reasoning.json()["id"]
        image_id = image.json()["id"]
        try:
            reasoning_preflight = client.post(f"/api/v1/production/v1-model-profiles/{reasoning_id}/preflight")
            image_preflight = client.post(f"/api/v1/production/v1-model-profiles/{image_id}/preflight")

            assert reasoning_preflight.status_code == 200, reasoning_preflight.text
            assert reasoning_preflight.json()["ready"] is True
            assert any(
                item["key"] == "secret" and item["status"] == "passed" and "YUNWU_REASONING_API_KEY" in item["message"]
                for item in reasoning_preflight.json()["checks"]
            )
            assert requested_urls == ["https://reasoning-relay.example/v1/models"]

            assert image_preflight.status_code == 200, image_preflight.text
            assert image_preflight.json()["ready"] is False
            assert any(
                item["key"] == "secret" and item["status"] == "failed" and "YUNWU_IMAGE_API_KEY" in item["message"]
                for item in image_preflight.json()["checks"]
            )
            # 图片 Key 缺失时不会用推理 Key 发起 /models 请求。
            assert requested_urls == ["https://reasoning-relay.example/v1/models"]
        finally:
            for profile_id in (image_id, reasoning_id):
                deleted = client.delete(f"/api/v1/production/v1-model-profiles/{profile_id}")
                assert deleted.status_code == 204, deleted.text


def test_v1_fal_queue_image_profile_uses_image_channel_without_models_catalog(monkeypatch) -> None:
    """Nano Banana 队列 Profile 只检查自己的图片 Key，预检不误调 /models 或推理 Key。"""

    monkeypatch.setenv("YUNWU_IMAGE_API_KEY", "test-image-key")
    requested_urls: list[str] = []

    def forbidden_urlopen(request, timeout: float):  # pragma: no cover - 调用即表示发生了非预期网络探针。
        requested_urls.append(request.full_url)
        raise AssertionError("Fal 图片基础预检不应产生网络请求")

    monkeypatch.setattr("app.services.v1_configuration_service.urlopen", forbidden_urlopen)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/production/v1-model-profiles",
            json={
                "slot_key": "CHARACTER_IMAGE_GENERATE",
                "adapter_key": "fal_queue_image",
                "model_key": "banana",
                "display_name": "云雾 Nano Banana 队列预检",
                "provider_config": {
                    "api_base_url": "https://yunwu.ai",
                    "secret_env_name": "YUNWU_IMAGE_API_KEY",
                    "poll_interval_seconds": 3,
                    "max_poll_seconds": 120,
                },
            },
        )
        assert created.status_code == 201, created.text
        profile_id = created.json()["id"]
        try:
            response = client.post(f"/api/v1/production/v1-model-profiles/{profile_id}/preflight")
            assert response.status_code == 200, response.text
            checks = response.json()["checks"]
            assert any(item["key"] == "secret" and item["status"] == "passed" for item in checks)
            assert any(item["key"] == "provider_permission" and item["status"] == "warning" for item in checks)
            assert requested_urls == []
        finally:
            deleted = client.delete(f"/api/v1/production/v1-model-profiles/{profile_id}")
            assert deleted.status_code == 204, deleted.text


def test_v1_ark_seedream_profile_uses_ark_secret_without_catalog_request(monkeypatch) -> None:
    """官方图片 Profile 只检查 ARK_API_KEY，预检不调用模型也不借用云雾 Key。"""

    monkeypatch.setenv("ARK_API_KEY", "test-ark-image-key")
    monkeypatch.setenv("YUNWU_IMAGE_API_KEY", "must-not-be-used")
    requested_urls: list[str] = []

    def forbidden_urlopen(request, timeout: float):  # pragma: no cover - 调用即表示发生非预期付费或目录请求。
        requested_urls.append(request.full_url)
        raise AssertionError("方舟图片基础预检不应产生网络请求")

    monkeypatch.setattr("app.services.v1_configuration_service.urlopen", forbidden_urlopen)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/production/v1-model-profiles",
            json={
                "slot_key": "CHARACTER_IMAGE_GENERATE",
                "adapter_key": "volcengine_ark_image",
                "model_key": "doubao-seedream-5-0-260128",
                "display_name": "方舟 Seedream 预检",
                "provider_config": {
                    "api_base_url": "https://ark.cn-beijing.volces.com/api/v3",
                    "secret_env_name": "ARK_API_KEY",
                    "size": "2K",
                    "sequential_image_generation": "disabled",
                    "response_format": "url",
                    "watermark": False,
                },
            },
        )
        assert created.status_code == 201, created.text
        profile_id = created.json()["id"]
        try:
            response = client.post(f"/api/v1/production/v1-model-profiles/{profile_id}/preflight")
            assert response.status_code == 200, response.text
            checks = response.json()["checks"]
            assert any(item["key"] == "secret" and item["status"] == "passed" for item in checks)
            assert any(item["key"] == "provider_permission" and item["status"] == "warning" for item in checks)
            assert requested_urls == []
        finally:
            deleted = client.delete(f"/api/v1/production/v1-model-profiles/{profile_id}")
            assert deleted.status_code == 204, deleted.text


def test_v1_three_image_slots_can_bind_separate_ark_seedream_profile_versions() -> None:
    """三个图片槽位各自冻结独立 Profile 版本，同时保留并可恢复旧活动绑定。"""

    slots = ("CHARACTER_IMAGE_GENERATE", "SCENE_IMAGE_GENERATE", "SHOT_KEYFRAME_GENERATE")
    created_ids: list[str] = []
    originals: dict[str, dict] = {}
    with TestClient(app) as client:
        before = client.get("/api/v1/production/v1-model-profiles")
        assert before.status_code == 200, before.text
        for slot in slots:
            originals[slot] = next(item for item in before.json() if item["slot_key"] == slot and item["is_enabled_in_slot"])
        try:
            for slot in slots:
                created = client.post(
                    "/api/v1/production/v1-model-profiles",
                    json={
                        "slot_key": slot,
                        "adapter_key": "volcengine_ark_image",
                        "model_key": "doubao-seedream-5-0-260128",
                        "display_name": f"{slot} 方舟图片候选",
                        "provider_config": {
                            "api_base_url": "https://ark.cn-beijing.volces.com/api/v3",
                            "secret_env_name": "ARK_API_KEY",
                            "size": "2K",
                            "sequential_image_generation": "disabled",
                            "response_format": "url",
                            "watermark": False,
                        },
                        "enable_in_slot": True,
                        "replace_existing": True,
                    },
                )
                assert created.status_code == 201, created.text
                created_ids.append(created.json()["id"])

            after = client.get("/api/v1/production/v1-model-profiles")
            assert after.status_code == 200, after.text
            for slot, profile_id in zip(slots, created_ids):
                active = next(item for item in after.json() if item["slot_key"] == slot and item["is_enabled_in_slot"])
                assert active["id"] == profile_id
                assert active["adapter_key"] == "volcengine_ark_image"
                assert active["provider_config"]["secret_env_name"] == "ARK_API_KEY"
        finally:
            for slot in slots:
                original = originals[slot]
                restored = client.post(
                    f"/api/v1/production/model-slots/{slot}/bindings",
                    json={
                        "model_profile_id": original["id"],
                        "enabled": True,
                        "priority": original["priority"] or 100,
                        "weight": None,
                        "replace_existing": True,
                    },
                )
                assert restored.status_code == 201, restored.text
            for profile_id in created_ids:
                deleted = client.delete(f"/api/v1/production/v1-model-profiles/{profile_id}")
                assert deleted.status_code == 204, deleted.text


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


def test_v1_model_profile_rejects_sensitive_aliases_and_nested_headers() -> None:
    """大小写、连字符、下划线和嵌套 headers 均不能绕过配置白名单。"""

    unsafe_configs = [
        {"credential": "test-value"},
        {"CREDENTIAL": "test-value"},
        {"secret-key": "test-value"},
        {"Access_Token": "test-value"},
        {"vision_request_options": {"headers": {"Authorization": "test-value"}}},
    ]
    with TestClient(app) as client:
        for unsafe in unsafe_configs:
            response = client.post(
                "/api/v1/production/v1-model-profiles",
                json={
                    "slot_key": "VIDEO_ANALYSIS",
                    "adapter_key": "openai_compatible_vision",
                    "model_key": "security-negative-model",
                    "display_name": "安全配置拒绝测试",
                    "provider_config": {
                        "api_base_url": "https://relay.example/v1",
                        "secret_env_name": "YUNWU_REASONING_API_KEY",
                        "result_contract": "V1_REFERENCE_ANALYSIS",
                        **unsafe,
                    },
                },
            )
            assert response.status_code == 422, response.text
            assert "test-value" not in response.text


def test_multiple_profiles_can_share_secret_reference_and_historical_values_are_redacted() -> None:
    """共享同一环境变量引用仍合法；绕过 API 的历史异常字段不会从读取/快照泄漏。"""

    with TestClient(app) as client:
        project_id = client.post("/api/v1/projects", json={"title": "模型配置安全审计"}).json()["id"]
        first = client.post(
            "/api/v1/production/v1-model-profiles",
            json={
                "slot_key": "VIDEO_ANALYSIS",
                "adapter_key": "openai_compatible_vision",
                "model_key": "shared-reasoning-vision",
                "display_name": "共享推理 Key 的视觉模型",
                "provider_config": {
                    "api_base_url": "https://reasoning.example/v1",
                    "secret_env_name": "YUNWU_REASONING_API_KEY",
                    "result_contract": "V1_REFERENCE_ANALYSIS",
                },
            },
        )
        second = client.post(
            "/api/v1/production/v1-model-profiles",
            json={
                "slot_key": "STORY_GENERATE",
                "adapter_key": "openai_compatible",
                "model_key": "shared-reasoning-text",
                "display_name": "共享推理 Key 的文本模型",
                "provider_config": {
                    "api_base_url": "https://reasoning.example/v1",
                    "secret_env_name": "YUNWU_REASONING_API_KEY",
                },
            },
        )
        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        first_id = first.json()["id"]
        second_id = second.json()["id"]
        try:
            db = SessionLocal()
            try:
                profile = db.get(ModelProfile, first_id)
                assert profile is not None
                # 模拟历史异常记录：写入绕过 API 的字段后，所有读取边界必须继续脱敏。
                profile.provider_config = {
                    "api_base_url": "https://reasoning.example/v1",
                    "secret_env_name": "YUNWU_REASONING_API_KEY",
                    "credential": "historical-test-value",
                    "nested": {"headers": {"Cookie": "historical-cookie-value"}},
                }
                db.commit()
                snapshot = _profile_snapshot(first_id, db)
                assert "historical-test-value" not in repr(snapshot)
                assert "historical-cookie-value" not in repr(snapshot)
                assert snapshot["provider_config"]["credential"] == "[REDACTED]"

                # 即使某个已冻结的旧绑定带着异常字段，ModelInvocation 审计落库前也
                # 必须再次脱敏。这里在同一测试事务回滚，不残留伪调用记录。
                slot = db.scalar(select(ModelSlot).where(ModelSlot.slot_key == "VIDEO_ANALYSIS"))
                assert slot is not None
                audit_run = WorkflowRun(
                    project_id=project_id,
                    workflow_key="v1_security_audit",
                    status=RunStatus.PENDING,
                    input_snapshot={
                        "prompt_templates": {"VIDEO_ANALYSIS": {"content": "仅测试审计快照"}},
                    },
                )
                db.add(audit_run)
                db.flush()
                invocation = _invoke(
                    db,
                    run=audit_run,
                    slot_key="VIDEO_ANALYSIS",
                    task_type="VIDEO_ANALYSIS",
                    input_snapshot={},
                    binding={
                        "slot_id": slot.id,
                        "model_profile_id": first_id,
                        "profile_snapshot": {
                            "profile_id": first_id,
                            "adapter_key": "openai_compatible_vision",
                            "provider_config": {"credential": "audit-copy-value"},
                        },
                    },
                    idempotency_key="security-audit-invocation",
                )
                assert "audit-copy-value" not in repr(invocation.model_profile_snapshot)
                assert invocation.model_profile_snapshot["provider_config"]["credential"] == "[REDACTED]"
                db.rollback()
            finally:
                db.close()

            listed = client.get("/api/v1/production/v1-model-profiles")
            assert listed.status_code == 200, listed.text
            item = next(profile for profile in listed.json() if profile["id"] == first_id)
            assert item["provider_config"]["credential"] == "[REDACTED]"
            assert "historical-test-value" not in listed.text
            assert "historical-cookie-value" not in listed.text
        finally:
            for profile_id in (second_id, first_id):
                deleted = client.delete(f"/api/v1/production/v1-model-profiles/{profile_id}")
                assert deleted.status_code == 204, deleted.text


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
