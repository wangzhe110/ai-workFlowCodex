"""火山方舟 Seedance 视频适配器的协议回归测试。"""

from pathlib import Path

import pytest

from app.services import analysis_provider
from app.services.analysis_provider import (
    ProviderPollNetworkError,
    ProviderTransportError,
    VideoGenerationInput,
    VolcengineArkVideoProvider,
)
from app.services.storage import LocalImageReference, local_asset_storage


def _snapshot() -> dict:
    """构造与模型配置中心保存格式一致的方舟视频配置快照。"""

    return {
        "provider_key": "volcengine_ark_video",
        "model_key": "doubao-seedance-2-5-260628",
        "provider_config": {
            "api_base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "secret_env_name": "ARK_API_KEY",
            "ratio": "16:9",
            "duration": 5,
            "resolution": "480p",
            "generate_audio": False,
        },
    }


def _first_frame_reference() -> LocalImageReference:
    """构造已验证本地关键帧的内存摘要；Data URL 不代表持久化快照。"""

    return LocalImageReference(
        asset_id="keyframe-1",
        role="first_frame",
        mime_type="image/jpeg",
        width=2848,
        height=1600,
        sha256="f" * 64,
        data_url="data:image/jpeg;base64,AAECAwQ=",
    )


def test_ark_provider_uses_official_task_endpoints_and_first_frame(monkeypatch) -> None:
    """创建任务时固定使用方舟任务接口和首帧图生视频请求结构。"""

    monkeypatch.setenv("ARK_API_KEY", "test-key")
    observed: dict = {}

    def fake_post(url, api_key, payload, timeout):
        observed.update(url=url, api_key=api_key, payload=payload, timeout=timeout)
        return {"id": "ark-task-001"}

    monkeypatch.setattr(analysis_provider, "_post_json", fake_post)
    provider = VolcengineArkVideoProvider(_snapshot())
    result = provider.submit(
        VideoGenerationInput(
            project_id="project-1",
            group_number=1,
            start_shot_number=1,
            end_shot_number=2,
            prompt="人物回头，镜头缓慢推进，电影短剧质感",
            image_urls=["https://cdn.example/first-frame.png"],
        )
    )

    assert result.provider_task_id == "ark-task-001"
    assert result.status == "PENDING"
    assert observed["url"] == "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
    assert observed["api_key"] == "test-key"
    assert observed["payload"] == {
        "model": "doubao-seedance-2-5-260628",
        "content": [
            {"type": "text", "text": "人物回头，镜头缓慢推进，电影短剧质感"},
            {
                "type": "image_url",
                "image_url": {"url": "https://cdn.example/first-frame.png"},
                "role": "first_frame",
            },
        ],
        "duration": 5,
        "resolution": "480p",
        "generate_audio": False,
    }


def test_ark_provider_uses_in_memory_first_frame_data_url(monkeypatch) -> None:
    """单机关键帧必须在内存中转换为官方 first_frame Data URL。"""

    monkeypatch.setenv("ARK_API_KEY", "test-key")
    observed: dict = {}
    monkeypatch.setattr(
        analysis_provider,
        "_post_json",
        lambda url, api_key, payload, timeout: observed.update(url=url, payload=payload) or {"id": "ark-task-002"},
    )

    result = VolcengineArkVideoProvider(_snapshot()).submit(
        VideoGenerationInput(
            project_id="project-1",
            group_number=1,
            start_shot_number=1,
            end_shot_number=1,
            prompt="固定提示词",
            image_urls=[],
            reference_images=[_first_frame_reference()],
        )
    )

    assert result.provider_task_id == "ark-task-002"
    content = observed["payload"]["content"]
    assert content[1]["role"] == "first_frame"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    # 图生视频的画幅由首帧决定；真实方舟拒绝同时传 ratio。
    assert "ratio" not in observed["payload"]
    # 适配器标准返回只保留任务 ID，Data URL 不会成为结果/审计字段。
    assert "data:image" not in repr(result)


def test_ark_provider_reads_succeeded_video_url(monkeypatch) -> None:
    """方舟任务成功时，从官方响应的 content.video_url 提取最终片段地址。"""

    monkeypatch.setenv("ARK_API_KEY", "test-key")

    def fake_get(url, api_key, timeout):
        assert url.endswith("/contents/generations/tasks/ark-task-001")
        assert api_key == "test-key"
        return {
            "id": "ark-task-001",
            "status": "succeeded",
            "content": {"video_url": "https://cdn.example/generated.mp4"},
        }

    monkeypatch.setattr(analysis_provider, "_get_json", fake_get)
    result = VolcengineArkVideoProvider(_snapshot()).poll("ark-task-001")

    assert result.status == "SUCCEEDED"
    assert result.video_url == "https://cdn.example/generated.mp4"


