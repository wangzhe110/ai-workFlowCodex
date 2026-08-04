"""OpenAI 兼容文本适配器的请求归一化与 JSON 解析测试。"""

import json

from app.services.analysis_provider import (
    ConfigurableAsyncVideoProvider,
    OpenAICompatibleImageProvider,
    OpenAICompatibleJsonProvider,
    OpenAICompatibleTranscriptionProvider,
    OpenAICompatibleVisionAnalysisProvider,
    VideoAnalysisInput,
    VideoGenerationInput,
)
from app.services.video_audio_service import ExtractedVideoAudio
from app.services.video_frame_service import SampledVideoFrame


class _FakeHttpResponse:
    """替代网络响应，保证单元测试不会访问真实中转站。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_openai_compatible_provider_normalizes_url_and_json(monkeypatch) -> None:
    """根地址会补齐 /v1/chat/completions，模型代码块 JSON 能被安全解析。"""

    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeHttpResponse(
            {"choices": [{"message": {"content": "```json\n{\"result\": \"ok\"}\n```"}}]}
        )

    monkeypatch.setenv("YUNWU_API_KEY", "test-only-key")
    monkeypatch.setattr("app.services.analysis_provider.urlopen", fake_urlopen)
    provider = OpenAICompatibleJsonProvider(
        {
            "model_key": "test-model",
            "provider_config": {
                "api_base_url": "https://yunwu.ai",
                "secret_env_name": "YUNWU_API_KEY",
                "timeout_seconds": 120,
            },
        }
    )

    result = provider.generate_json(
        system_instruction="只返回 JSON",
        user_payload={"topic": "测试"},
        output_contract='{"result":"string"}',
    )

    assert result == {"result": "ok"}
    assert captured["url"] == "https://yunwu.ai/v1/chat/completions"
    assert captured["timeout"] == 120
    assert captured["body"]["model"] == "test-model"


def test_openai_compatible_image_provider_returns_remote_image_url(monkeypatch) -> None:
    """图片适配器向 images/generations 发送单图请求并提取稳定 URL。"""

    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeHttpResponse({"data": [{"url": "https://cdn.example/generated.png"}]})

    monkeypatch.setenv("YUNWU_API_KEY", "test-only-key")
    monkeypatch.setattr("app.services.analysis_provider.urlopen", fake_urlopen)
    provider = OpenAICompatibleImageProvider(
        {
            "model_key": "image-model",
            "provider_config": {
                "api_base_url": "https://yunwu.ai/v1",
                "secret_env_name": "YUNWU_API_KEY",
                "image_size": "1728x2304",
                "image_request_options": {"watermark": False},
            },
        }
    )

    assert provider.generate("原创短剧分镜") == "https://cdn.example/generated.png"
    assert captured["url"] == "https://yunwu.ai/v1/images/generations"
    assert captured["body"]["model"] == "image-model"
    assert captured["body"]["size"] == "1728x2304"
    assert captured["body"]["watermark"] is False


def test_openai_compatible_image_provider_requires_explicit_reference_field(monkeypatch) -> None:
    """参考生图不会悄悄退化为文生图，字段名由模型配置而非业务代码决定。"""

    captured = {}

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeHttpResponse({"data": [{"url": "https://cdn.example/generated.png"}]})

    monkeypatch.setenv("YUNWU_API_KEY", "test-only-key")
    monkeypatch.setattr("app.services.analysis_provider.urlopen", fake_urlopen)
    provider = OpenAICompatibleImageProvider(
        {
            "model_key": "image-model",
            "provider_config": {
                "api_base_url": "https://yunwu.ai/v1",
                "secret_env_name": "YUNWU_API_KEY",
                "reference_image_field": "images",
            },
        }
    )

    result = provider.generate(
        "以锁定角色和场景为参考生成关键帧",
        reference_image_urls=["https://cdn.example/character.png", "https://cdn.example/scene.png"],
    )

    assert result == "https://cdn.example/generated.png"
    assert captured["body"]["images"] == [
        "https://cdn.example/character.png",
        "https://cdn.example/scene.png",
    ]


def test_configurable_async_video_provider_submits_then_polls(monkeypatch) -> None:
    """异步视频适配器可由配置映射请求字段、任务状态和最终 MP4 地址。"""

    captured: list[dict] = []
    responses = iter(
        [
            {"id": "video-task-1", "state": "pending"},
            {"id": "video-task-1", "state": "completed", "video": {"url": "https://cdn.example/clip.mp4"}},
        ]
    )

    def fake_urlopen(request, timeout):
        captured.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "body": json.loads(request.data.decode("utf-8")) if request.data else None,
                "timeout": timeout,
            }
        )
        return _FakeHttpResponse(next(responses))

    monkeypatch.setenv("VIDEO_RELAY_API_KEY", "test-only-key")
    monkeypatch.setattr("app.services.analysis_provider.urlopen", fake_urlopen)
    provider = ConfigurableAsyncVideoProvider(
        {
            "model_key": "chosen-video-model",
            "provider_config": {
                "api_base_url": "https://relay.example",
                "secret_env_name": "VIDEO_RELAY_API_KEY",
                "submit_path": "/luma/generations",
                "query_path_template": "/luma/generations/{task_id}",
                "prompt_field": "user_prompt",
                "image_input_mode": "luma_keyframe",
                "video_request_options": {"duration": "5s", "aspect_ratio": "9:16"},
                "timeout_seconds": 120,
            },
        }
    )

    submitted = provider.submit(
        VideoGenerationInput(
            project_id="project-1",
            group_number=1,
            start_shot_number=1,
            end_shot_number=4,
            prompt="角色向前走，镜头平稳推进",
            image_urls=["https://cdn.example/shot-1.png", "https://cdn.example/shot-4.png"],
        )
    )
    completed = provider.poll(submitted.provider_task_id)

    assert submitted.status == "PENDING"
    assert completed.status == "SUCCEEDED"
    assert completed.video_url == "https://cdn.example/clip.mp4"
    assert captured[0]["url"] == "https://relay.example/luma/generations"
    assert captured[0]["method"] == "POST"
    assert captured[0]["body"]["user_prompt"] == "角色向前走，镜头平稳推进"
    assert captured[0]["body"]["keyframes"]["frame0"]["url"] == "https://cdn.example/shot-1.png"
    assert captured[1]["url"] == "https://relay.example/luma/generations/video-task-1"
    assert captured[1]["method"] == "GET"


def test_openai_compatible_vision_provider_sends_sampled_frames_and_keeps_abstract_fields(monkeypatch) -> None:
    """视觉适配器把内存抽帧作为多模态内容发送，并过滤模型的额外原始字段。"""

    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeHttpResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "开场先抛出异常，再建立即时目标。",
                                    "opening_mechanism": ["异常事件", "即时目标"],
                                    "viral_elements": ["冲突清晰", "悬念递进"],
                                    "pacing_notes": "每一小段推进一个新信息。",
                                    "compliance_note": "仅使用抽象机制，不复用具体表达。",
                                    "untrusted_raw_field": "不应保留",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }
        )

    monkeypatch.setenv("YUNWU_API_KEY", "test-only-key")
    monkeypatch.setattr("app.services.analysis_provider.urlopen", fake_urlopen)
    provider = OpenAICompatibleVisionAnalysisProvider(
        {
            "model_key": "vision-model",
            "provider_config": {
                "api_base_url": "https://yunwu.ai/v1",
                "secret_env_name": "YUNWU_API_KEY",
                    "frame_sample_count": 2,
                    "frame_max_bytes": 2097152,
            },
        }
    )

    result = provider.analyze(
        VideoAnalysisInput(
            asset_id="asset-1",
            filename="reference.mp4",
            content_type="video/mp4",
            sampled_frames=[
                SampledVideoFrame(0.5, "data:image/jpeg;base64,Zmlyc3Q="),
                SampledVideoFrame(3.0, "data:image/jpeg;base64,c2Vjb25k"),
            ],
            transcript_for_mechanism_analysis="这段原始语音只应在本次内存分析中使用。",
        )
    )

    content = captured["body"]["messages"][1]["content"]
    assert captured["url"] == "https://yunwu.ai/v1/chat/completions"
    assert [item["image_url"]["url"] for item in content if item["type"] == "image_url"] == [
        "data:image/jpeg;base64,Zmlyc3Q=",
        "data:image/jpeg;base64,c2Vjb25k",
    ]
    assert result["source"]["sampled_frame_timestamps"] == [0.5, 3.0]
    assert result["source"]["audio_mechanism_considered"] is True
    assert "原始语音" not in json.dumps(result, ensure_ascii=False)
    assert "untrusted_raw_field" not in result


def test_openai_compatible_vision_provider_supports_v1_reviewable_contract(monkeypatch) -> None:
    """配置 V1 结果契约后，视觉模型输出五类审核数据而不影响旧流程契约。"""

    def fake_urlopen(request, timeout):
        return _FakeHttpResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "video_script_structure": {"theme": "异常信息驱动", "structure": ["异常", "目标", "反转"]},
                                    "opening_analysis": {"time_window": "前 3 秒", "hook_type": "异常来电", "mechanism": "先抛问题再给目标"},
                                    "viral_elements": [{"type": "conflict", "description": "目标与阻碍同现"}],
                                    "scene_analysis": [{"role": "建立压力", "visual_style": "近景高信息密度"}],
                                    "creative_brief": {"originality_rule": "只复用机制", "recommended_rhythm": "每段推进", "target_format": "9:16"},
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }
        )

    monkeypatch.setenv("YUNWU_API_KEY", "test-only-key")
    monkeypatch.setattr("app.services.analysis_provider.urlopen", fake_urlopen)
    provider = OpenAICompatibleVisionAnalysisProvider(
        {
            "model_key": "vision-model",
            "provider_config": {
                "api_base_url": "https://yunwu.ai/v1",
                "secret_env_name": "YUNWU_API_KEY",
                "result_contract": "V1_REFERENCE_ANALYSIS",
            },
        }
    )

    result = provider.analyze(
        VideoAnalysisInput(
            asset_id="asset-1",
            filename="reference.mp4",
            content_type="video/mp4",
            sampled_frames=[SampledVideoFrame(1.0, "data:image/jpeg;base64,Zmlyc3Q=")],
        )
    )

    assert result["video_script_structure"]["theme"] == "异常信息驱动"
    assert result["opening_analysis"]["hook_type"] == "异常来电"
    assert result["viral_elements"] == [{"type": "conflict", "description": "目标与阻碍同现"}]


def test_openai_compatible_transcription_provider_uses_multipart_and_returns_memory_text(monkeypatch) -> None:
    """ASR 适配器提交短暂 MP3，并将响应限制为任务内存对象。"""

    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["content_type"] = request.headers["Content-type"]
        captured["body"] = request.data
        captured["timeout"] = timeout
        return _FakeHttpResponse({"text": "开头语速快，先给出异常信息。"})

    monkeypatch.setenv("YUNWU_API_KEY", "test-only-key")
    monkeypatch.setattr("app.services.analysis_provider.urlopen", fake_urlopen)
    provider = OpenAICompatibleTranscriptionProvider(
        {
            "model_key": "asr-model",
            "provider_config": {
                "api_base_url": "https://yunwu.ai/v1",
                "secret_env_name": "YUNWU_API_KEY",
                "transcription_request_options": {"language": "zh"},
            },
        }
    )

    result = provider.transcribe(
        ExtractedVideoAudio(
            filename="reference-opening.mp3",
            content_type="audio/mpeg",
            data=b"fake-mp3-bytes",
            duration_seconds=180,
        )
    )

    assert captured["url"] == "https://yunwu.ai/v1/audio/transcriptions"
    assert captured["content_type"].startswith("multipart/form-data; boundary=")
    assert b'name="model"' in captured["body"]
    assert b"asr-model" in captured["body"]
    assert b'filename="reference-opening.mp3"' in captured["body"]
    assert result.text == "开头语速快，先给出异常信息。"
    assert result.audio_seconds == 180
