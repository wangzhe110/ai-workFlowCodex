"""LemonFlow V1 的模型能力 Adapter 边界。

V1 生产服务只知道 ``VIDEO_ANALYSIS``、``STORY_GENERATE``、``IMAGE_GENERATE``
和 ``VIDEO_GENERATE`` 等能力，不直接知道任何供应商或模型名称。本模块根据一次
任务已冻结的 ``ModelProfile`` 快照选择协议 Adapter，并将各家请求/响应差异收敛
在这里。

这里不会读取数据库，也不会保存 API Key。Key 只由已有供应商 Adapter 在 Worker
运行时通过 ``secret_env_name`` 从服务器环境变量读取。
"""

from __future__ import annotations

import time
from typing import Any

from app.models import MediaAsset
from app.services.analysis_provider import (
    ConfigurableAsyncVideoProvider,
    FalQueueImageProvider,
    ImageGenerationProvider,
    ImageTaskResult,
    OpenAICompatibleImageProvider,
    OpenAICompatibleJsonProvider,
    OpenAICompatibleVisionAnalysisProvider,
    VideoAnalysisInput,
    VideoGenerationInput,
    VideoGenerationProvider,
    VideoTaskResult,
    VolcengineArkImageProvider,
    VolcengineArkVideoProvider,
)
from app.services.storage import LocalImageReference, generated_image_delivery, local_asset_storage
from app.services.video_frame_service import extract_sampled_video_frames


TEXT_TASK_TYPES = {
    "STORY_GENERATE",
    "CHARACTER_DESIGN",
    "SCENE_DESIGN",
    "DIRECTOR_PLAN",
}
IMAGE_TASK_TYPE = "IMAGE_GENERATE"
VIDEO_TASK_TYPE = "VIDEO_GENERATE"
VIDEO_ANALYSIS_TASK_TYPE = "VIDEO_ANALYSIS"


def adapter_key(snapshot: dict[str, Any]) -> str:
    """读取冻结的 Adapter 键，并拒绝没有明确 Adapter 的历史配置。"""

    key = snapshot.get("adapter_key") or snapshot.get("provider_key")
    if not isinstance(key, str) or not key.strip():
        raise RuntimeError("模型配置缺少 adapter_key，无法安全执行")
    return key.strip()


def is_mock_adapter(snapshot: dict[str, Any]) -> bool:
    """显式区分本地模拟 Adapter，避免它被误认为真实模型。"""

    return adapter_key(snapshot) == "mock_v1"


def assert_supported(snapshot: dict[str, Any], task_type: str) -> None:
    """在发起可能收费的调用前确认该 Adapter 支持当前能力槽位。"""

    key = adapter_key(snapshot)
    if key == "mock_v1":
        return
    if task_type == VIDEO_ANALYSIS_TASK_TYPE and key == "openai_compatible_vision":
        return
    if task_type in TEXT_TASK_TYPES and key == "openai_compatible":
        return
    if task_type == IMAGE_TASK_TYPE and key in {"openai_compatible_image", "fal_queue_image", "volcengine_ark_image"}:
        return
    if task_type == VIDEO_TASK_TYPE and key in {"volcengine_ark_video", "configurable_async_video"}:
        return
    if task_type == "FINAL_COMPOSE" and key == "ffmpeg_concat":
        return
    raise RuntimeError(f"Adapter {key} 尚未接入 V1 的 {task_type} 能力")


def _frame_extraction_settings(snapshot: dict[str, Any]) -> tuple[int, float, int]:
    """在 V1 Adapter 边界重复执行抽帧预算门禁，防止异常配置放大费用。"""

    config = snapshot.get("provider_config") or {}
    frame_count = config.get("frame_sample_count", 6)
    timeout_seconds = config.get("frame_extraction_timeout_seconds", 120)
    max_frame_bytes = config.get("frame_max_bytes", 2 * 1024 * 1024)
    if not isinstance(frame_count, int) or not 1 <= frame_count <= 12:
        raise RuntimeError("frame_sample_count 必须是 1 至 12 的整数")
    if not isinstance(timeout_seconds, (int, float)) or not 5 <= float(timeout_seconds) <= 300:
        raise RuntimeError("frame_extraction_timeout_seconds 必须在 5 至 300 秒之间")
    if not isinstance(max_frame_bytes, int) or not 64 * 1024 <= max_frame_bytes <= 8 * 1024 * 1024:
        raise RuntimeError("frame_max_bytes 必须在 64KB 至 8MB 之间")
    return frame_count, float(timeout_seconds), max_frame_bytes


