"""方舟官方 Seedream 单图 Adapter 的协议、安全与本地转存测试。

所有网络入口均被替换为假的标准库响应；本文件不会读取真实密钥、不会访问方舟，
也不会创建供应商图片任务。
"""

from __future__ import annotations

from base64 import b64encode
from dataclasses import replace
import hashlib
import json
import struct

import pytest

from app.services import analysis_provider
from app.services import storage
from app.services.analysis_provider import VolcengineArkImageProvider
from app.services.sensitive_data import redact_sensitive_data, sanitize_error_summary
from app.services.v1_model_adapter_service import persist_v1_image_bytes, start_image_generation


def _png(width: int = 2, height: int = 3) -> bytes:
    """构造足够供 Adapter 校验 MIME、魔数和尺寸的最小 PNG 头。"""

    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"


class _Headers:
    def __init__(self, content_type: str, content_length: int) -> None:
        self._content_type = content_type
        self._content_length = content_length

    def get_content_type(self) -> str:
        return self._content_type

    def get(self, key: str, default: object = None):
        return str(self._content_length) if key.lower() == "content-length" else default


class _Response:
    def __init__(self, payload: dict | bytes, *, content_type: str = "application/json", final_url: str | None = None, status: int = 200) -> None:
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.headers = _Headers(content_type, len(self._raw))
        self._final_url = final_url
        self._status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit: int | None = None) -> bytes:
        return self._raw if _limit is None else self._raw[:_limit]

    def geturl(self) -> str:
        return self._final_url or "https://ark.cn-beijing.volces.com/image"

    def getcode(self) -> int:
        return self._status


def _snapshot(**config: object) -> dict:
    return {
        "adapter_key": "volcengine_ark_image",
        "model_key": "doubao-seedream-5-0-260128",
        "provider_config": {
            "api_base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "secret_env_name": "ARK_API_KEY",
            "timeout_seconds": 20,
            "size": "2K",
            "sequential_image_generation": "disabled",
            "response_format": "url",
            "watermark": False,
            **config,
        },
    }


def _reference(
    *, asset_id: str, role: str, content: bytes | None = None, content_type: str = "image/png"
) -> storage.LocalImageReference:
    """构造仅供 Adapter 单测使用的内存参考图，不接触网络或数据库。"""

    body = content or _png()
    return storage.LocalImageReference(
        asset_id=asset_id,
        role=role,
        mime_type=content_type,
        width=2,
        height=3,
        sha256=hashlib.sha256(body).hexdigest(),
        data_url=f"data:{content_type};base64,{b64encode(body).decode('ascii')}",
    )


def test_ark_seedream_uses_exact_official_path_one_safe_request_and_local_bytes(monkeypatch) -> None:
    """请求不产生 /v1 路径、下载不带鉴权，临时 URL 不进入标准化结果。"""

    signed_url = "https://image.example/result.png?signature=temporary-secret"
    seen = []

    def fake_urlopen(request, timeout: float):
        seen.append(request)
        if request.get_method() == "POST":
            assert request.full_url == "https://ark.cn-beijing.volces.com/api/v3/images/generations"
            assert request.get_header("Authorization") == "Bearer unit-test-ark-key"
            assert json.loads(request.data.decode("utf-8")) == {
                "model": "doubao-seedream-5-0-260128",
                "prompt": "测试产品图",
                "size": "2K",
                "sequential_image_generation": "disabled",
                "response_format": "url",
                "watermark": False,
            }
            return _Response({"data": [{"url": signed_url}]})
        assert request.full_url == signed_url
        assert request.get_header("Authorization") is None
        assert request.get_header("Accept") == "image/*"
        return _Response(_png(), content_type="image/png", final_url=signed_url)

    monkeypatch.setenv("ARK_API_KEY", "unit-test-ark-key")
    monkeypatch.setattr(analysis_provider, "urlopen", fake_urlopen)

    result = VolcengineArkImageProvider(_snapshot()).generate("测试产品图")

    assert result.status == "SUCCEEDED"
    assert result.provider_task_id is None
    assert result.image_url is None
    assert result.image_bytes == _png()
    assert result.content_type == "image/png"
    assert result.width == 2 and result.height == 3
    assert result.byte_size == len(_png())
    assert result.sha256 == hashlib.sha256(_png()).hexdigest()
    assert len(seen) == 2
    assert signed_url not in repr(result)


