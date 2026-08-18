"""模型渠道配置的安全边界测试。

这些测试只使用假值和本地 SQLite；不会读取环境中任何真实 Key，也不会发起模型调用。
"""

import pytest
from fastapi import HTTPException

from app.services.provider_config_security import (
    assert_safe_execution_metadata,
    classify_execution_metadata,
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


def test_execution_snapshot_allows_registered_adapter_and_safe_connection_metadata(monkeypatch) -> None:
    """冻结快照保留 Adapter、固定 HTTPS 地址与变量名，但绝不读取变量值。"""

    monkeypatch.setenv("UNIT_TEST_EXECUTION_METADATA_KEY", "must-never-be-read")
    config = {
        "api_base_url": "https://reasoning.example/v1",
        "secret_env_name": "UNIT_TEST_EXECUTION_METADATA_KEY",
        "temperature": 0,
        "max_tokens": 64,
    }
    assert_safe_execution_metadata(adapter_key="openai_compatible", provider_config=config)
    scan = classify_execution_metadata(
        {"adapter_key": "openai_compatible", "provider_config": config}, path="snapshot"
    )
    assert scan.sensitive_findings == ()
    assert set(scan.allowed_execution_metadata) == {
        "snapshot.adapter_key",
        "snapshot.provider_config.api_base_url",
        "snapshot.provider_config.secret_env_name",
    }
    # 校验函数没有读取环境变量，快照中也只保存变量名称。
    assert "must-never-be-read" not in repr(config)


@pytest.mark.parametrize(
    ("adapter_key", "provider_config"),
    [
        ("not_registered_adapter", {}),
        ("openai_compatible", {"secret_env_name": "lowercase_key"}),
        ("openai_compatible", {"api_base_url": "https://user:pass@reasoning.example/v1"}),
        ("openai_compatible", {"api_base_url": "https://reasoning.example/v1?token=forbidden"}),
        ("openai_compatible", {"api_base_url": "https://reasoning.example/v1#fragment"}),
        ("openai_compatible", {"api_base_url": "data:text/plain;base64,forbidden"}),
        ("openai_compatible", {"headers": {"Authorization": "forbidden"}}),
    ],
)
def test_execution_snapshot_rejects_unregistered_or_unsafe_metadata(
    adapter_key: str, provider_config: dict[str, object]
) -> None:
    with pytest.raises(HTTPException) as raised:
        assert_safe_execution_metadata(adapter_key=adapter_key, provider_config=provider_config)
    assert raised.value.status_code == 422


def test_execution_snapshot_scanner_separates_metadata_from_sensitive_values() -> None:
    """扫描只返回路径：合法执行元数据不计为敏感，认证和签名内容必须计入。"""

    scan = classify_execution_metadata(
        {
            "adapter_key": "openai_compatible",
            "provider_config": {
                "api_base_url": "https://reasoning.example/v1",
                "secret_env_name": "YUNWU_REASONING_API_KEY",
            },
            "headers": {"Authorization": "not-printed"},
            "image": "data:image/png;base64,not-printed",
            "result_url": "https://cdn.example/result?signature=not-printed",
        },
        path="snapshot",
    )
    assert len(scan.allowed_execution_metadata) == 3
    assert set(scan.sensitive_findings) == {
        "snapshot.headers",
        "snapshot.headers.Authorization",
        "snapshot.image",
        "snapshot.result_url",
    }


def test_execution_snapshot_scanner_rejects_arbitrary_absolute_path_but_allows_local_media_reference() -> None:
    scan = classify_execution_metadata(
        {
            "safe_media": "/media/generated/projects/project/image/v1.png",
            "unsafe_path": "/private/host-only-file",
        },
        path="snapshot",
    )
    assert scan.allowed_execution_metadata == ()
    assert scan.sensitive_findings == ("snapshot.unsafe_path",)


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
