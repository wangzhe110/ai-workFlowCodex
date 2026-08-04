"""V1 模型中心的候选创建、人工切换和密钥边界测试。"""

from fastapi.testclient import TestClient

from app.main import app


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