def test_ark_provider_maps_existing_task_transport_failure_to_recoverable_poll_error(monkeypatch) -> None:
    """已有任务号的 GET 连接波动必须有专属语义，不能被误判为创建失败。"""

    monkeypatch.setenv("ARK_API_KEY", "test-key")
    monkeypatch.setattr(
        analysis_provider,
        "_get_json",
        lambda *_args: (_ for _ in ()).throw(ProviderTransportError("safe transport failure")),
    )

    with pytest.raises(ProviderPollNetworkError, match="恢复查询"):
        VolcengineArkVideoProvider(_snapshot()).poll("ark-existing-task")


def test_ark_provider_treats_expired_as_terminal_failure(monkeypatch) -> None:
    """expired 不能继续轮询或重新提交。"""

    monkeypatch.setenv("ARK_API_KEY", "test-key")
    monkeypatch.setattr(
        analysis_provider,
        "_get_json",
        lambda *_args: {"id": "ark-task-expired", "status": "expired"},
    )
    result = VolcengineArkVideoProvider(_snapshot()).poll("ark-task-expired")
    assert result.status == "FAILED"
    assert result.provider_task_id == "ark-task-expired"


def test_ark_provider_does_not_fallback_to_yunwu_secret_channels(monkeypatch) -> None:
    """视频 Profile 缺少 ARK Key 时必须失败，不能误借两条云雾 Key。"""

    monkeypatch.setenv("YUNWU_REASONING_API_KEY", "reasoning-channel-key")
    monkeypatch.setenv("YUNWU_IMAGE_API_KEY", "image-channel-key")
    monkeypatch.delenv("ARK_API_KEY", raising=False)

    provider = VolcengineArkVideoProvider(_snapshot())
    try:
        provider.submit(
            VideoGenerationInput(
                project_id="project-1",
                group_number=1,
                start_shot_number=1,
                end_shot_number=1,
                prompt="测试",
                image_urls=["https://cdn.example/first-frame.png"],
            )
        )
    except RuntimeError as exc:
        assert "ARK_API_KEY" in str(exc)
    else:
        raise AssertionError("缺少 ARK_API_KEY 时不应提交视频任务")


def test_generated_video_download_sends_no_authorization_and_requires_mp4(monkeypatch) -> None:
    """签名下载只使用临时 HTTPS 地址，绝不把模型鉴权带给媒体 CDN。"""

    observed: dict = {}

    class Headers:
        @staticmethod
        def get_content_type() -> str:
            return "video/mp4"

    class Response:
        status = 200
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self) -> int:
            return 200

        @staticmethod
        def geturl() -> str:
            return "https://cdn.example/result.mp4?signature=temporary"

        def read(self, _size: int) -> bytes:
            if observed.get("read_once"):
                return b""
            observed["read_once"] = True
            return b"\x00\x00\x00\x18ftypisomtest-mp4"

    def fake_open(request, timeout):
        observed["headers"] = dict(request.header_items())
        observed["timeout"] = timeout
        return Response()

    monkeypatch.setattr("app.services.storage.urlopen", fake_open)
    content_type, content = local_asset_storage.download_generated_video(
        "https://cdn.example/result.mp4?signature=temporary"
    )
    assert content_type == "video/mp4"
    assert content[4:8] == b"ftyp"
    assert not any(key.lower() == "authorization" for key in observed["headers"])


def test_generated_video_storage_rejects_non_mp4_before_persisting(tmp_path, monkeypatch) -> None:
    """下载异常内容不得变成本地视频资产。"""

    monkeypatch.setattr("app.services.storage.settings", type("Settings", (), {"local_storage_path": tmp_path, "max_upload_bytes": 1024})())
    try:
        local_asset_storage.save_generated_video_bytes(
            project_id="project-1", asset_kind="commerce-video", asset_id="clip-1", version=1,
            content=b"not-an-mp4", content_type="video/mp4",
        )
    except RuntimeError as exc:
        assert "MP4" in str(exc)
    else:
        raise AssertionError("无 ftyp 的内容不得写入本地媒体")
