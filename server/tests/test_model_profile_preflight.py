"""模型候选预检必须不生成内容、不暴露密钥，并能指出部署缺项。"""

from fastapi.testclient import TestClient

from app.main import app


def test_mock_model_profile_preflight_passes_without_external_request() -> None:
    """内置模拟配置用于零密钥联调，预检应明确给出通过结果。"""

    with TestClient(app) as client:
        profiles = client.get("/api/v1/model-profiles").json()
        mock_profile = next(item for item in profiles if item["step_key"] == "generate_story_package")
        response = client.post(f"/api/v1/model-profiles/{mock_profile['id']}/preflight")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is True
    assert {item["key"] for item in payload["checks"]} == {"adapter", "config", "runtime"}
    assert all(item["status"] == "passed" for item in payload["checks"])


def test_remote_model_preflight_reports_missing_secret_without_contacting_provider(monkeypatch) -> None:
    """候选配置可先保存；密钥未注入时预检只报告变量名，绝不发起外部请求。"""

    monkeypatch.delenv("PREFLIGHT_TEST_API_KEY", raising=False)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/model-profiles",
            json={
                "step_key": "generate_original_topics",
                "provider_key": "openai_compatible",
                "model_key": "candidate-text-model",
                "provider_config": {
                    "api_base_url": "https://relay.example/v1",
                    "secret_env_name": "PREFLIGHT_TEST_API_KEY",
                },
                "activate": False,
            },
        )
        assert created.status_code == 201
        response = client.post(f"/api/v1/model-profiles/{created.json()['id']}/preflight")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is False
    secret_check = next(item for item in payload["checks"] if item["key"] == "secret")
    assert secret_check["status"] == "failed"
    assert "PREFLIGHT_TEST_API_KEY" in secret_check["message"]
    assert "candidate-text-model" not in secret_check["message"]