def analyze_reference_video(snapshot: dict[str, Any], source: MediaAsset) -> tuple[dict[str, Any], int]:
    """通过视觉 Adapter 分析参考视频，并返回 V1 结果和实际抽帧数。

    视觉模型只接收临时抽样帧。原视频、帧数据和可能的受版权保护台词均不会持久化
    到 V1 的模型调用审计或前端结果中。
    """

    assert_supported(snapshot, VIDEO_ANALYSIS_TASK_TYPE)
    if is_mock_adapter(snapshot):
        raise RuntimeError("本地模拟分析由 V1 生产服务处理，不应经过真实视觉 Adapter")
    frame_count, timeout_seconds, max_frame_bytes = _frame_extraction_settings(snapshot)
    frames = extract_sampled_video_frames(
        source.storage_key,
        frame_count=frame_count,
        timeout_seconds=timeout_seconds,
        max_frame_bytes=max_frame_bytes,
    )
    result = OpenAICompatibleVisionAnalysisProvider(snapshot).analyze(
        VideoAnalysisInput(
            asset_id=source.id,
            filename=source.original_filename,
            content_type=source.content_type,
            sampled_frames=frames,
        )
    )
    if not isinstance(result, dict):
        raise RuntimeError("视频分析 Adapter 返回的结果不是 JSON 对象")
    return result, len(frames)


def generate_structured_text(
    snapshot: dict[str, Any],
    *,
    task_type: str,
    system_instruction: str,
    user_payload: dict[str, Any],
    output_contract: str,
) -> dict[str, Any]:
    """使用 OpenAI 兼容文本协议获取一个已要求 JSON 的结构化结果。"""

    assert_supported(snapshot, task_type)
    if is_mock_adapter(snapshot):
        raise RuntimeError("本地模拟文本由 V1 生产服务处理，不应经过真实文本 Adapter")
    result = OpenAICompatibleJsonProvider(snapshot).generate_json(
        system_instruction=system_instruction,
        user_payload=user_payload,
        output_contract=output_contract,
    )
    if not isinstance(result, dict):
        raise RuntimeError("文本模型必须返回 JSON 对象")
    return result


def start_image_generation(
    snapshot: dict[str, Any],
    *,
    prompt: str,
    reference_image_urls: list[str] | None = None,
    reference_images: list[LocalImageReference] | None = None,
    existing_provider_task_id: str | None = None,
) -> tuple[ImageGenerationProvider | None, ImageTaskResult]:
    """提交或恢复一张图片任务。

    Fal 队列在提交后会立即返回 ``request_id``。调用者必须先将它写入
    ``ModelInvocation`` 并提交数据库，再调用 :func:`wait_for_image_result`。Worker
    恢复时传入相同任务号，只会轮询，绝不会再次向可能收费的供应商提交图片任务。
    OpenAI 兼容图片接口仍是同步协议，因此直接返回成功态；这不是回退，也不会把
    Fal 配置偷偷改走旧协议。
    """

    assert_supported(snapshot, IMAGE_TASK_TYPE)
    if is_mock_adapter(snapshot):
        raise RuntimeError("本地模拟图片由 V1 生产服务处理，不应经过真实图片 Adapter")
    key = adapter_key(snapshot)
    if key == "openai_compatible_image":
        if existing_provider_task_id:
            raise RuntimeError("同步图片模型不支持恢复供应商任务")
        return None, ImageTaskResult(
            provider_task_id=None,
            status="SUCCEEDED",
            image_url=OpenAICompatibleImageProvider(snapshot).generate(
                prompt,
                reference_image_urls=reference_image_urls or [],
            ),
        )
    if key == "fal_queue_image":
        provider = FalQueueImageProvider(snapshot)
        if existing_provider_task_id:
            return provider, provider.poll(existing_provider_task_id)
        return provider, provider.submit(prompt, reference_image_urls=reference_image_urls or [])
    if key == "volcengine_ark_image":
        if existing_provider_task_id:
            raise RuntimeError("方舟同步图片模型不支持恢复供应商任务")
        if reference_image_urls and reference_images:
            raise RuntimeError("方舟图片不能同时接收 URL 参考图和本地参考图")
        # 同步官方图片协议没有 provider_task_id；POST 只在这里执行一次，异常由
        # 上层记录后交给人工重新生成，绝不静默重试或改走其他图片模型。
        return None, VolcengineArkImageProvider(snapshot).generate(
            prompt,
            reference_image_urls=reference_image_urls or [],
            reference_images=reference_images or [],
        )
    raise RuntimeError(f"Adapter {key} 不能生成图片任务")


def wait_for_image_result(
    provider: ImageGenerationProvider | None,
    snapshot: dict[str, Any],
    first_result: ImageTaskResult,
) -> ImageTaskResult:
    """轮询异步图片任务直至终态，超时不会再次提交供应商请求。"""

    if first_result.status != "PENDING":
        return first_result
    if provider is None:
        raise RuntimeError("图片 Adapter 返回等待状态但没有轮询实现")
    task_id = first_result.provider_task_id
    if not task_id:
        raise RuntimeError("图片 Adapter 返回等待状态但没有供应商任务号")
    config = snapshot.get("provider_config") or {}
    try:
        interval = float(config.get("poll_interval_seconds", 3))
        maximum_wait = float(config.get("max_poll_seconds", 600))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("poll_interval_seconds 和 max_poll_seconds 必须为数字") from exc
    interval = min(max(interval, 1), 60)
    maximum_wait = min(max(maximum_wait, 10), 1800)
    deadline = time.monotonic() + maximum_wait
    result = first_result
    while result.status == "PENDING":
        if time.monotonic() >= deadline:
            return ImageTaskResult(
                provider_task_id=task_id,
                status="FAILED",
                error_message="等待图片供应商结果超时；请检查任务号和模型配置后重新生成",
            )
        time.sleep(interval)
        result = provider.poll(task_id)
        task_id = result.provider_task_id or task_id
    return result


