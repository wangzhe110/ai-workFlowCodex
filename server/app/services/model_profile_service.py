"""模型步骤配置的版本化管理与运行快照选择。

模型 Key、第三方地址和非敏感参数可配置；真实 API Key 只能以环境变量名称引用，
不能写入数据库、日志或前端响应。配置变化新增版本而非覆盖旧版本。
"""

from copy import deepcopy
import json
import os
import re
import subprocess
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ModelEvaluation, ModelProfile
from app.services.provider_config_security import normalize_provider_config, redact_provider_config


DEFAULT_PROFILES: dict[str, dict[str, Any]] = {
    "transcribe_reference_audio": {
        "provider_key": "mock_provider",
        "model_key": "mock-audio-transcription-v1",
        "provider_config": {"display_name": "本地模拟语音转写（不读取音轨）"},
    },
    "analyze_reference_mechanisms": {
        "provider_key": "mock_provider",
        "model_key": "mock-video-understanding-v1",
        "provider_config": {"display_name": "本地模拟视频分析"},
    },
    "generate_original_topics": {
        "provider_key": "mock_provider",
        "model_key": "mock-topic-generation-v1",
        "provider_config": {"display_name": "本地模拟选题生成"},
    },
    "generate_story_package": {
        "provider_key": "mock_provider",
        "model_key": "mock-story-generation-v1",
        "provider_config": {"display_name": "本地模拟故事生成"},
    },
    "generate_storyboard": {
        "provider_key": "mock_provider",
        "model_key": "mock-storyboard-generation-v1",
        "provider_config": {"display_name": "本地模拟分镜生成"},
    },
    "generate_storyboard_images": {
        "provider_key": "mock_provider",
        "model_key": "mock-image-generation-v1",
        "provider_config": {"display_name": "本地模拟分镜图片"},
    },
    "generate_storyboard_video_groups": {
        "provider_key": "mock_provider",
        "model_key": "mock-video-generation-v1",
        "provider_config": {"display_name": "本地模拟视频片段"},
    },
    "assemble_final_video": {
        "provider_key": "mock_provider",
        "model_key": "mock-final-video-export-v1",
        "provider_config": {"display_name": "本地模拟完整成片"},
    },
}

_ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_FIELD_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
_JSON_PATH_PATTERN = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*$")
OPENAI_COMPATIBLE_TEXT_STEPS = {
    "generate_original_topics",
    "generate_story_package",
    "generate_storyboard",
}
OPENAI_COMPATIBLE_IMAGE_STEPS = {"generate_storyboard_images"}
CONFIGURABLE_ASYNC_VIDEO_STEPS = {"generate_storyboard_video_groups"}
VOLCENGINE_ARK_VIDEO_STEPS = {"generate_storyboard_video_groups"}
OPENAI_COMPATIBLE_VISION_STEPS = {"analyze_reference_mechanisms"}
OPENAI_COMPATIBLE_TRANSCRIPTION_STEPS = {"transcribe_reference_audio"}
FFMPEG_CONCAT_STEPS = {"assemble_final_video"}


def _normalize_provider_config(provider_key: str, provider_config: dict[str, Any]) -> dict[str, Any]:
    """校验非敏感 JSON 配置，防止 API Key 被误写进数据库。"""

    normalized = normalize_provider_config(adapter_key=provider_key.strip(), provider_config=provider_config)
    secret_env_name = normalized.get("secret_env_name")
    if secret_env_name and (
        not isinstance(secret_env_name, str) or not _ENV_NAME_PATTERN.fullmatch(secret_env_name)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="secret_env_name 必须是大写环境变量名称，例如 YUNWU_REASONING_API_KEY",
        )
    return normalized