def test_ark_seedream_missing_key_never_borrows_yunwu_or_mock(monkeypatch) -> None:
    """缺 ARK Key 是明确错误，不能借用任一云雾通道或 Mock。"""

    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setenv("YUNWU_REASONING_API_KEY", "must-not-be-used")
    monkeypatch.setenv("YUNWU_IMAGE_API_KEY", "must-not-be-used")

    with pytest.raises(RuntimeError, match="ARK_API_KEY"):
        start_image_generation(_snapshot(), prompt="测试产品图")


@pytest.mark.parametrize(
    ("content_type", "content", "message"),
    [
        ("text/plain", _png(), "MIME"),
        ("image/png", b"not-an-image", "文件头"),
    ],
)
def test_ark_seedream_rejects_invalid_download_before_persistence(monkeypatch, content_type: str, content: bytes, message: str) -> None:
    """下载响应需要 MIME 与文件头都有效；失败不会产生任何标准化图片 URL。"""

    calls = 0

    def fake_urlopen(request, timeout: float):
        nonlocal calls
        calls += 1
        if request.get_method() == "POST":
            return _Response({"data": [{"url": "https://image.example/temporary.png?signature=never-persist"}]})
        return _Response(content, content_type=content_type, final_url=request.full_url)

    monkeypatch.setenv("ARK_API_KEY", "unit-test-ark-key")
    monkeypatch.setattr(analysis_provider, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match=message) as raised:
        VolcengineArkImageProvider(_snapshot()).generate("测试产品图")
    assert "signature=never-persist" not in str(raised.value)
    assert calls == 2


def test_ark_seedream_post_does_not_retry_and_rejects_untrusted_reference_urls(monkeypatch) -> None:
    """网络故障只尝试一次 POST；本地 URL 不能越过资产解析边界。"""

    calls = 0

    def fail_once(_request, *, timeout: float):
        nonlocal calls
        calls += 1
        raise OSError("network unavailable")

    monkeypatch.setenv("ARK_API_KEY", "unit-test-ark-key")
    monkeypatch.setattr(analysis_provider, "urlopen", fail_once)
    with pytest.raises(RuntimeError, match="供应商接口暂时无法连接"):
        VolcengineArkImageProvider(_snapshot()).generate("测试产品图")
    assert calls == 1

    with pytest.raises(RuntimeError, match="本地资产校验"):
        VolcengineArkImageProvider(_snapshot()).generate("测试产品图", reference_image_urls=["https://example.com/ref.png"])


def test_ark_seedream_result_bytes_are_saved_locally_without_supplier_url(monkeypatch, tmp_path) -> None:
    """转存只保存 LemonFlow 本地文件和内部路径，不依赖短期签名 URL。"""

    monkeypatch.setattr(storage, "settings", replace(storage.settings, local_storage_path=tmp_path))
    url = persist_v1_image_bytes(
        project_id="project-1",
        asset_kind="character-reference",
        asset_id="role-1",
        version=1,
        content=_png(),
        content_type="image/png",
    )
    assert url.startswith("/media/generated/projects/project-1/character-reference/")
    relative_key = url.removeprefix("/media/generated/")
    saved = tmp_path / "generated" / relative_key
    assert saved.read_bytes() == _png()
    assert "signature" not in url