def generate_image(
    snapshot: dict[str, Any],
    *,
    prompt: str,
    reference_image_urls: list[str] | None = None,
) -> str:
    """兼容旧调用方的一次性图片入口。

    V1 和 Commerce 的新 Worker 路径应使用 ``start_image_generation``，以便先持久化
    Fal ``request_id``。该函数仅保留给历史调用方，不会改变已接入 Adapter 的行为。
    """

    provider, first_result = start_image_generation(
        snapshot,
        prompt=prompt,
        reference_image_urls=reference_image_urls,
    )
    result = wait_for_image_result(provider, snapshot, first_result)
    if result.status != "SUCCEEDED" or not result.image_url:
        raise RuntimeError(result.error_message or "图片供应商任务失败")
    return result.image_url


def persist_v1_image(
    *,
    project_id: str,
    asset_kind: str,
    asset_id: str,
    version: int,
    source_url: str,
) -> str:
    """把图片转为稳定地址，复用统一存储 Adapter 而不让 V1 知道 S3 细节。

    旧图片存储接口按“分镜包/镜头”组织对象。本函数给 V1 资产生成一个不可冲突的
    虚拟命名空间，保留其存储实现和安全下载限制；这不改变 V1 的业务归属关系。
    """

    namespace = f"v1-{asset_kind.lower()}-{asset_id}"
    return generated_image_delivery.persist(
        project_id=project_id,
        storyboard_package_id=namespace,
        shot_number=1,
        version=version,
        source_url=source_url,
    )


def persist_v1_image_bytes(
    *,
    project_id: str,
    asset_kind: str,
    asset_id: str,
    version: int,
    content: bytes,
    content_type: str,
) -> str:
    """保存官方方舟已下载的图片字节，不持久化供应商临时 URL。"""

    return local_asset_storage.save_generated_image_bytes(
        project_id=project_id,
        asset_kind=asset_kind,
        asset_id=asset_id,
        version=version,
        content=content,
        content_type=content_type,
    )


def video_provider(snapshot: dict[str, Any]) -> VideoGenerationProvider:
    """按冻结配置返回异步视频协议实现，不依赖具体视频模型名称。"""

    assert_supported(snapshot, VIDEO_TASK_TYPE)
    key = adapter_key(snapshot)
    if key == "volcengine_ark_video":
        return VolcengineArkVideoProvider(snapshot)
    if key == "configurable_async_video":
        return ConfigurableAsyncVideoProvider(snapshot)
    raise RuntimeError(f"Adapter {key} 不能创建视频生成任务")


def wait_for_video_result(
    provider: VideoGenerationProvider,
    snapshot: dict[str, Any],
    first_result: VideoTaskResult,
) -> VideoTaskResult:
    """轮询异步视频任务到终态，受间隔与总等待时间上限保护。

    V1 与旧视频模块采用同样的 Worker 内轮询原则。超过最大等待时间会明确失败，
    而不是把一个已收费、无人跟踪的任务伪装成已完成。
    """

    if first_result.status != "PENDING":
        return first_result
    task_id = first_result.provider_task_id
    if not task_id:
        raise RuntimeError("视频 Adapter 返回等待状态但没有供应商任务号")
    config = snapshot.get("provider_config") or {}
    try:
        interval = float(config.get("poll_interval_seconds", 4))
        maximum_wait = float(config.get("max_poll_seconds", 900))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("poll_interval_seconds 和 max_poll_seconds 必须为数字") from exc
    interval = min(max(interval, 1), 60)
    maximum_wait = min(max(maximum_wait, 10), 1800)
    deadline = time.monotonic() + maximum_wait
    result = first_result
    while result.status == "PENDING":
        if time.monotonic() >= deadline:
            return VideoTaskResult(
                provider_task_id=task_id,
                status="FAILED",
                error_message="等待视频供应商结果超时；请检查任务号和模型配置后重新生成",
            )
        time.sleep(interval)
        result = provider.poll(task_id)
        task_id = result.provider_task_id or task_id
    return result


def create_video_request(
    *,
    project_id: str,
    shot_number: int,
    prompt: str,
    image_urls: list[str],
    reference_images: list[LocalImageReference] | None = None,
) -> VideoGenerationInput:
    """建立供应商无关的视频输入对象，视频 Adapter 再转换为其专属协议。"""

    if not image_urls and not reference_images:
        raise RuntimeError("视频生成缺少锁定关键帧图片")
    return VideoGenerationInput(
        project_id=project_id,
        group_number=shot_number,
        start_shot_number=shot_number,
        end_shot_number=shot_number,
        prompt=prompt,
        image_urls=image_urls,
        reference_images=reference_images or [],
    )
