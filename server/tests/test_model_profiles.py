"""步骤模型配置的安全性、版本化与启用门禁测试。"""

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.main import app
from app.models import ModelProfile


def test_model_profiles_seed_and_keep_unavailable_provider_inactive() -> None:
    """默认配置可用，未来中转站配置能登记但不能在未接入时误启用。"""

    with TestClient(app) as client:
        defaults = client.get("/api/v1/model-profiles")
        assert defaults.status_code == 200
        # 其他测试可能已保存未启用候选；默认可执行配置仍应恰好覆盖 8 个步骤。
        active_defaults = [item for item in defaults.json() if item["is_active"]]
        assert len(active_defaults) == 8
        assert all(item["adapter_available"] for item in active_defaults)

        candidate = client.post(
            "/api/v1/model-profiles",
            json={
                "step_key": "generate_storyboard_video_groups",
                "provider_key": "relay_provider",
                "model_key": "video-model-2026",
                "provider_config": {
                    "api_base_url": "https://relay.example/v1",
                    "secret_env_name": "RELAY_VIDEO_API_KEY",
                    "timeout_seconds": 180,
                },
                "activate": False,
            },
        )
        assert candidate.status_code == 201
        assert candidate.json()["version"] == 2
        assert not candidate.json()["adapter_available"]

        blocked_activation = client.post(f"/api/v1/model-profiles/{candidate.json()['id']}/activate")
        assert blocked_activation.status_code == 409

        openai_candidate = client.post(
            "/api/v1/model-profiles",
            json={
                "step_key": "generate_story_package",
                "provider_key": "openai_compatible",
                "model_key": "selected-model-name",
                "provider_config": {
                    "api_base_url": "https://yunwu.ai/v1",
                    "secret_env_name": "YUNWU_API_KEY",
                },
                "activate": False,
            },
        )
        assert openai_candidate.status_code == 201
        assert openai_candidate.json()["adapter_available"]

        image_candidate = client.post(
            "/api/v1/model-profiles",
            json={
                "step_key": "generate_storyboard_images",
                "provider_key": "openai_compatible_image",
                "model_key": "selected-image-model",
                "provider_config": {
                    "api_base_url": "https://yunwu.ai/v1",
                    "secret_env_name": "YUNWU_API_KEY",
                    "image_size": "1728x2304",
                },
                "activate": False,
            },
        )
        assert image_candidate.status_code == 201
        assert image_candidate.json()["adapter_available"]

        vision_candidate = client.post(
            "/api/v1/model-profiles",
            json={
                "step_key": "analyze_reference_mechanisms",
                "provider_key": "openai_compatible_vision",
                "model_key": "selected-vision-model",
                "provider_config": {
                    "api_base_url": "https://yunwu.ai/v1",
                    "secret_env_name": "YUNWU_API_KEY",
                    "frame_sample_count": 6,
                    "frame_extraction_timeout_seconds": 120,
                    "frame_max_bytes": 2097152,
                },
                "activate": False,
            },
        )
        assert vision_candidate.status_code == 201
        assert vision_candidate.json()["adapter_available"]

        transcription_candidate = client.post(
            "/api/v1/model-profiles",
            json={
                "step_key": "transcribe_reference_audio",
                "provider_key": "openai_compatible_transcription",
                "model_key": "selected-asr-model",
                "provider_config": {
                    "api_base_url": "https://yunwu.ai/v1",
                    "secret_env_name": "YUNWU_API_KEY",
                    "audio_max_duration_seconds": 180,
                    "audio_extraction_timeout_seconds": 120,
                    "audio_max_bytes": 8388608,
                },
                "activate": False,
            },
        )
        assert transcription_candidate.status_code == 201
        assert transcription_candidate.json()["adapter_available"]

        export_candidate = client.post(
            "/api/v1/model-profiles",
            json={
                "step_key": "assemble_final_video",
                "provider_key": "ffmpeg_concat",
                "model_key": "ffmpeg-concat-v1",
                "provider_config": {
                    "download_timeout_seconds": 120,
                    "max_clip_bytes": 524288000,
                    "max_output_bytes": 2147483648,
                    "render_timeout_seconds": 1800,
                },
                "activate": False,
            },
        )
        assert export_candidate.status_code == 201
        assert export_candidate.json()["adapter_available"]

        video_candidate = client.post(
            "/api/v1/model-profiles",
            json={
                "step_key": "generate_storyboard_video_groups",
                "provider_key": "configurable_async_video",
                "model_key": "selected-video-model",
                "provider_config": {
                    "api_base_url": "https://yunwu.ai",
                    "secret_env_name": "YUNWU_API_KEY",
                    "submit_path": "/luma/generations",
                    "query_path_template": "/luma/generations/{task_id}",
                    "image_input_mode": "top_level_url",
                },
                "activate": False,
            },
        )
        assert video_candidate.status_code == 201
        assert video_candidate.json()["adapter_available"]

        secret_attempt = client.post(
            "/api/v1/model-profiles",
            json={
                "step_key": "generate_storyboard_video_groups",
                "provider_key": "relay_provider",
                "model_key": "video-model-2026",
                "provider_config": {"api_key": "must-not-be-stored"},
                "activate": False,
            },
        )
        assert secret_attempt.status_code == 422


def test_legacy_model_profile_api_redacts_historical_sensitive_provider_config() -> None:
    """旧模型中心同样不应把绕过新白名单的历史数据直接回显给前端。"""

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/model-profiles",
            json={
                "step_key": "generate_story_package",
                "provider_key": "openai_compatible",
                "model_key": "legacy-security-candidate",
                "provider_config": {
                    "api_base_url": "https://reasoning.example/v1",
                    "secret_env_name": "YUNWU_REASONING_API_KEY",
                },
                "activate": False,
            },
        )
        assert created.status_code == 201, created.text
        profile_id = created.json()["id"]
        try:
            db = SessionLocal()
            try:
                profile = db.get(ModelProfile, profile_id)
                assert profile is not None
                profile.provider_config = {
                    "api_base_url": "https://reasoning.example/v1",
                    "secret_env_name": "YUNWU_REASONING_API_KEY",
                    "Access-Token": "legacy-api-value",
                }
                db.commit()
            finally:
                db.close()

            listed = client.get("/api/v1/model-profiles?step_key=generate_story_package")
            assert listed.status_code == 200, listed.text
            item = next(row for row in listed.json() if row["id"] == profile_id)
            assert item["provider_config"]["Access-Token"] == "[REDACTED]"
            assert "legacy-api-value" not in listed.text
        finally:
            db = SessionLocal()
            try:
                profile = db.get(ModelProfile, profile_id)
                if profile is not None:
                    db.delete(profile)
                    db.commit()
            finally:
                db.close()