def is_adapter_available(step_key: str, provider_key: str, model_key: str) -> bool:
    """判断当前代码包是否已经实现该步骤的供应商适配器。"""

    default = DEFAULT_PROFILES.get(step_key)
    uses_mock_adapter = bool(
        default
        and provider_key == default["provider_key"]
        and model_key == default["model_key"]
    )
    return uses_mock_adapter or (
        provider_key == "openai_compatible" and step_key in OPENAI_COMPATIBLE_TEXT_STEPS
    ) or (
        provider_key == "openai_compatible_image" and step_key in OPENAI_COMPATIBLE_IMAGE_STEPS
    ) or (
        provider_key == "openai_compatible_vision" and step_key in OPENAI_COMPATIBLE_VISION_STEPS
    ) or (
        provider_key == "openai_compatible_transcription" and step_key in OPENAI_COMPATIBLE_TRANSCRIPTION_STEPS
    ) or (
        provider_key == "configurable_async_video" and step_key in CONFIGURABLE_ASYNC_VIDEO_STEPS
    ) or (
        provider_key == "volcengine_ark_video" and step_key in VOLCENGINE_ARK_VIDEO_STEPS
    ) or (
        provider_key == "ffmpeg_concat" and step_key in FFMPEG_CONCAT_STEPS
    )


def _validate_activation_config(
    step_key: str,
    provider_key: str,
    provider_config: dict[str, Any],
) -> None:
    """验证可执行适配器所需的非敏感参数，密钥值仍只在运行环境读取。"""

    if provider_key not in {
        "openai_compatible",
        "openai_compatible_image",
        "openai_compatible_vision",
        "openai_compatible_transcription",
        "configurable_async_video",
        "volcengine_ark_video",
        "ffmpeg_concat",
    }:
        return
    if provider_key == "configurable_async_video":
        _validate_async_video_config(step_key, provider_config)
        return
    if provider_key == "volcengine_ark_video":
        _validate_volcengine_ark_video_config(step_key, provider_config)
        return
    if provider_key == "ffmpeg_concat":
        _validate_ffmpeg_concat_config(step_key, provider_config)
        return
    supported_steps = (
        OPENAI_COMPATIBLE_TEXT_STEPS
        if provider_key == "openai_compatible"
        else OPENAI_COMPATIBLE_IMAGE_STEPS
        if provider_key == "openai_compatible_image"
        else OPENAI_COMPATIBLE_VISION_STEPS
        if provider_key == "openai_compatible_vision"
        else OPENAI_COMPATIBLE_TRANSCRIPTION_STEPS
    )
    if step_key not in supported_steps:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该步骤暂未接入此 OpenAI 兼容适配器")
    api_base_url = provider_config.get("api_base_url")
    secret_env_name = provider_config.get("secret_env_name")
    if not isinstance(api_base_url, str) or not api_base_url.startswith("https://"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="OpenAI 兼容配置需要 https:// api_base_url")
    if not isinstance(secret_env_name, str) or not _ENV_NAME_PATTERN.fullmatch(secret_env_name):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="OpenAI 兼容配置需要 secret_env_name 环境变量名称")
    if provider_key == "openai_compatible_vision":
        _validate_vision_config(provider_config)
    if provider_key == "openai_compatible_transcription":
        _validate_transcription_config(provider_config)


def _validate_vision_config(provider_config: dict[str, Any]) -> None:
    """验证视觉分析的抽帧规模与扩展请求参数，控制成本和单次请求体积。"""

    frame_count = provider_config.get("frame_sample_count", 6)
    timeout_seconds = provider_config.get("frame_extraction_timeout_seconds", 120)
    max_frame_bytes = provider_config.get("frame_max_bytes", 2 * 1024 * 1024)
    if not isinstance(frame_count, int) or not 1 <= frame_count <= 12:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="frame_sample_count 必须是 1 至 12 的整数",
        )
    if not isinstance(timeout_seconds, (int, float)) or not 5 <= float(timeout_seconds) <= 300:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="frame_extraction_timeout_seconds 必须在 5 至 300 秒之间",
        )
    if not isinstance(max_frame_bytes, int) or not 64 * 1024 <= max_frame_bytes <= 8 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="frame_max_bytes 必须在 64KB 至 8MB 之间",
        )
    options = provider_config.get("vision_request_options", {})
    if not isinstance(options, dict):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="vision_request_options 必须是 JSON 对象")


