"""云雾 Nano Banana Fal 队列图片 Adapter 的协议与安全边界测试。

测试全部替换网络入口并使用假密钥：不会访问云雾、不会产生图片费用。重点验证
``request_id`` 恢复、鉴权域名和图片下载边界，而不是模拟供应商的完整平台。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.services import analysis_provider
from app.services.analysis_provider import FalQueueImageProvider
from app.services.v1_model_adapter_service import start_image_generation, wait_for_image_result


_PNG = b"\x89PNG\r\n\x1a\n" + b"minimal-fake-png"


class _Headers:
    def __init__(self, content_type: str = "application/json", content_length: int | None = None) -> None:
        self.content_type = content_type
        self.content_length = content_length

    def get_content_type(self) -> str:
        return self.content_type

    def get(self, key: str, default: object = None):
        if key.lower() == "content-length" and self.content_length is not None:
            return str(self.content_length)
        return default


class _Response:
    def __init__(
        self,
        payload: dict | bytes,
        *,
        content_type: str = "application/json",
        final_url: str | None = None,
    ) -> None:
        self._payload = payload
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.headers = _Headers(content_type, len(raw))
        self._raw = raw
        self._final_url = final_url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit: int | None = None) -> bytes:
        return self._raw if _limit is None else self._raw[:_limit]

    def geturl(self) -> str:
        return self._final_url or "https://yunwu.ai/fal-ai/nano-banana"


def _snapshot(**config: object) -> dict:
    return {
        "adapter_key": "fal_queue_image",
        "model_key": "banana",
        "provider_config": {
            "api_base_url": "https://yunwu.ai",
            "secret_env_name": "YUNWU_IMAGE_API_KEY",
            "timeout_seconds": 20,
            "poll_interval_seconds": 1,
            "max_poll_seconds": 10,
            **config,
        },
    }


def test_fal_queue_submits_once_rewrites_queue_urls_and_downloads_without_auth(monkeypatch) -> None:
    """请求鉴权只发向云雾；Fal 返回 URL 换源，结果图下载绝不带模型 Key。"""

    seen = []

    def fake_urlopen(request, timeout):
        seen.append(request)
        if request.get_method() == "POST":
            assert request.full_url == "https://yunwu.ai/fal-ai/nano-banana"
            assert json.loads(request.data.decode("utf-8")) == {
                "prompt": "白底产品图",
                "image_urls": ["https://cdn.example/reference.png"],
            }
            return _Response(
                {
                    "request_id": "fal-request-1",
                    "status_url": "https://queue.fal.run/fal-ai/nano-banana/requests/fal-request-1/status?signature=hidden",
                    "response_url": "https://queue.fal.run/fal-ai/nano-banana/requests/fal-request-1?signature=hidden",
                }
            )
        if request.full_url.endswith("/status"):
            assert request.full_url == "https://yunwu.ai/fal-ai/nano-banana/requests/fal-request-1/status"
            return _Response({"status": "COMPLETED"})
        if request.full_url == "https://yunwu.ai/fal-ai/nano-banana/requests/fal-request-1":
            return _Response({"images": [{"url": "https://cdn.example/image.png?temporary=hidden"}]})
        assert request.full_url == "https://cdn.example/image.png?temporary=hidden"
        assert request.get_header("Authorization") is None
        assert request.get_header("Accept") == "image/*"
        return _Response(_PNG, content_type="image/png", final_url=request.full_url)

    monkeypatch.setenv("YUNWU_IMAGE_API_KEY", "test-image-key")
    monkeypatch.setattr(analysis_provider, "urlopen", fake_urlopen)

    provider = FalQueueImageProvider(_snapshot())
    submitted = provider.submit("白底产品图", reference_image_urls=["https://cdn.example/reference.png"])
    completed = provider.poll("fal-request-1")

    assert submitted.provider_task_id == "fal-request-1"
    assert submitted.status == "PENDING"
    assert completed.status == "SUCCEEDED"
    assert completed.image_url == "https://cdn.example/image.png?temporary=hidden"
    assert completed.content_type == "image/png"
    assert completed.byte_size == len(_PNG)
    assert completed.sha256 == hashlib.sha256(_PNG).hexdigest()
    assert len(seen) == 4
    for request in seen[:3]:
        assert request.full_url.startswith("https://yunwu.ai/fal-ai/nano-banana")
        assert request.get_header("Authorization") == "Bearer test-image-key"
    assert seen[3].get_header("Authorization") is None


def test_fal_resume_existing_request_only_polls_without_second_paid_submit(monkeypatch) -> None:
    """已落库 request_id 的 Worker 恢复只查询任务，不会再次 POST。"""

    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.get_method(), request.full_url))
        assert request.get_method() == "GET"
        return _Response({"status": "PENDING"})

    monkeypatch.setenv("YUNWU_IMAGE_API_KEY", "test-image-key")
    monkeypatch.setattr(analysis_provider, "urlopen", fake_urlopen)

    provider, result = start_image_generation(
        _snapshot(),
        prompt="不能重提",
        existing_provider_task_id="persisted-request-9",
    )

    assert provider is not None
    assert result.status == "PENDING"
    assert result.provider_task_id == "persisted-request-9"
    assert calls == [("GET", "https://yunwu.ai/fal-ai/nano-banana/requests/persisted-request-9/status")]


def test_fal_image_rejects_non_https_result_and_missing_image_key_has_no_fallback(monkeypatch) -> None:
    """返回图与密钥都必须走图片通道，不能借推理通道或降级成 Mock。"""

    monkeypatch.setenv("YUNWU_REASONING_API_KEY", "reasoning-must-not-be-used")
    monkeypatch.delenv("YUNWU_IMAGE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="YUNWU_IMAGE_API_KEY"):
        FalQueueImageProvider(_snapshot()).submit("测试")

    monkeypatch.setenv("YUNWU_IMAGE_API_KEY", "test-image-key")

    def fake_urlopen(request, timeout):
        return _Response({"request_id": "task-a", "status": "COMPLETED"}) if request.get_method() == "POST" else _Response(
            {"images": [{"url": "http://not-allowed.example/image.png"}]}
        )

    monkeypatch.setattr(analysis_provider, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="HTTPS"):
        FalQueueImageProvider(_snapshot()).submit("测试")


def test_fal_image_rejects_mime_and_magic_mismatch(monkeypatch) -> None:
    """图片下载即使宣称 PNG，也必须通过文件头校验后才能写入后续资产。"""

    def fake_urlopen(request, timeout):
        if request.get_method() == "POST":
            return _Response({"request_id": "task-mime", "status": "COMPLETED"})
        if request.full_url.endswith("/task-mime"):
            return _Response({"images": [{"url": "https://cdn.example/not-really-png.png"}]})
        return _Response(b"not-a-png", content_type="image/png", final_url=request.full_url)

    monkeypatch.setenv("YUNWU_IMAGE_API_KEY", "test-image-key")
    monkeypatch.setattr(analysis_provider, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="文件头"):
        FalQueueImageProvider(_snapshot()).submit("测试")


def test_wait_uses_existing_task_result_without_new_submission(monkeypatch) -> None:
    """通用轮询函数只调用 provider.poll，避免调用路径暗中触发新的 submit。"""

    class _Provider:
        model_key = "banana"
        provider_key = "fal_queue_image"

        def __init__(self) -> None:
            self.calls = 0

        def poll(self, task_id: str):
            self.calls += 1
            from app.services.analysis_provider import ImageTaskResult

            return ImageTaskResult(provider_task_id=task_id, status="SUCCEEDED", image_url="https://cdn.example/result.png")

    provider = _Provider()
    from app.services.analysis_provider import ImageTaskResult

    result = wait_for_image_result(
        provider,
        _snapshot(poll_interval_seconds=1, max_poll_seconds=10),
        ImageTaskResult(provider_task_id="request-1", status="PENDING"),
    )

    assert provider.calls == 1
    assert result.status == "SUCCEEDED"