def test_ark_seedream_single_local_reference_uses_official_image_string(monkeypatch) -> None:
    """一张已验证本地参考图传为官方 ``image: string``，不传本机 URL。"""

    seen_payloads: list[dict] = []

    def fake_urlopen(request, timeout: float):
        if request.get_method() == "POST":
            seen_payloads.append(json.loads(request.data.decode("utf-8")))
            return _Response({"data": [{"url": "https://image.example/result.png?signature=temporary"}]})
        return _Response(_png(), content_type="image/png", final_url=request.full_url)

    monkeypatch.setenv("ARK_API_KEY", "unit-test-ark-key")
    monkeypatch.setattr(analysis_provider, "urlopen", fake_urlopen)
    reference = _reference(asset_id="character-1", role="character")

    VolcengineArkImageProvider(_snapshot()).generate("人物测试", reference_images=[reference])

    assert len(seen_payloads) == 1
    payload = seen_payloads[0]
    assert payload["image"] == reference.data_url
    assert "image_url" not in payload and "image_urls" not in payload and "reference_images" not in payload
    assert "/media/" not in payload["image"] and "localhost" not in payload["image"]


def test_ark_seedream_two_references_keep_character_then_scene_order_and_prompt(monkeypatch) -> None:
    """关键帧的角色图、场景图按固定顺序转为 ``image: []`` 并明确各自职责。"""

    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout: float):
        if request.get_method() == "POST":
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return _Response({"data": [{"url": "https://image.example/result.png?signature=temporary"}]})
        return _Response(_png(), content_type="image/png", final_url=request.full_url)

    monkeypatch.setenv("ARK_API_KEY", "unit-test-ark-key")
    monkeypatch.setattr(analysis_provider, "urlopen", fake_urlopen)
    character = _reference(asset_id="character-1", role="character")
    scene = _reference(asset_id="scene-1", role="scene", content=_png(4, 5))

    VolcengineArkImageProvider(_snapshot()).generate("分镜动作", reference_images=[character, scene])

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["image"] == [character.data_url, scene.data_url]
    assert "参考图1是角色参考图" in payload["prompt"]
    assert "参考图2是场景参考图" in payload["prompt"]