def _validate_transcription_config(provider_config: dict[str, Any]) -> None:
    """验证语音转写的提取预算和扩展请求参数，限制成本与上传体积。"""

    max_duration_seconds = provider_config.get("audio_max_duration_seconds", 180)
    extraction_timeout = provider_config.get("audio_extraction_timeout_seconds", 120)
    max_audio_bytes = provider_config.get("audio_max_bytes", 8 * 1024 * 1024)
    if not isinstance(max_duration_seconds, int) or not 5 <= max_duration_seconds <= 600:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="audio_max_duration_seconds 必须是 5 至 600 的整数",
        )
    if not isinstance(extraction_timeout, (int, float)) or not 5 <= float(extraction_timeout) <= 300:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="audio_extraction_timeout_seconds 必须在 5 至 300 秒之间",
        )
    if not isinstance(max_audio_bytes, int) or not 64 * 1024 <= max_audio_bytes <= 50 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="audio_max_bytes 必须在 64KB 至 50MB 之间",
        )
    options = provider_config.get("transcription_request_options", {})
    if not isinstance(options, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="transcription_request_options 必须是 JSON 对象",
        )
    forbidden = {"file", "model", "response_format"}
    if forbidden.intersection(options):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="transcription_request_options 不能覆盖 file、model 或 response_format",
        )


def _validate_ffmpeg_concat_config(step_key: str, provider_config: dict[str, Any]) -> None:
    """验证完整成片导出的网络/磁盘预算，避免一条任务耗尽 Worker 资源。"""

    if step_key not in FFMPEG_CONCAT_STEPS:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该步骤暂未接入 FFmpeg 合成适配器")
    timeout_seconds = provider_config.get("download_timeout_seconds", 120)
    max_clip_bytes = provider_config.get("max_clip_bytes", 500 * 1024 * 1024)
    max_output_bytes = provider_config.get("max_output_bytes", 2 * 1024 * 1024 * 1024)
    render_timeout_seconds = provider_config.get("render_timeout_seconds", 1800)
    if not isinstance(timeout_seconds, (int, float)) or not 5 <= float(timeout_seconds) <= 600:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="download_timeout_seconds 必须在 5 至 600 秒之间")
    if not isinstance(max_clip_bytes, int) or not 1 * 1024 * 1024 <= max_clip_bytes <= 2 * 1024 * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="max_clip_bytes 必须在 1MB 至 2GB 之间")
    if not isinstance(max_output_bytes, int) or not 1 * 1024 * 1024 <= max_output_bytes <= 10 * 1024 * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="max_output_bytes 必须在 1MB 至 10GB 之间")
    if not isinstance(render_timeout_seconds, (int, float)) or not 30 <= float(render_timeout_seconds) <= 7200:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="render_timeout_seconds 必须在 30 至 7200 秒之间")

