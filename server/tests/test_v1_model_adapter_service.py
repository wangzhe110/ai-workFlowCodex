"""V1 模型 Adapter 的能力隔离与异步视频轮询测试。"""

import pytest

from app.services.analysis_provider import ProviderPollNetworkError, VideoTaskResult
from app.services.storage import LocalImageReference
from app.services.v1_model_adapter_service import assert_supported, create_video_request, wait_for_video_result


def test_v1_adapter_support_is_based_on_capability_not_model_name() -> None:
    """任意模型名只要协议匹配即可接入；不支持的能力会在扣费前被拒绝。"""

    assert_supported(
        {"adapter_key": "openai_compatible", "model_key": "any-future-text-model"},
        "STORY_GENERATE",
    )
    assert_supported(
        {"adapter_key": "volcengine_ark_video", "model_key": "any-future-video-model"},
        "VIDEO_GENERATE",
    )
    with pytest.raises(RuntimeError, match="尚未接入"):
        assert_supported(
            {"adapter_key": "openai_compatible_image", "model_key": "image-model"},
            "VIDEO_GENERATE",
        )


def test_v1_video_waiter_polls_to_terminal_state(monkeypatch) -> None:
    """异步视频提交后会保留任务号并轮询到成功，不依赖具体供应商状态字段。"""

    class FakeVideoProvider:
        def __init__(self) -> None:
            self.task_ids: list[str] = []

        def poll(self, provider_task_id: str) -> VideoTaskResult:
            self.task_ids.append(provider_task_id)
            return VideoTaskResult(
                provider_task_id=provider_task_id,
                status="SUCCEEDED",
                video_url="https://cdn.example/clip.mp4",
            )

    monkeypatch.setattr("app.services.v1_model_adapter_service.time.sleep", lambda _seconds: None)
    provider = FakeVideoProvider()
    result = wait_for_video_result(
        provider,
        {"provider_config": {"poll_interval_seconds": 1, "max_poll_seconds": 10}},
        VideoTaskResult(provider_task_id="task-1", status="PENDING"),
    )

    assert provider.task_ids == ["task-1"]
    assert result.status == "SUCCEEDED"
    assert result.video_url == "https://cdn.example/clip.mp4"


def test_v1_video_waiter_retries_only_get_after_transient_poll_network_error(monkeypatch) -> None:
    """轮询网络波动只重试同一任务号的 poll；等待器本身没有 submit 能力。"""

    class RecoveringPollProvider:
        def __init__(self) -> None:
            self.poll_ids: list[str] = []

        def poll(self, provider_task_id: str) -> VideoTaskResult:
            self.poll_ids.append(provider_task_id)
            if len(self.poll_ids) == 1:
                raise ProviderPollNetworkError("temporary")
            return VideoTaskResult(
                provider_task_id=provider_task_id,
                status="SUCCEEDED",
                video_url="https://cdn.example/clip.mp4",
            )

    monkeypatch.setattr("app.services.v1_model_adapter_service.time.sleep", lambda _seconds: None)
    provider = RecoveringPollProvider()
    result = wait_for_video_result(
        provider,
        {"provider_config": {"poll_interval_seconds": 1, "max_poll_seconds": 10, "poll_network_retry_count": 1}},
        VideoTaskResult(provider_task_id="already-created-task", status="PENDING"),
    )

    assert provider.poll_ids == ["already-created-task", "already-created-task"]
    assert result.status == "SUCCEEDED"


def test_video_request_accepts_verified_in_memory_first_frame_without_public_url() -> None:
    """单机 Commerce 首帧可只在 Worker 内存中传给原生方舟 Adapter。"""

    reference = LocalImageReference(
        asset_id="keyframe-1", role="first_frame", mime_type="image/jpeg",
        width=2848, height=1600, sha256="a" * 64,
        data_url="data:image/jpeg;base64,AAECAwQ=",
    )
    request = create_video_request(
        project_id="project-1", shot_number=1, prompt="固定提示词", image_urls=[], reference_images=[reference]
    )
    assert request.image_urls == []
    assert request.reference_images == [reference]
