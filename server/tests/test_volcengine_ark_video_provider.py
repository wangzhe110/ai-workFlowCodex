"""火山方舟 Seedance 视频适配器的协议回归测试。"""

from app.services import analysis_provider
from app.services.analysis_provider import VideoGenerationInput, VolcengineArkVideoProvider


def _snapshot() -> dict:
    """构造与模型配置中心保存格式一致的方舟视频配置快照。"""

    return {
        "provider_key": "volcengine_ark_video",
        "model_key": "doubao-seedance-2-0-mini-260615",
        "provider_config": {
            "secret_env_name": "ARK_API_KEY",
            "ratio": "9:16",
            "duration": 5,
        },
    }


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
            prompt="人物回头，镜头缓慢推进，竖屏短剧质感",
            image_urls=["https://cdn.example/first-frame.png"],
        )
    )

    assert result.provider_task_id == "ark-task-001"
    assert result.status == "PENDING"
    assert observed["url"] == "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
    assert observed["api_key"] == "test-key"
    assert observed["payload"] == {
        "model": "doubao-seedance-2-0-mini-260615",
        "content": [
            {"type": "text", "text": "人物回头，镜头缓慢推进，竖屏短剧质感"},
            {
                "type": "image_url",
                "image_url": {"url": "https://cdn.example/first-frame.png"},
                "role": "first_frame",
            },
        ],
        "ratio": "9:16",
        "duration": 5,
    }


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