def _validate_async_video_config(step_key: str, provider_config: dict[str, Any]) -> None:
    """验证通用异步图生视频适配器的最低配置。

    该门禁故意要求提交与查询地址同时存在，避免把“提交成功但永远不查询”的
    半成品模型配置启用到生产步骤。供应商状态字段仍以配置保存，可随模型替换。
    """

    if step_key not in CONFIGURABLE_ASYNC_VIDEO_STEPS:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该步骤暂未接入异步视频适配器")
    api_base_url = provider_config.get("api_base_url")
    secret_env_name = provider_config.get("secret_env_name")
    submit_path = provider_config.get("submit_path")
    query_path_template = provider_config.get("query_path_template")
    if not isinstance(api_base_url, str) or not api_base_url.startswith("https://"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="异步视频配置需要 https:// api_base_url")
    if not isinstance(secret_env_name, str) or not _ENV_NAME_PATTERN.fullmatch(secret_env_name):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="异步视频配置需要 secret_env_name 环境变量名称")
    if not isinstance(submit_path, str) or not submit_path.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="异步视频配置需要 submit_path")
    if not isinstance(query_path_template, str) or "{task_id}" not in query_path_template:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="异步视频配置需要包含 {task_id} 的 query_path_template",
        )
    image_mode = provider_config.get("image_input_mode", "top_level_url")
    if image_mode not in {"top_level_url", "luma_keyframe"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="image_input_mode 仅支持 top_level_url 或 luma_keyframe",
        )
    request_options = provider_config.get("video_request_options", {})
    if not isinstance(request_options, dict):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="video_request_options 必须是 JSON 对象")
    for config_key in (
        "prompt_field",
        "image_field",
        "keyframes_field",
        "keyframe_name",
        "model_field",
        "end_image_field",
    ):
        value = provider_config.get(config_key)
        if value is not None and (not isinstance(value, str) or not _FIELD_NAME_PATTERN.fullmatch(value)):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{config_key} 必须仅包含字母、数字或下划线",
            )
    for config_key in ("task_id_path", "state_path", "error_message_path"):
        value = provider_config.get(config_key)
        if value is not None and (not isinstance(value, str) or not _JSON_PATH_PATTERN.fullmatch(value)):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"{config_key} 必须是简单 JSON 路径")
    video_url_paths = provider_config.get("video_url_paths")
    if video_url_paths is not None and (
        not isinstance(video_url_paths, list)
        or not video_url_paths
        or not all(isinstance(item, str) and _JSON_PATH_PATTERN.fullmatch(item) for item in video_url_paths)
    ):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="video_url_paths 必须是简单 JSON 路径数组")
    for config_key in ("success_states", "failure_states"):
        value = provider_config.get(config_key)
        if value is not None and (
            not isinstance(value, list)
            or not value
            or not all(isinstance(item, str) and item.strip() for item in value)
        ):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"{config_key} 必须是非空字符串数组")


def _validate_volcengine_ark_video_config(step_key: str, provider_config: dict[str, Any]) -> None:
    """校验火山方舟原生视频配置，仅保留制作人员真正需要选择的参数。"""

    if step_key not in VOLCENGINE_ARK_VIDEO_STEPS:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该步骤暂未接入火山方舟视频适配器")
    secret_env_name = provider_config.get("secret_env_name", "ARK_API_KEY")
    if not isinstance(secret_env_name, str) or not _ENV_NAME_PATTERN.fullmatch(secret_env_name):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="火山方舟视频配置需要 ARK_API_KEY 环境变量名称")
    ratio = provider_config.get("ratio", "9:16")
    if not isinstance(ratio, str) or ratio not in {"16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="视频画幅必须是平台支持的比例，例如 9:16")
    duration = provider_config.get("duration", 5)
    if not isinstance(duration, int) or not 2 <= duration <= 12:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="视频时长必须是 2 至 12 秒的整数")
    resolution = provider_config.get("resolution")
    if resolution is not None and (not isinstance(resolution, str) or not resolution.strip()):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="视频分辨率必须是非空文本或留空使用模型默认值")
    for option in ("generate_audio", "watermark", "return_last_frame", "use_last_frame"):
        value = provider_config.get(option)
        if value is not None and not isinstance(value, bool):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"{option} 必须是是或否")
    seed = provider_config.get("seed")
    if seed is not None and (not isinstance(seed, int) or seed < 0):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="seed 必须是非负整数或留空")


def ensure_default_profiles(db: Session) -> None:
    """在空数据库首次启动时写入各步骤可直接联调的模拟配置。"""

    for step_key, defaults in DEFAULT_PROFILES.items():
        exists = db.scalar(select(ModelProfile.id).where(ModelProfile.step_key == step_key))
        if exists:
            continue
        db.add(
            ModelProfile(
                step_key=step_key,
                provider_key=defaults["provider_key"],
                model_key=defaults["model_key"],
                version=1,
                provider_config=deepcopy(defaults["provider_config"]),
                is_active=True,
            )
        )
    db.commit()


