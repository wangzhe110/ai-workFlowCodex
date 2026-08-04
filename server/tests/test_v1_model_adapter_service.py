"""V1 模型 Adapter 的能力隔离与异步视频轮询测试。"""

import pytest

from app.services.analysis_provider import VideoTaskResult
from app.services.v1_model_adapter_service import assert_supported, wait_for_video_result


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
