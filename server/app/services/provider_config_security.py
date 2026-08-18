"""模型 Provider 配置的白名单与脱敏边界。

``provider_config`` 是可审计的非敏感配置，不是供应商请求的任意 JSON 透传口。
真实鉴权始终只能由顶层 ``secret_env_name`` 间接指向服务器环境变量。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, status

from app.services.sensitive_data import REDACTED_VALUE, is_sensitive_key, redact_sensitive_data


_SHARED_METADATA_FIELDS = frozenset({"display_name", "estimated_cost_per_call", "currency"})
_OPENAI_BASE_FIELDS = frozenset({"api_base_url", "secret_env_name", "timeout_seconds"})
_SECRET_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_FORBIDDEN_SNAPSHOT_KEYS = frozenset({"header", "headers", "url", "endpoint"})
_SENSITIVE_URL_QUERY_KEYS = frozenset(
    {"authorization", "token", "access_token", "api_key", "apikey", "signature", "sig", "xamzsignature"}
)

# 每个已接入 Adapter 只能持久化其代码实际会读取的非敏感参数。请求扩展参数被
# 明确收敛到四个 ``*_request_options`` 容器；不能用根配置字段伪造请求头或凭证。
_ADAPTER_FIELDS: dict[str, frozenset[str]] = {
    "mock_provider": frozenset({"display_name", "local_only"}),
    "mock_v1": frozenset({"display_name", "local_only"}),
    "openai_compatible": _SHARED_METADATA_FIELDS | _OPENAI_BASE_FIELDS | frozenset({"temperature", "max_tokens"}),
    "openai_compatible_vision": _SHARED_METADATA_FIELDS | _OPENAI_BASE_FIELDS | frozenset(
        {
            "temperature",
            "max_tokens",
            "frame_sample_count",
            "frame_extraction_timeout_seconds",
            "frame_max_bytes",
            "result_contract",
            "vision_request_options",
        }
    ),
    "openai_compatible_transcription": _SHARED_METADATA_FIELDS | _OPENAI_BASE_FIELDS | frozenset(
        {
            "audio_max_duration_seconds",
            "audio_extraction_timeout_seconds",
            "audio_max_bytes",
            "transcription_request_options",
        }
    ),
    "openai_compatible_image": _SHARED_METADATA_FIELDS | _OPENAI_BASE_FIELDS | frozenset(
        {"image_size", "image_response_format", "reference_image_field", "image_request_options"}
    ),
    # 当前只支持云雾 Nano Banana 的最小 Fal 队列协议。它没有任意请求头、任意
    # 端点或模型市场配置入口；鉴权仍只能由 ``secret_env_name`` 读取服务器环境变量。
    "fal_queue_image": _SHARED_METADATA_FIELDS
    | _OPENAI_BASE_FIELDS
    | frozenset({"poll_interval_seconds", "max_poll_seconds"}),
    # 方舟 Seedream V1 只允许官方单图接口真正读取的字段。鉴权不允许通过
    # headers 或 token 配置透传，只能由 secret_env_name 间接读取 ARK_API_KEY。
    "volcengine_ark_image": _SHARED_METADATA_FIELDS
    | _OPENAI_BASE_FIELDS
    | frozenset({"size", "sequential_image_generation", "response_format", "watermark"}),
    "configurable_async_video": _SHARED_METADATA_FIELDS | _OPENAI_BASE_FIELDS | frozenset(
        {
            "submit_path",
            "query_path_template",
            "prompt_field",
            "image_field",
            "keyframes_field",
            "keyframe_name",
            "model_field",
            "end_image_field",
            "image_input_mode",
            "video_request_options",
            "task_id_path",
            "state_path",
            "error_message_path",
            "video_url_paths",
            "success_states",
            "failure_states",
            "poll_interval_seconds",
            "max_poll_seconds",
        }
    ),
    "volcengine_ark_video": _SHARED_METADATA_FIELDS
    | _OPENAI_BASE_FIELDS
    | frozenset(
        {
            "secret_env_name",
            "ratio",
            "duration",
            "resolution",
            "generate_audio",
            "watermark",
            "return_last_frame",
            "use_last_frame",
            "seed",
            "timeout_seconds",
            "poll_interval_seconds",
            "max_poll_seconds",
        }
    ),
    "ffmpeg_concat": _SHARED_METADATA_FIELDS
    | frozenset({"download_timeout_seconds", "max_clip_bytes", "max_output_bytes", "render_timeout_seconds"}),
}

# 尚未接入的候选只能登记最少的非敏感元数据，不能把它当作绕过白名单的容器。
_UNREGISTERED_CANDIDATE_FIELDS = _SHARED_METADATA_FIELDS | _OPENAI_BASE_FIELDS
_OPTION_FIELDS = frozenset(
    {
        "vision_request_options",
        "transcription_request_options",
        "image_request_options",
        "video_request_options",
    }
)
_SAFE_REQUEST_OPTION_FIELDS: dict[str, frozenset[str]] = {
    # 这些字段是已接入协议中不会携带鉴权信息、也不会覆盖 Adapter 自己管理输入的
    # 生成参数。若某供应商需要新参数，必须先在此处显式登记并新增测试。
    "vision_request_options": frozenset({"top_p", "presence_penalty", "frequency_penalty", "seed"}),
    "transcription_request_options": frozenset({"language", "prompt", "temperature"}),
    "image_request_options": frozenset(
        {"watermark", "quality", "style", "output_format", "seed", "steps", "guidance_scale", "aspect_ratio"}
    ),
    "video_request_options": frozenset(
        {"duration", "aspect_ratio", "fps", "resolution", "seed", "motion_strength", "camera_fixed", "generate_audio"}
    ),
}


@dataclass(frozen=True)
class ExecutionMetadataScan:
    """快照安全扫描的路径分类；从不携带原始配置值。"""

    allowed_execution_metadata: tuple[str, ...]
    sensitive_findings: tuple[str, ...]


def is_registered_adapter_key(adapter_key: object) -> bool:
    """只有代码已注册的 Adapter 标识可被固定到可执行配置中。"""

    return isinstance(adapter_key, str) and adapter_key.strip() in _ADAPTER_FIELDS


def _valid_api_base_url(value: object) -> bool:
    """Base URL 是非敏感的固定 HTTPS 地址，不能携带鉴权或签名状态。"""

    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return False
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        return False
    return True


def classify_execution_metadata(value: Any, *, path: str = "snapshot") -> ExecutionMetadataScan:
    """仅按路径分类历史快照：允许的执行元数据与真正敏感发现分开统计。

    该函数只返回字段路径，不读取或回显值；用于只读审计、测试及运维扫描。
    """

    allowed: list[str] = []
    sensitive: list[str] = []

    def redacted_only(item: Any) -> bool:
        """历史 API 脱敏占位符不是凭证泄露；真实值仍按敏感发现处理。"""

        if item == REDACTED_VALUE:
            return True
        if isinstance(item, dict):
            return bool(item) and all(redacted_only(nested) for nested in item.values())
        if isinstance(item, list):
            return bool(item) and all(redacted_only(nested) for nested in item)
        return False

    def walk(item: Any, current: str) -> None:
        if isinstance(item, dict):
            for raw_key, nested in item.items():
                key = str(raw_key)
                child = f"{current}.{key}"
                normalized = "".join(char for char in key.casefold() if char.isalnum())
                if key == "adapter_key":
                    (allowed if is_registered_adapter_key(nested) else sensitive).append(child)
                elif key == "secret_env_name":
                    (allowed if isinstance(nested, str) and _SECRET_ENV_NAME.fullmatch(nested) else sensitive).append(child)
                elif key == "api_base_url":
                    (allowed if _valid_api_base_url(nested) else sensitive).append(child)
                elif is_sensitive_key(key) or normalized in _FORBIDDEN_SNAPSHOT_KEYS:
                    # 旧审计记录中的 [REDACTED] 是防御性展示，不是 Key/Token 值。
                    # 扫描不将其计入真正的泄露发现，也绝不将其列为可执行元数据。
                    if not redacted_only(nested):
                        sensitive.append(child)
                walk(nested, child)
        elif isinstance(item, list):
            for index, nested in enumerate(item):
                walk(nested, f"{current}[{index}]")
        elif isinstance(item, str):
            parsed = urlsplit(item)
            query_names = {
                key.casefold().replace("-", "").replace("_", "")
                for part in parsed.query.split("&") if part
                for key in [part.split("=", 1)[0]]
            }
            if (
                item.casefold().startswith("data:")
                or "base64," in item.casefold()
                or "bearer " in item.casefold()
                or query_names & _SENSITIVE_URL_QUERY_KEYS
                or parsed.username is not None
                or parsed.password is not None
                # A controlled local media reference is the only absolute path
                # permitted in durable execution/result snapshots.  Arbitrary
                # filesystem paths would otherwise become a path traversal or
                # host disclosure channel in Model Lab inputs.
                or (item.startswith("/") and not item.startswith("/media/generated/"))
            ):
                sensitive.append(current)

    walk(value, path)
    return ExecutionMetadataScan(tuple(allowed), tuple(sensitive))


def normalize_provider_config(*, adapter_key: str, provider_config: dict[str, Any]) -> dict[str, Any]:
    """返回可安全入库的配置；未知字段或敏感字段一律以 422 拒绝。"""

    if not isinstance(provider_config, dict):
        _reject("模型配置必须是 JSON 对象")
    normalized = deepcopy(provider_config)
    # 先递归检查敏感字段，确保 ``api_key``、headers 等不会被“未知字段”提示掩盖，
    # 也让所有命名变体得到一致、明确的 422。
    _validate_mapping(normalized, path="provider_config", allow_secret_env_name=True)
    allowed = _ADAPTER_FIELDS.get(adapter_key, _UNREGISTERED_CANDIDATE_FIELDS)
    unknown = sorted(str(key) for key in normalized if key not in allowed)
    if unknown:
        _reject(f"当前 Adapter 不允许保存配置字段：{', '.join(unknown)}")
    _validate_execution_metadata(normalized)
    return normalized


def redact_provider_config(value: Any) -> dict[str, Any]:
    """防御性清理历史异常配置后再返回 API、快照或审计记录。"""

    if not isinstance(value, dict):
        return {}
    redacted = redact_sensitive_data(value)
    return redacted if isinstance(redacted, dict) else {}


def find_sensitive_provider_config_paths(value: Any, *, path: str = "provider_config") -> list[str]:
    """仅返回敏感字段路径，供既有数据库只读扫描使用，绝不读取字段值。"""

    paths: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            child_path = f"{path}.{key}"
            if is_sensitive_key(key):
                paths.append(child_path)
                continue
            paths.extend(find_sensitive_provider_config_paths(nested, path=child_path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            paths.extend(find_sensitive_provider_config_paths(nested, path=f"{path}[{index}]"))
    return paths


def _validate_mapping(value: dict[str, Any], *, path: str, allow_secret_env_name: bool) -> None:
    for key, nested in value.items():
        if not isinstance(key, str):
            _reject(f"{path} 的字段名必须是文本")
        child_path = f"{path}.{key}"
        if is_sensitive_key(key):
            _reject(f"{child_path} 属于敏感鉴权字段，不能填；请改用顶层 secret_env_name")
        if key == "secret_env_name" and not allow_secret_env_name:
            _reject(f"{child_path} 不能嵌套；只允许顶层 secret_env_name")
        if key == "headers":
            # 当前所有 HTTP Adapter 均固定自行生成 Authorization，未实现自定义 Header
            # 透传。拒绝而不是“存了但不生效”，同时关闭 headers.authorization/cookie 绕过。
            _reject(f"{child_path} 不受支持；鉴权只能使用顶层 secret_env_name")
        if key in _OPTION_FIELDS:
            if not isinstance(nested, dict):
                _reject(f"{child_path} 必须是 JSON 对象")
            _validate_request_options(nested, path=child_path, option_name=key)
        elif isinstance(nested, dict):
            _validate_mapping(nested, path=child_path, allow_secret_env_name=False)
        elif isinstance(nested, list):
            _validate_list(nested, path=child_path)


def _validate_list(value: list[Any], *, path: str) -> None:
    for index, nested in enumerate(value):
        if isinstance(nested, dict):
            _validate_mapping(nested, path=f"{path}[{index}]", allow_secret_env_name=False)
        elif isinstance(nested, list):
            _validate_list(nested, path=f"{path}[{index}]")


def _validate_request_options(value: dict[str, Any], *, path: str, option_name: str) -> None:
    """收敛供应商额外生成参数，阻止任意 JSON 透传及嵌套鉴权字段。"""

    allowed = _SAFE_REQUEST_OPTION_FIELDS[option_name]
    for key, nested in value.items():
        if not isinstance(key, str):
            _reject(f"{path} 的字段名必须是文本")
        child_path = f"{path}.{key}"
        if is_sensitive_key(key):
            _reject(f"{child_path} 属于敏感鉴权字段，不能填；请改用顶层 secret_env_name")
        if key == "headers":
            _reject(f"{child_path} 不受支持；鉴权只能使用顶层 secret_env_name")
        if key not in allowed:
            _reject(f"{child_path} 不是当前 Adapter 已声明的安全参数")
        if isinstance(nested, dict):
            _reject(f"{child_path} 不支持嵌套对象")
        if isinstance(nested, list):
            _validate_list(nested, path=child_path)


def _reject(detail: str) -> None:
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _validate_execution_metadata(value: dict[str, Any]) -> None:
    """入口配置只能引用受控 Adapter、环境变量名及无鉴权 HTTPS Base URL。"""

    secret_env_name = value.get("secret_env_name")
    if secret_env_name is not None and (
        not isinstance(secret_env_name, str) or not _SECRET_ENV_NAME.fullmatch(secret_env_name)
    ):
        _reject("secret_env_name 必须是 2 至 128 位的大写环境变量名称")
    base_url = value.get("api_base_url")
    if base_url is not None and not _valid_api_base_url(base_url):
        _reject("api_base_url 必须是无 userinfo、query 或 fragment 的 HTTPS 固定服务地址")


def assert_safe_execution_metadata(*, adapter_key: object, provider_config: dict[str, Any]) -> None:
    """在冻结可执行快照前验证 Adapter 与连接元数据。

    非执行的候选 Profile 仍可由模型中心登记为未接入状态；只有实际进入
    Workflow/StoryRun 冻结的 Profile 必须是代码已注册的 Adapter。
    """

    if not is_registered_adapter_key(adapter_key):
        _reject("adapter_key 未在当前代码包注册，不能冻结为可执行配置")
    if not isinstance(provider_config, dict):
        _reject("可执行模型配置必须是 JSON 对象")
    _validate_execution_metadata(provider_config)
    # 历史异常数据或绕过模型中心的写入也不能在新运行冻结时把鉴权字段、Data URL
    # 或带签名的 URL 带进快照。分类器只返回路径，因此这一检查不会接触或回显值。
    snapshot_scan = classify_execution_metadata(
        {"adapter_key": adapter_key, "provider_config": provider_config},
        path="execution_snapshot",
    )
    if snapshot_scan.sensitive_findings:
        _reject("可执行模型配置包含敏感或不合法的执行字段，不能冻结")
