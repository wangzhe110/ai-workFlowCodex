"""模型图片转存到自有对象存储的离线测试。"""

from dataclasses import replace
from email.message import Message
from pathlib import Path

from app.services import storage


class _FakeImageResponse:
    """模拟只读图片 HTTP 响应，避免测试访问任何真实图片 URL。"""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.headers = Message()
        self.headers.add_header("Content-Type", "image/png")
        self.headers.add_header("Content-Length", str(len(content)))

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.content


class _FakeS3Client:
    """记录上传参数，以验证对象键、内容与 Content-Type 的审计约定。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def put_object(self, **kwargs) -> None:
        self.calls.append(kwargs)

    def upload_fileobj(self, source, bucket: str, key: str, ExtraArgs: dict) -> None:
        self.calls.append(
            {
                "Bucket": bucket,
                "Key": key,
                "Body": source.read(),
                "ExtraArgs": ExtraArgs,
            }
        )

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        self.calls.append({"Bucket": bucket, "Key": key, "Download": filename})
        Path(filename).write_bytes(b"source-video-bytes")

    def delete_object(self, **kwargs) -> None:
        self.calls.append({"Delete": kwargs})


def test_s3_delivery_persists_provider_image_with_stable_public_url(monkeypatch) -> None:
    """转存后返回自有 HTTPS 域名，既不暴露 S3 密钥也不依赖源站临时 URL。"""

    fake_client = _FakeS3Client()
    monkeypatch.setattr(
        storage,
        "settings",
        replace(
            storage.settings,
            generated_image_delivery_mode="s3",
            s3_bucket="ai-drama-media",
            s3_public_base_url="https://cdn.example.com/ai-drama-media",
            generated_image_max_bytes=1024,
        ),
    )
    monkeypatch.setattr(
        storage,
        "urlopen",
        lambda request, timeout: _FakeImageResponse(b"png-bytes"),
    )
    monkeypatch.setattr(
        storage.S3GeneratedImageDelivery,
        "_client",
        staticmethod(lambda: fake_client),
    )

    delivery = storage.S3GeneratedImageDelivery()
    result_url = delivery.persist(
        project_id="project-1",
        storyboard_package_id="board-1",
        shot_number=2,
        version=3,
        source_url="https://provider.example/results/image.png?temporary=yes",
    )

    assert result_url.startswith("https://cdn.example.com/ai-drama-media/projects/project-1/")
    assert result_url.endswith(".png")
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["Bucket"] == "ai-drama-media"
    assert fake_client.calls[0]["Body"] == b"png-bytes"
    assert fake_client.calls[0]["ContentType"] == "image/png"


def test_s3_final_video_delivery_streams_mp4_and_returns_stable_url(monkeypatch, tmp_path) -> None:
    """完整成片可独立转存 S3，不依赖合成 Worker 的本地磁盘长期保留文件。"""

    fake_client = _FakeS3Client()
    output_path = tmp_path / "final.mp4"
    output_path.write_bytes(b"mp4-bytes")
    monkeypatch.setattr(
        storage,
        "settings",
        replace(
            storage.settings,
            final_video_delivery_mode="s3",
            s3_bucket="ai-drama-media",
            s3_public_base_url="https://cdn.example.com/ai-drama-media",
        ),
    )
    monkeypatch.setattr(
        storage.S3FinalVideoDelivery,
        "_client",
        staticmethod(lambda: fake_client),
    )

    result = storage.S3FinalVideoDelivery().persist(
        project_id="project-1",
        final_video_id="final-1",
        source_path=output_path,
    )

    assert result.storage_key == "projects/project-1/final/final-1.mp4"
    assert result.public_url == "https://cdn.example.com/ai-drama-media/projects/project-1/final/final-1.mp4"
    assert fake_client.calls[0]["Bucket"] == "ai-drama-media"
    assert fake_client.calls[0]["Body"] == b"mp4-bytes"
    assert fake_client.calls[0]["ExtraArgs"] == {"ContentType": "video/mp4"}


def test_s3_source_video_storage_materializes_worker_temp_file(monkeypatch) -> None:
    """S3 源视频在 Worker 只落临时目录，退出上下文后由存储层自动清理。"""

    fake_client = _FakeS3Client()
    monkeypatch.setattr(
        storage,
        "settings",
        replace(storage.settings, source_video_storage_mode="s3", s3_bucket="ai-drama-media"),
    )
    monkeypatch.setattr(
        storage.S3SourceVideoStorage,
        "_client",
        staticmethod(lambda: fake_client),
    )

    source_storage = storage.S3SourceVideoStorage()
    with source_storage.source_video_file("projects/project-1/source/reference.mp4") as video_path:
        assert video_path.read_bytes() == b"source-video-bytes"
        assert video_path.suffix == ".mp4"
        temporary_path = video_path
    assert not temporary_path.exists()
    assert fake_client.calls[0]["Bucket"] == "ai-drama-media"
    assert fake_client.calls[0]["Key"] == "projects/project-1/source/reference.mp4"
