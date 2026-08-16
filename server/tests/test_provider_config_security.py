"""模型渠道配置的安全边界测试。

这些测试只使用假值和本地 SQLite；不会读取环境中任何真实 Key，也不会发起模型调用。
"""

import pytest
from fastapi import HTTPException

from app.services.provider_config_security import (
    find_sensitive_provider_config_paths,
    normalize_provider_config,
    redact_provider_config,
)
from app.services.sensitive_data import sanitize_error_summary


@pytest.mark.parametrize(
    ("adapter_key", "provider_config"),
    [
        ("mock_v1", {"display_name": "本地测试", "local_only": True}),
        (
            "openai_compatible",
            {
                "api_base_url": "https://reasoning.example/v1",
                "secret_env_name": "YUNWU_REASONING_API_KEY",
                "temperature": 0.2,
                "max_tokens": 1024,
            },
        ),
        (
            "openai_compatible_vision",
            {
                "api_base_url": "https://reasoning.example/v1",
                "secret_env_name": "YUNWU_REASONING_API_KEY",
                "result_contract": "V1_REFERENCE_ANALYSIS",
                "vision_request_options": {"top_p": 0.9},
            },
        ),
        (
            "openai_compatible_transcription",
            {
                "api_base_url": "https://reasoning.example/v1",
                "secret_env_name": "YUNWU_REASONING_API_KEY",
                "transcription_request_options": {"language": "zh"},
            },
        ),
        (
            "openai_compatible_image",
            {
                "api_base_url": "https://image.example/v1",
                "secret_env_name": "YUNWU_IMAGE_API_KEY",
                "image_size": "1024x1024",
                "image_request_options": {"watermark": False},
            },
        ),
        (
            "fal_queue_image",
            {
                "api_base_url": "https://yunwu.ai",
                "secret_env_name": "YUNWU_IMAGE_API_KEY",
                "poll_interval_seconds": 3,
                "max_poll_seconds": 600,
            },
        ),
        (
            "volcengine_ark_image",
            {
                "api_base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "secret_env_name": "ARK_API_KEY",
                "timeout_seconds": 30,
                "size": "2K",
                "sequential_image_generation": "disabled",
                "response_format": "url",
                "watermark": False,
            },
        ),
        (
            "configurable_async_video",
            {
                "api_base_url": "https://video.example/v1",
                "secret_env_name": "VIDEO_API_KEY",
                "submit_path": "/tasks",
                "query_path_template": "/tasks/{task_id}",
                "video_request_options": {"duration": "5s", "aspect_ratio": "9:16"},
            },
        ),
        (
            "volcengine_ark_video",
            {"secret_env_name": "ARK_API_KEY", "duration": 5, "resolution": "720p"},
        ),
        ("ffmpeg_concat", {"download_timeout_seconds": 120, "max_clip_bytes": 10_000}),
    ],
)
def test_each_real_adapter_accepts_only_its_declared_safe_configuration(
    adapter_key: str, provider_config: dict[str, object]
) -> None:
    """正常模型参数保留兼容；每个适配器的安全字段由显式白名单定义。"""

    assert normalize_provider_config(adapter_key=adapter_key, provider_config=provider_config) == provider_config


@pytest.mark.parametrize(
    "provider_config",
    [
        {"api_base_url": "https://relay.example/v1", "credential": "test-value"},
        {"api_base_url": "https://relay.example/v1", "CREDENTIAL": "test-value"},
        {"api_base_url": "https://relay.example/v1", "secret-key": "test-value"},
        {"api_base_url": "https://relay.example/v1", "Access_Token": "test-value"},
        {
            "api_base_url": "https://relay.example/v1",
            "vision_request_options": {"headers": {"Authorization": "test-value"}},
        },
        {
            "api_base_url": "https://relay.example/v1",
            "image_request_options": {"headers": {"cookie": "test-value"}},
        },
        {"api_base_url": "https://relay.example/v1", "undeclared_vendor_field": True},
    ],
)
def test_sensitive_or_undeclared_configuration_cannot_bypass_allowlist(
    provider_config: dict[str, object]
) -> None:
    with pytest.raises(HTTPException) as raised:
        normalize_provider_config(adapter_key="openai_compatible_vision", provider_config=provider_config)

    assert raised.value.status_code == 422
    assert "test-value" not in str(raised.value.detail)


def test_historical_abnormal_config_is_redacted_and_scan_returns_paths_only() -> None:
    """即使历史数据绕开 API，读取、快照和扫描也不会暴露其中的值。"""

    historical = {
        "api_base_url": "https://relay.example/v1",
        "credential": "legacy-value-one",
        "nested": {"headers": {"Authorization": "legacy-value-two"}},
        "variants": [{"Secret_Key": "legacy-value-three"}],
    }
    redacted = redact_provider_config(historical)
    rendered = repr(redacted)

    assert "legacy-value-one" not in rendered
    assert "legacy-value-two" not in rendered
    assert "legacy-value-three" not in rendered
    assert redacted["credential"] == "[REDACTED]"
    assert redacted["nested"]["headers"]["Authorization"] == "[REDACTED]"
    assert find_sensitive_provider_config_paths(historical) == [
        "provider_config.credential",
        "provider_config.nested.headers.Authorization",
        "provider_config.variants[0].Secret_Key",
    ]


def test_ark_image_provider_rejects_unknown_configuration_fields() -> None:
    """官方方舟图片协议没有“任意参数透传”口，未知字段必须明确返回 422。"""

    with pytest.raises(HTTPException) as raised:
        normalize_provider_config(
            adapter_key="volcengine_ark_image",
            provider_config={
                "api_base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "secret_env_name": "ARK_API_KEY",
                "size": "2K",
                "unapproved_vendor_option": True,
            },
        )
    assert raised.value.status_code == 422
    assert "unapproved_vendor_option" in str(raised.value.detail)


@pytest.mark.parametrize(
    "message",
    [
        "credential=test-value",
        "Access_Token: test-value",
        "headers.authorization=test-value",
        "set-cookie: test-value",
        "secret-key = test-value",
    ],
)
def test_persisted_error_summaries_redact_new_sensitive_aliases(message: str) -> None:
    """供应商错误、任务日志和审计的文本边界也不能回显认证值。"""

    sanitized = sanitize_error_summary(message)
    assert "test-value" not in sanitized
    assert "[REDACTED]" in sanitized
