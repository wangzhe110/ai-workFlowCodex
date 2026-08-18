"""版本化模型能力、质量预设与运行参数解析。

这里是生成参数的唯一权威入口：Adapter 定义协议边界，Profile 在边界内声明可用
能力，Workflow 创建时把解析后的结果冻结到快照。它故意不是“任意 JSON 透传器”，
因此不能携带 Key、Header、URL 或供应商私有请求字段。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi import HTTPException, status

from app.services.sensitive_data import is_sensitive_key, redact_sensitive_data


SCHEMA_VERSION = 1

# 当前仅声明已经被 Adapter 代码实际读取和校验的参数。新模型能力必须先扩展
# Adapter，再在这里登记，避免 UI 先出现一个供应商并不支持的“选项”。
_ADAPTER_CAPABILITIES: dict[str, dict[str, Any]] = {
    # 历史联调 Profile 仍可被旧测试/旧任务读取，但不会伪装为真实生产能力。
    "mock_v1": {"capability": "text", "parameters": {}},
    "openai_compatible": {
        "capability": "text",
        "parameters": {
            "temperature": {"kind": "number", "minimum": 0.0, "maximum": 2.0},
            "max_tokens": {"kind": "integer", "minimum": 1, "maximum": 32768},
        },
    },
    "openai_compatible_vision": {
        "capability": "text",
        "parameters": {
            "temperature": {"kind": "number", "minimum": 0.0, "maximum": 2.0},
            "max_tokens": {"kind": "integer", "minimum": 1, "maximum": 32768},
        },
    },
    # 这三个是已经存在的历史 Adapter。它们保留自己的历史 Profile 及冻结快照，
    # 也需要能在模型中心中安全展示“旧版本能力不完整”的兼容视图；不能因新字段
    # 上线而使历史任务或 Profile 列表无法读取。
    "openai_compatible_image": {
        "capability": "image",
        "parameters": {
            "input_mode": {"kind": "enum", "values": ["text", "reference"]},
            "num_images": {"kind": "enum", "values": [1]},
        },
    },
    "fal_queue_image": {
        "capability": "image",
        "parameters": {
            "input_mode": {"kind": "enum", "values": ["text", "reference"]},
            "num_images": {"kind": "enum", "values": [1]},
        },
    },
    "volcengine_ark_image": {
        "capability": "image",
        "parameters": {
            "input_mode": {"kind": "enum", "values": ["text", "reference"]},
            # Seedream V1 当前生产 Adapter 只验证并发送 2K 单图；不能展示未知画幅。
            "size": {"kind": "enum", "values": ["2K"]},
            "num_images": {"kind": "enum", "values": [1]},
            "watermark": {"kind": "enum", "values": [False]},
        },
    },
    "volcengine_ark_video": {
        "capability": "video",
        "parameters": {
            "input_mode": {"kind": "enum", "values": ["first_frame"]},
            "duration": {"kind": "integer", "minimum": 2, "maximum": 12},
            # 已跑通的 Seedance 2.5 Profile 当前只声明 480p；不同 Profile 可以缩小
            # 或扩展自己的允许列表，但不能凭 UI 猜测出未知分辨率。
            "resolution": {"kind": "enum", "values": ["480p", "720p", "1080p"]},
            "aspect_ratio": {"kind": "enum", "values": ["16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"]},
            "generate_audio": {"kind": "enum", "values": [False, True]},
            "watermark": {"kind": "enum", "values": [False, True]},
        },
    },
    "configurable_async_video": {
        "capability": "video",
        "parameters": {
            "input_mode": {"kind": "enum", "values": ["first_frame"]},
            "duration": {"kind": "integer", "minimum": 2, "maximum": 12},
            "aspect_ratio": {"kind": "enum", "values": ["16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"]},
            "generate_audio": {"kind": "enum", "values": [False, True]},
            "watermark": {"kind": "enum", "values": [False, True]},
        },
    },
    "ffmpeg_concat": {
        "capability": "local_compose",
        "parameters": {
            # 先作为可冻结的交付意图保留；当前 concat 仍使用既有统一转码默认值，
            # 不会因配置功能上线而改变已经验证过的合成行为。
            "delivery_width": {"kind": "integer", "minimum": 320, "maximum": 7680},
            "delivery_height": {"kind": "integer", "minimum": 320, "maximum": 4320},
            "fps": {"kind": "integer", "minimum": 1, "maximum": 120},
            "codec": {"kind": "enum", "values": ["h264"]},
            "crf": {"kind": "integer", "minimum": 0, "maximum": 51},
        },
    },
}


def _error(detail: str) -> None:
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _adapter(snapshot: dict[str, Any]) -> str:
    adapter = snapshot.get("adapter_key") or snapshot.get("provider_key")
    if not isinstance(adapter, str) or adapter not in _ADAPTER_CAPABILITIES:
        _error("当前 Adapter 尚未声明可配置的生成参数能力")
    return adapter


def _assert_safe(value: Any, *, path: str = "parameter_config") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                _error(f"{path} 的字段名必须是文本")
            if is_sensitive_key(key) or key.lower() in {"headers", "url", "endpoint", "authorization", "cookie"}:
                _error(f"{path}.{key} 属于敏感或连接配置，不能保存到参数能力配置")
            _assert_safe(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_safe(nested, path=f"{path}[{index}]")
    elif isinstance(value, str) and value.startswith("data:image/"):
        _error(f"{path} 不能保存 Data URL")


def _default_config(adapter: str, provider_config: dict[str, Any]) -> dict[str, Any]:
    """为旧 Profile 生成不改变现有实际默认行为的兼容能力视图。"""

    capability = deepcopy(_ADAPTER_CAPABILITIES[adapter])
    params = capability["parameters"]
    defaults: dict[str, Any] = {}
    if adapter in {"openai_compatible", "openai_compatible_vision"}:
        defaults = {"temperature": provider_config.get("temperature", 0.2)}
        if provider_config.get("max_tokens") is not None:
            defaults["max_tokens"] = provider_config["max_tokens"]
    elif adapter == "volcengine_ark_image":
        defaults = {"input_mode": "text", "size": provider_config.get("size", "2K"), "num_images": 1, "watermark": provider_config.get("watermark", False)}
    elif adapter in {"openai_compatible_image", "fal_queue_image"}:
        defaults = {"input_mode": "text", "num_images": 1}
    elif adapter in {"volcengine_ark_video", "configurable_async_video"}:
        defaults = {
            "input_mode": "first_frame",
            "duration": provider_config.get("duration", 5),
            **({"resolution": provider_config.get("resolution", "480p")} if "resolution" in params else {}),
            "aspect_ratio": provider_config.get("ratio", "9:16"),
            "generate_audio": provider_config.get("generate_audio", False),
            "watermark": provider_config.get("watermark", False),
        }
    elif adapter == "ffmpeg_concat":
        defaults = {"codec": "h264", "crf": 23, "fps": 24}
    elif adapter == "mock_v1":
        defaults = {}

    # Profile 的 allowed 列表若没有存储，则按 Adapter 的实际硬边界生成。预设均保持
    # 当前默认值，绝不在旧配置上自动降质或自动提升费用。
    return {
        "schema_version": SCHEMA_VERSION,
        "capability": capability["capability"],
        "supported_parameters": deepcopy(params),
        "defaults": defaults,
        "presets": {"preview": deepcopy(defaults), "standard": deepcopy(defaults), "high": deepcopy(defaults)},
    }


def effective_parameter_config(snapshot: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """返回安全能力配置和是否为该 Profile 显式保存的完整版本。"""

    adapter = _adapter(snapshot)
    raw = snapshot.get("parameter_config")
    if not isinstance(raw, dict) or not raw:
        return _default_config(adapter, snapshot.get("provider_config") or {}), False
    return validate_parameter_config(adapter, raw), True


def _validate_value(name: str, value: Any, spec: dict[str, Any]) -> None:
    kind = spec["kind"]
    if kind == "enum":
        if value not in spec["values"]:
            _error(f"参数 {name} 不支持值 {value!r}")
        return
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int) or not spec["minimum"] <= value <= spec["maximum"]:
            _error(f"参数 {name} 必须在 {spec['minimum']} 至 {spec['maximum']} 之间")
        return
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not spec["minimum"] <= float(value) <= spec["maximum"]:
            _error(f"参数 {name} 必须在 {spec['minimum']} 至 {spec['maximum']} 之间")
        return
    _error(f"参数 {name} 类型未声明")


def validate_parameter_config(adapter: str, value: dict[str, Any]) -> dict[str, Any]:
    """验证 Profile 可声明的能力、默认值与质量预设；不允许任意字段。"""

    if adapter not in _ADAPTER_CAPABILITIES:
        _error("当前 Adapter 未接入参数能力配置")
    _assert_safe(value)
    allowed_top = {"schema_version", "capability", "supported_parameters", "defaults", "presets"}
    unknown_top = sorted(set(value) - allowed_top)
    if unknown_top:
        _error(f"parameter_config 不支持字段：{', '.join(unknown_top)}")
    if value.get("schema_version") != SCHEMA_VERSION:
        _error(f"parameter_config.schema_version 必须为 {SCHEMA_VERSION}")
    contract = _ADAPTER_CAPABILITIES[adapter]
    if value.get("capability") != contract["capability"]:
        _error("parameter_config.capability 与 Adapter 能力不一致")
    supported = value.get("supported_parameters")
    defaults = value.get("defaults")
    presets = value.get("presets")
    if not isinstance(supported, dict) or not isinstance(defaults, dict) or not isinstance(presets, dict):
        _error("parameter_config 必须包含对象形式的 supported_parameters、defaults 和 presets")
    declared = contract["parameters"]
    unknown = sorted(set(supported) - set(declared))
    if unknown:
        _error(f"Adapter 不支持参数：{', '.join(unknown)}")
    normalized_supported: dict[str, Any] = {}
    for name, override in supported.items():
        if not isinstance(override, dict):
            _error(f"supported_parameters.{name} 必须是对象")
        base = deepcopy(declared[name])
        # API 会回传完整的 Adapter 字段规范；复制 Profile 或前端再次保存时原样带回
        # 该规范是正常路径，不能误判为试图扩展能力。除此之外只准收窄 enum。
        if override == base:
            normalized_supported[name] = base
            continue
        # 只允许通过 enum values 收窄可选项，不允许改类型/范围扩大协议边界。
        # 已保存的 Profile 会有标准化后的 ``kind + values`` 形式，前端复制/编辑时
        # 会原样带回；该格式与仅传 values 一样合法。
        if base["kind"] == "enum":
            if set(override) - {"kind", "values"} or override.get("kind", "enum") != "enum":
                _error(f"supported_parameters.{name} 只能收窄 values")
            if "values" not in override or not isinstance(override["values"], list) or not override["values"]:
                _error(f"supported_parameters.{name}.values 无效")
            if any(item not in base["values"] for item in override["values"]):
                _error(f"supported_parameters.{name}.values 超出 Adapter 支持范围")
            base["values"] = list(dict.fromkeys(override["values"]))
        else:
            # 数值范围同样允许 Profile 在 Adapter 的协议边界内收窄。例如不同视频
            # 模型可分别只声明 2-5 秒或 5-12 秒，前端因此不会误展示不可用时长。
            # 绝不允许扩大范围、修改数值类型或偷偷替换为另一个值集合。
            allowed_fields = {"kind", "minimum", "maximum"}
            if set(override) - allowed_fields or override.get("kind", base["kind"]) != base["kind"]:
                _error(f"supported_parameters.{name} 只能收窄 Adapter 的数值范围")
            minimum = override.get("minimum", base["minimum"])
            maximum = override.get("maximum", base["maximum"])
            if (
                isinstance(minimum, bool)
                or isinstance(maximum, bool)
                or not isinstance(minimum, (int, float))
                or not isinstance(maximum, (int, float))
                or (base["kind"] == "integer" and (not isinstance(minimum, int) or not isinstance(maximum, int)))
                or minimum < base["minimum"]
                or maximum > base["maximum"]
                or minimum > maximum
            ):
                _error(f"supported_parameters.{name} 的数值范围超出 Adapter 支持边界")
            base["minimum"] = minimum
            base["maximum"] = maximum
        normalized_supported[name] = base
    for name, param_value in defaults.items():
        if name not in normalized_supported:
            _error(f"defaults.{name} 未被当前 Profile 声明支持")
        _validate_value(name, param_value, normalized_supported[name])
    normalized_presets: dict[str, dict[str, Any]] = {}
    for preset in ("preview", "standard", "high"):
        item = presets.get(preset)
        if item is None:
            continue
        if not isinstance(item, dict):
            _error(f"presets.{preset} 必须是对象")
        for name, param_value in item.items():
            if name not in normalized_supported:
                _error(f"presets.{preset}.{name} 未被当前 Profile 声明支持")
            _validate_value(name, param_value, normalized_supported[name])
        normalized_presets[preset] = deepcopy(item)
    if "standard" not in normalized_presets:
        _error("parameter_config 必须声明 standard 质量预设")
    return {
        "schema_version": SCHEMA_VERSION,
        "capability": contract["capability"],
        "supported_parameters": normalized_supported,
        "defaults": deepcopy(defaults),
        "presets": normalized_presets,
    }


def resolve_effective_model_parameters(
    profile_snapshot: dict[str, Any],
    *,
    preset: str = "standard",
    run_overrides: dict[str, Any] | None = None,
    execution_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """以固定顺序解析参数，并返回可审计的无敏感冻结结果。"""

    config, explicit = effective_parameter_config(profile_snapshot)
    if preset not in {"preview", "standard", "high"}:
        _error("质量预设仅支持 preview、standard 或 high")
    presets = config["presets"]
    if preset not in presets:
        _error(f"当前模型 Profile 不支持 {preset} 质量预设")
    overrides = deepcopy(run_overrides or {})
    _assert_safe(overrides, path="parameter_overrides")
    supported = config["supported_parameters"]
    # 旧客户端把画幅叫 ratio；把它只视作 API 兼容别名，不允许两个名字同时表达
    # 两个值。之后首帧规则会显式记录它被省略，绝不会把 ratio 发送给 Seedance。
    requested_ratio = "ratio" in overrides
    if requested_ratio:
        if "aspect_ratio" in overrides:
            _error("parameter_overrides 不能同时传 ratio 和 aspect_ratio")
        overrides["aspect_ratio"] = overrides.pop("ratio")
    unknown = sorted(set(overrides) - set(supported))
    if unknown:
        _error(f"当前模型不支持参数：{', '.join(unknown)}")
    effective = deepcopy(config["defaults"])
    sources = {key: "profile_default" for key in effective}
    for key, value in presets[preset].items():
        effective[key] = value
        sources[key] = f"preset:{preset}"
    for key, value in overrides.items():
        _validate_value(key, value, supported[key])
        effective[key] = value
        sources[key] = "run_override"
    omitted: list[dict[str, str]] = []
    context = execution_context or {}
    # Seedance 首帧图生视频画幅由首帧决定；旧客户端传 ratio/aspect_ratio 时不能
    # 偷偷发送，也不能默默替换为别的画幅。记录省略原因供审计与恢复使用。
    if config["capability"] == "video" and effective.get("input_mode") == "first_frame":
        if "aspect_ratio" in effective:
            effective.pop("aspect_ratio")
            sources.pop("aspect_ratio", None)
            omitted.append({"parameter": "aspect_ratio", "reason": "first_frame_mode_uses_source_frame_aspect_ratio"})
        if requested_ratio:
            omitted.append({"parameter": "ratio", "reason": "first_frame_mode_uses_source_frame_aspect_ratio"})
    return {
        "schema_version": config["schema_version"],
        "capability": config["capability"],
        "selected_preset": preset,
        "requested_overrides": redact_sensitive_data(overrides),
        "effective_parameters": redact_sensitive_data(effective),
        "parameter_sources": sources,
        "omitted_parameters": omitted,
        "parameter_config_complete": explicit,
        "execution_context": {key: value for key, value in context.items() if key in {"operation", "input_mode"}},
    }


def profile_parameter_config(adapter: str, provider_config: dict[str, Any], parameter_config: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    """给 API/快照使用的 Profile 包装，兼容历史空字段。"""

    return effective_parameter_config({"adapter_key": adapter, "provider_config": provider_config, "parameter_config": parameter_config or {}})


def apply_effective_parameters(profile_snapshot: dict[str, Any]) -> dict[str, Any]:
    """构造仅供 Worker 内存使用的 Adapter 快照，不修改已冻结原对象。"""

    result = deepcopy(profile_snapshot)
    resolution = result.get("parameter_resolution") or {}
    effective = resolution.get("effective_parameters") if isinstance(resolution, dict) else None
    if not isinstance(effective, dict):
        return result
    config = deepcopy(result.get("provider_config") or {})
    for key, value in effective.items():
        if key in {"input_mode", "num_images", "delivery_width", "delivery_height", "fps", "codec", "crf"}:
            continue
        if key == "aspect_ratio":
            config["ratio"] = value
        else:
            config[key] = value
    result["provider_config"] = config
    return result