def test_ark_seedream_reference_limit_and_invalid_order_fail_before_network(monkeypatch) -> None:
    """超过 14 张或场景在角色前的输入在发送 POST 前明确失败。"""

    calls = 0

    def forbidden_urlopen(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("非法参考图不应发起网络调用")

    monkeypatch.setenv("ARK_API_KEY", "unit-test-ark-key")
    monkeypatch.setattr(analysis_provider, "urlopen", forbidden_urlopen)
    provider = VolcengineArkImageProvider(_snapshot())
    with pytest.raises(RuntimeError, match="最多允许 14"):
        provider.generate(
            "测试",
            reference_images=[_reference(asset_id=str(index), role="character", content=_png(index + 2, 3)) for index in range(15)],
        )
    with pytest.raises(RuntimeError, match="角色图在前"):
        provider.generate(
            "测试",
            reference_images=[
                _reference(asset_id="scene", role="scene"),
                _reference(asset_id="role", role="character", content=_png(4, 5)),
            ],
        )
    assert calls == 0


def test_local_storage_reference_reads_only_expected_generated_asset_and_records_safe_metadata(monkeypatch, tmp_path) -> None:
    """资产 ID、项目目录、MIME/文件头和 Data URL 生命周期均由 Storage 边界控制。"""

    monkeypatch.setattr(storage, "settings", replace(storage.settings, local_storage_path=tmp_path))
    public_url = storage.local_asset_storage.save_generated_image_bytes(
        project_id="project-1", asset_kind="character-reference", asset_id="role-1", version=1,
        content=_png(), content_type="image/png",
    )
    reference = storage.local_asset_storage.load_generated_image_reference(
        project_id="project-1", asset_id="role-1", role="character", image_url=public_url
    )

    assert reference.data_url.startswith("data:image/png;base64,")
    assert reference.audit_metadata() == {
        "asset_id": "role-1", "role": "character", "sha256": hashlib.sha256(_png()).hexdigest(),
        "mime_type": "image/png", "width": 2, "height": 3,
    }
    assert "base64" not in repr(reference)
    with pytest.raises(RuntimeError, match="地址与冻结项目或资产 ID 不匹配"):
        storage.local_asset_storage.load_generated_image_reference(
            project_id="project-1", asset_id="other-role", role="character", image_url=public_url
        )
    with pytest.raises(RuntimeError, match="本地生成媒体目录"):
        storage.local_asset_storage.load_generated_image_reference(
            project_id="project-1", asset_id="role-1", role="character", image_url="/etc/passwd"
        )


def test_local_storage_reference_separates_audited_image_id_from_verified_storage_namespace(monkeypatch, tmp_path) -> None:
    """历史角色/场景媒体按逻辑 ID 命名时，审计仍必须保留图片版本 ID。"""

    monkeypatch.setattr(storage, "settings", replace(storage.settings, local_storage_path=tmp_path))
    public_url = storage.local_asset_storage.save_generated_image_bytes(
        project_id="project-1", asset_kind="character-reference", asset_id="logical-role-1", version=1,
        content=_png(), content_type="image/png",
    )
    reference = storage.local_asset_storage.load_generated_image_reference(
        project_id="project-1", asset_id="image-version-1", storage_namespace_id="logical-role-1",
        role="character", image_url=public_url,
    )
    assert reference.asset_id == "image-version-1"
    with pytest.raises(RuntimeError, match="地址与冻结项目或资产 ID 不匹配"):
        storage.local_asset_storage.load_generated_image_reference(
            project_id="project-1", asset_id="image-version-1", storage_namespace_id="other-logical-role",
            role="character", image_url=public_url,
        )


def test_local_storage_reference_rejects_non_image_oversize_and_traversal_before_provider(monkeypatch, tmp_path) -> None:
    """非图片、越界和超过 30MB 的文件不能变成方舟参考图。"""

    monkeypatch.setattr(storage, "settings", replace(storage.settings, local_storage_path=tmp_path))
    asset_id = "role-1"
    asset_dir = hashlib.sha256(asset_id.encode("utf-8")).hexdigest()[:16]
    root = tmp_path / "generated" / "projects" / "project-1" / "character-reference" / asset_dir
    root.mkdir(parents=True)
    bad = root / "v1-bad.png"
    bad.write_bytes(b"not-an-image")
    public = "/media/generated/projects/project-1/character-reference/" + asset_dir + "/v1-bad.png"
    with pytest.raises(RuntimeError, match="文件头"):
        storage.local_asset_storage.load_generated_image_reference(
            project_id="project-1", asset_id=asset_id, role="character", image_url=public
        )
    with pytest.raises(RuntimeError, match="存储键"):
        storage.local_asset_storage.load_generated_image_reference(
            project_id="project-1", asset_id=asset_id, role="character", image_url="/media/generated/../outside.png"
        )
    oversized = root / "v1-large.png"
    oversized.write_bytes(b"x" * (30 * 1024 * 1024 + 1))
    oversized_url = "/media/generated/projects/project-1/character-reference/" + asset_dir + "/v1-large.png"
    with pytest.raises(RuntimeError, match="30MB"):
        storage.local_asset_storage.load_generated_image_reference(
            project_id="project-1", asset_id=asset_id, role="character", image_url=oversized_url
        )


def test_data_urls_are_redacted_from_error_and_snapshot_boundaries() -> None:
    """防御历史异常数据：即使字段名正常，也不能把 Base64 返回给日志或 API。"""

    value = "data:image/png;base64,QUJDREVGRw=="
    assert "QUJD" not in sanitize_error_summary(f"provider error: {value}")
    assert "QUJD" not in repr(redact_sensitive_data({"reference": value}))


@pytest.mark.parametrize(
    ("content", "mime_type"),
    [
        (_png(), "image/png"),
        (b"\xff\xd8\xff\xe0minimal-jpeg", "image/jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8X", "image/webp"),
    ],
)
def test_reference_image_magic_detects_png_jpeg_and_webp(content: bytes, mime_type: str) -> None:
    """本地读取边界以文件头识别三种允许格式，不能只相信文件扩展名。"""

    assert storage.LocalAssetStorage._image_mime_from_magic(content) == mime_type