def list_model_profiles(db: Session, step_key: Optional[str] = None) -> list[ModelProfile]:
    """读取配置历史；可按步骤筛选，便于配置中心按工作流分组展示。"""

    statement = select(ModelProfile).order_by(ModelProfile.step_key, ModelProfile.version.desc())
    if step_key:
        if step_key not in DEFAULT_PROFILES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="未知的工作流步骤")
        statement = statement.where(ModelProfile.step_key == step_key)
    return list(db.scalars(statement).all())


def get_active_profile_snapshot(db: Session, step_key: str) -> dict[str, Any]:
    """冻结当前启用的模型配置，供一次工作流运行持久化。"""

    profile = db.scalars(
        select(ModelProfile)
        .where(ModelProfile.step_key == step_key, ModelProfile.is_active.is_(True))
        .order_by(ModelProfile.version.desc())
    ).first()
    if profile is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"步骤 {step_key} 没有启用模型配置")
    return {
        "profile_id": profile.id,
        "provider_key": profile.provider_key,
        "model_key": profile.model_key,
        "version": profile.version,
        "provider_config": redact_provider_config(profile.provider_config),
    }


def create_model_profile(
    db: Session,
    *,
    step_key: str,
    provider_key: str,
    model_key: str,
    provider_config: dict[str, Any],
    activate: bool,
) -> ModelProfile:
    """追加一版配置；若要求启用，先验证适配器已存在再原子切换。"""

    if step_key not in DEFAULT_PROFILES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="未知的工作流步骤")
    normalized_config = _normalize_provider_config(provider_key, provider_config)
    if activate and not is_adapter_available(step_key, provider_key, model_key):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该供应商适配器尚未安装，只能先保存为未启用配置",
        )
    if activate:
        _validate_activation_config(step_key, provider_key, normalized_config)
    latest_version = db.scalar(
        select(func.max(ModelProfile.version)).where(ModelProfile.step_key == step_key)
    ) or 0
    if activate:
        for profile in db.scalars(
            select(ModelProfile).where(
                ModelProfile.step_key == step_key,
                ModelProfile.is_active.is_(True),
            )
        ):
            profile.is_active = False
    profile = ModelProfile(
        step_key=step_key,
        provider_key=provider_key.strip(),
        model_key=model_key.strip(),
        version=latest_version + 1,
        provider_config=normalized_config,
        is_active=activate,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def activate_model_profile(db: Session, profile_id: str) -> ModelProfile:
    """只在适配器存在时切换活动版本，避免生产任务落到无法执行的配置。"""

    profile = db.get(ModelProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模型配置不存在")
    if not is_adapter_available(profile.step_key, profile.provider_key, profile.model_key):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该供应商适配器尚未安装，暂不能启用")
    _validate_activation_config(profile.step_key, profile.provider_key, profile.provider_config)
    for item in db.scalars(
        select(ModelProfile).where(
            ModelProfile.step_key == profile.step_key,
            ModelProfile.is_active.is_(True),
        )
    ):
        item.is_active = False
    profile.is_active = True
    db.commit()
    db.refresh(profile)
    return profile


def preflight_model_profile(db: Session, profile_id: str) -> list[dict[str, str]]:
    """对一版模型配置执行无扣费预检，并返回不含密钥的检查结果。

    预检不会提交图片、视频或文本生成请求：异步视频提交在网络超时时无法判断供应商
    是否已接收，贸然测试可能产生费用。OpenAI 兼容配置仅在密钥存在时尝试只读的
    ``/models`` 目录接口；若中转站不实现该接口，会提示人工用一个测试项目做小样本
    验收，而不是将其误判为模型不可用。
    """

    profile = db.get(ModelProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模型配置不存在")

    checks: list[dict[str, str]] = []
    adapter_available = is_adapter_available(profile.step_key, profile.provider_key, profile.model_key)
    checks.append(
        {
            "key": "adapter",
            "status": "passed" if adapter_available else "failed",
            "message": "当前代码包已接入该步骤适配器" if adapter_available else "当前代码包尚未接入该步骤适配器",
        }
    )
    if not adapter_available:
        return checks

    try:
        _validate_activation_config(profile.step_key, profile.provider_key, profile.provider_config)
        checks.append({"key": "config", "status": "passed", "message": "必要参数与安全约束校验通过"})
    except HTTPException as exc:
        checks.append({"key": "config", "status": "failed", "message": str(exc.detail)})
        return checks

    if profile.provider_key == "mock_provider":
        checks.append({"key": "runtime", "status": "passed", "message": "本地模拟适配器可直接用于联调，不访问第三方"})
        return checks

    if profile.provider_key == "ffmpeg_concat":
        checks.append(_preflight_ffmpeg())
        return checks

    if profile.provider_key == "configurable_async_video":
        checks.append(_preflight_secret(profile.provider_config))
        checks.append(
            {
                "key": "network",
                "status": "warning",
                "message": "通用异步视频协议没有统一的无扣费探针；请先用测试项目生成 1 组镜头验证提交与轮询映射。",
            }
        )
        return checks

    if profile.provider_key == "volcengine_ark_video":
        checks.append(_preflight_secret(profile.provider_config))
        checks.append(
            {
                "key": "protocol",
                "status": "passed",
                "message": "已使用火山方舟原生视频协议；创建、查询、首帧字段和状态映射由系统固定管理",
            }
        )
        return checks

    # 已由激活校验确认：到达此处的远程模型均为 OpenAI 兼容文本/图片/视觉/转写协议。
    secret_check = _preflight_secret(profile.provider_config)
    checks.append(secret_check)
    if secret_check["status"] != "passed":
        return checks
    checks.append(
        _preflight_openai_catalog(
            api_base_url=str(profile.provider_config["api_base_url"]),
            api_key=os.environ[str(profile.provider_config["secret_env_name"])],
            model_key=profile.model_key,
            timeout_seconds=_preflight_timeout_seconds(profile.provider_config),
        )
    )
    return checks


def _preflight_secret(provider_config: dict[str, Any]) -> dict[str, str]:
    """仅确认密钥环境变量是否已注入，绝不读取或回显其实际内容。"""

    secret_env_name = provider_config.get("secret_env_name")
    if not isinstance(secret_env_name, str) or not _ENV_NAME_PATTERN.fullmatch(secret_env_name):
        return {"key": "secret", "status": "failed", "message": "缺少有效的密钥环境变量名称"}
    if not os.getenv(secret_env_name):
        return {
            "key": "secret",
            "status": "failed",
            "message": f"服务器尚未注入环境变量 {secret_env_name}；请写入后端 .env 或部署 Secret 后重启 API/Worker",
        }
    return {"key": "secret", "status": "passed", "message": f"服务器已注入 {secret_env_name}"}


def _preflight_ffmpeg() -> dict[str, str]:
    """检查 API/Worker 共用镜像是否能调用 FFmpeg，而不创建任何媒体文件。"""

    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"key": "runtime", "status": "failed", "message": "当前服务环境找不到可用 FFmpeg"}
    if result.returncode != 0:
        return {"key": "runtime", "status": "failed", "message": "FFmpeg 命令无法正常执行"}
    return {"key": "runtime", "status": "passed", "message": "FFmpeg 已安装，可用于完整成片合成"}


def _preflight_openai_catalog(
    *,
    api_base_url: str,
    api_key: str,
    model_key: str,
    timeout_seconds: float,
) -> dict[str, str]:
    """读取 OpenAI 兼容 ``/models``，只验证可达性和账户可见模型，不生成内容。"""

    url = f"{api_base_url.rstrip('/')}/models"
    request = Request(url, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw_body = response.read(2 * 1024 * 1024)
    except HTTPError as exc:
        if exc.code in (404, 405, 501):
            return {
                "key": "network",
                "status": "warning",
                "message": "中转站未提供 /models 目录接口；静态配置已通过，请用测试项目做一次小样本验证。",
            }
        if exc.code in (401, 403):
            return {"key": "network", "status": "failed", "message": "中转站拒绝密钥，请检查密钥、账户权限或 API 地址"}
        return {"key": "network", "status": "failed", "message": f"中转站目录接口返回 HTTP {exc.code}"}
    except (URLError, TimeoutError, OSError):
        return {"key": "network", "status": "failed", "message": "当前 API 服务无法连接中转站，请检查地址、网络和出口策略"}

    try:
        payload = json.loads(raw_body.decode("utf-8"))
        data = payload.get("data") if isinstance(payload, dict) else None
        available_models = {item.get("id") for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)}
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return {
            "key": "network",
            "status": "warning",
            "message": "中转站可连接，但 /models 响应格式无法识别；请用测试项目做一次小样本验证。",
        }
    if available_models and model_key not in available_models:
        return {"key": "network", "status": "failed", "message": "中转站可连接，但当前账户的 /models 列表中没有该模型标识"}
    return {"key": "network", "status": "passed", "message": "中转站可连接，当前账户可查询到该模型"}


def _preflight_timeout_seconds(provider_config: dict[str, Any]) -> float:
    """预检永远使用短超时，不继承可能长达半小时的生成任务超时。"""

    configured = provider_config.get("timeout_seconds", 10)
    if not isinstance(configured, (int, float)):
        return 10
    return min(max(float(configured), 1), 15)


def create_model_evaluation(
    db: Session,
    *,
    profile_id: str,
    scenario: str,
    sample_count: int,
    success_count: int,
    total_cost_yuan: float,
    average_latency_seconds: float,
    quality_score: int,
    notes: Optional[str],
) -> ModelEvaluation:
    """保存一次人工验收统计，供同一步骤的模型版本比较。

    记录汇总数值而不是视频、提示词或模型原始输出，避免评测功能绕过业务素材授权
    边界，也降低数据库和备份中的敏感内容量。
    """

    if db.get(ModelProfile, profile_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模型配置不存在")
    if success_count > sample_count:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="成功样本数不能大于总样本数")
    evaluation = ModelEvaluation(
        model_profile_id=profile_id,
        scenario=scenario.strip(),
        sample_count=sample_count,
        success_count=success_count,
        total_cost_yuan=total_cost_yuan,
        average_latency_seconds=average_latency_seconds,
        quality_score=quality_score,
        notes=notes.strip() if notes else None,
    )
    db.add(evaluation)
    db.commit()
    db.refresh(evaluation)
    return evaluation


def list_model_evaluations(db: Session, profile_id: str) -> list[ModelEvaluation]:
    """按最新记录优先展示同一模型配置的全部人工验收统计。"""

    if db.get(ModelProfile, profile_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模型配置不存在")
    return list(
        db.scalars(
            select(ModelEvaluation)
            .where(ModelEvaluation.model_profile_id == profile_id)
            .order_by(ModelEvaluation.created_at.desc())
        ).all()
    )


def list_model_evaluation_comparisons(
    db: Session,
    step_key: Optional[str],
) -> list[tuple[ModelEvaluation, ModelProfile]]:
    """读取带配置版本信息的评测行，供用户在同一步骤内做横向对比。

    相同场景名称才具有直接可比性，因此不在服务端混合计算跨场景平均值，避免把“5
    镜视频”与“10 条文本选题”误合并成看似精确、实际无意义的分数。
    """

    if step_key and step_key not in DEFAULT_PROFILES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="未知的工作流步骤")
    statement = (
        select(ModelEvaluation, ModelProfile)
        .join(ModelProfile, ModelEvaluation.model_profile_id == ModelProfile.id)
        .order_by(ModelProfile.step_key, ModelEvaluation.created_at.desc())
    )
    if step_key:
        statement = statement.where(ModelProfile.step_key == step_key)
    return list(db.execute(statement).all())
