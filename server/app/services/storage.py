"""资产存储与模型媒体转存适配层。

V1 开发阶段把源文件写入本地目录、直接使用图片供应商 URL。生产环境可以独立
启用 S3/MinIO 转存：图片生成完成后即复制到自己控制的对象存储，避免第三方短期
URL 失效导致后续图生视频无法读取首帧。业务服务只依赖本模块的标准接口。
"""

from contextlib import contextmanager
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from typing import Iterator, Optional, Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import UploadFile

from app.core.config import settings


class LocalAssetStorage:
    """本地开发文件存储实现，不可直接用于多实例生产部署。"""

    def save_source_video(self, project_id: str, upload: UploadFile) -> tuple[str, int]:
        """保存上传文件并返回稳定存储键和实际字节数。

        文件名只保留后缀，避免用户文件名被用作路径的一部分而造成路径穿越。
        """

        suffix = Path(upload.filename or "source-video").suffix.lower() or ".bin"
        storage_key = f"projects/{project_id}/source/{uuid4()}{suffix}"
        destination = settings.local_storage_path / storage_key
        destination.parent.mkdir(parents=True, exist_ok=True)

        byte_size = 0
        try:
            with destination.open("wb") as target:
                while chunk := upload.file.read(1024 * 1024):
                    byte_size += len(chunk)
                    if byte_size > settings.max_upload_bytes:
                        raise ValueError(f"视频文件超过 {settings.max_upload_bytes} 字节的上传限制")
                    target.write(chunk)
        except Exception:
            # 上传失败或超限时立即删除部分文件，避免对象存储产生无法关联的垃圾数据。
            destination.unlink(missing_ok=True)
            raise

        return storage_key, byte_size

    def source_video_path(self, storage_key: str) -> Path:
        """解析本地源视频的安全绝对路径，供 Worker 抽帧而非 HTTP 路由使用。

        存储键来自数据库，但仍须验证其最终路径位于配置根目录内，防止异常数据
        通过 ``..`` 访问工作区任意文件。S3/MinIO 的源视频读取实现接入后应保留
        同名能力或提供本地临时文件，而不能让工作流依赖存储细节。
        """

        root = settings.local_storage_path.resolve()
        candidate = (root / storage_key).resolve()
        if candidate != root and root not in candidate.parents:
            raise RuntimeError("素材存储键不在允许目录内")
        if not candidate.is_file():
            raise RuntimeError("参考视频文件不存在或无法读取")
        return candidate

    @contextmanager
    def source_video_file(self, storage_key: str) -> Iterator[Path]:
        """将本地素材以统一上下文接口提供给媒体 Worker，无需复制文件。"""

        yield self.source_video_path(storage_key)

    def save_final_video(self, project_id: str, final_video_id: str, source_path: Path) -> str:
        """原子保存 FFmpeg 合成的 MP4，并返回不暴露给浏览器的内部存储键。

        本地实现服务于开发和单机验收。生产可保持相同方法契约，替换为 S3/MinIO
        上传实现；完整成片服务无需了解具体介质存放位置。
        """

        if not source_path.is_file():
            raise RuntimeError("待保存的完整成片文件不存在")
        storage_key = f"projects/{project_id}/final/{final_video_id}.mp4"
        destination = settings.local_storage_path / storage_key
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_destination = destination.with_suffix(".mp4.tmp")
        try:
            with source_path.open("rb") as source, temporary_destination.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            temporary_destination.replace(destination)
        except Exception:
            temporary_destination.unlink(missing_ok=True)
            raise
        return storage_key

    def final_video_path(self, storage_key: str) -> Path:
        """解析完整成片的安全本地路径，供已验证项目范围的下载接口使用。"""

        return self.source_video_path(storage_key)


class SourceVideoStorage(Protocol):
    """参考视频的上传与 Worker 本地化访问契约。

    FFmpeg 只能读取本地路径，因此即使源素材位于对象存储，也应由该层负责临时
    下载并在上下文退出时清理；分析和转写服务绝不能自行假设存储实现。
    """

    def save_source_video(self, project_id: str, upload: UploadFile) -> tuple[str, int]:
        """保存上传源视频，返回内部键和实际字节数。"""

    def source_video_file(self, storage_key: str) -> Iterator[Path]:
        """在上下文内提供 Worker 可读取的本地视频文件路径。"""


class S3SourceVideoStorage:
    """生产源视频存储：上传至 S3/MinIO，Worker 按需下载临时副本。"""

    mode = "s3"

    def __init__(self) -> None:
        """只要求 S3 API Bucket；源视频不需要公开访问 URL。"""

        if not settings.s3_bucket:
            raise RuntimeError("参考视频 S3 存储需要配置 S3_BUCKET")
        self.bucket = settings.s3_bucket

    def save_source_video(self, project_id: str, upload: UploadFile) -> tuple[str, int]:
        """先受限写入短暂文件，再流式上传，避免把大视频读进内存。"""

        suffix = Path(upload.filename or "source-video").suffix.lower() or ".bin"
        storage_key = f"projects/{project_id}/source/{uuid4()}{suffix}"
        with TemporaryDirectory(prefix="ai-drama-source-upload-") as directory:
            temporary_path = Path(directory) / f"source{suffix}"
            byte_size = 0
            try:
                with temporary_path.open("wb") as target:
                    while chunk := upload.file.read(1024 * 1024):
                        byte_size += len(chunk)
                        if byte_size > settings.max_upload_bytes:
                            raise ValueError(f"视频文件超过 {settings.max_upload_bytes} 字节的上传限制")
                        target.write(chunk)
                with temporary_path.open("rb") as source:
                    self._client().upload_fileobj(
                        source,
                        self.bucket,
                        storage_key,
                        ExtraArgs={"ContentType": upload.content_type or "video/mp4"},
                    )
            except Exception:
                # 上传完成后数据库登记失败时会由上层显式处理；此处仅在本次上传
                # 已知异常时尽力清理远程孤儿对象，不向用户暴露 S3 凭据或原始错误。
                try:
                    self._client().delete_object(Bucket=self.bucket, Key=storage_key)
                except Exception:
                    pass
                raise
        return storage_key, byte_size

    @contextmanager
    def source_video_file(self, storage_key: str) -> Iterator[Path]:
        """下载 S3 对象至 Worker 临时目录，供 FFmpeg/FFprobe 使用后立即删除。"""

        if not isinstance(storage_key, str) or not storage_key.startswith("projects/"):
            raise RuntimeError("参考视频存储键格式无效")
        suffix = Path(storage_key).suffix.lower() or ".bin"
        with TemporaryDirectory(prefix="ai-drama-source-download-") as directory:
            temporary_path = Path(directory) / f"source{suffix}"
            try:
                self._client().download_file(self.bucket, storage_key, str(temporary_path))
            except Exception as exc:
                raise RuntimeError("无法从对象存储读取参考视频") from exc
            if not temporary_path.is_file() or not temporary_path.stat().st_size:
                raise RuntimeError("对象存储中的参考视频为空或不存在")
            if temporary_path.stat().st_size > settings.max_upload_bytes:
                raise RuntimeError("对象存储中的参考视频超过 MAX_UPLOAD_BYTES 限制")
            yield temporary_path

    @staticmethod
    def _client():
        """延迟创建 S3 客户端；本地模式无需安装或连接 boto3。"""

        return S3GeneratedImageDelivery._client()


class GeneratedImageDelivery(Protocol):
    """将模型输出图片变为可供后续视频模型使用的稳定地址。"""

    def persist(
        self,
        *,
        project_id: str,
        storyboard_package_id: str,
        shot_number: int,
        version: int,
        source_url: str,
    ) -> str:
        """返回可被视频供应商读取的图片 URL；不得返回或记录任何密钥。"""


class FinalVideoDeliveryResult:
    """成片持久化后的内部键与可选公开下载地址。"""

    def __init__(self, *, storage_key: str, public_url: Optional[str]) -> None:
        self.storage_key = storage_key
        self.public_url = public_url


class FinalVideoDelivery(Protocol):
    """将 FFmpeg 本地输出交付到最终媒体存储的可替换边界。"""

    def persist(self, *, project_id: str, final_video_id: str, source_path: Path) -> FinalVideoDeliveryResult:
        """持久化 MP4 并返回内部键；若有稳定公网地址，同时返回 public_url。"""


class LocalFinalVideoDelivery:
    """单机开发实现：保存到本地媒体目录，通过受控 API 下载。"""

    mode = "local"

    def persist(self, *, project_id: str, final_video_id: str, source_path: Path) -> FinalVideoDeliveryResult:
        """复用本地资产存储的原子写入，避免浏览器直接访问内部路径。"""

        return FinalVideoDeliveryResult(
            storage_key=local_asset_storage.save_final_video(project_id, final_video_id, source_path),
            public_url=None,
        )


class S3FinalVideoDelivery:
    """生产实现：流式上传成片到 S3/MinIO，并返回稳定 HTTPS CDN 地址。"""

    mode = "s3"

    def __init__(self) -> None:
        """复用图片转存所使用的 Bucket、CDN 与服务端凭据边界。"""

        if not settings.s3_bucket:
            raise RuntimeError("完整成片 S3 转存需要配置 S3_BUCKET")
        if not settings.s3_public_base_url or not settings.s3_public_base_url.startswith("https://"):
            raise RuntimeError("完整成片 S3 转存需要可公网访问的 HTTPS S3_PUBLIC_BASE_URL")
        self.bucket = settings.s3_bucket
        self.public_base_url = settings.s3_public_base_url.rstrip("/")

    def persist(self, *, project_id: str, final_video_id: str, source_path: Path) -> FinalVideoDeliveryResult:
        """流式上传已合成 MP4，不将潜在 GB 级成片额外读入 Python 内存。"""

        if not source_path.is_file():
            raise RuntimeError("待上传的完整成片文件不存在")
        object_key = f"projects/{project_id}/final/{final_video_id}.mp4"
        try:
            with source_path.open("rb") as source:
                self._client().upload_fileobj(
                    source,
                    self.bucket,
                    object_key,
                    ExtraArgs={"ContentType": "video/mp4"},
                )
        except Exception as exc:
            raise RuntimeError("无法将完整成片上传到对象存储") from exc
        return FinalVideoDeliveryResult(
            storage_key=object_key,
            public_url=f"{self.public_base_url}/{object_key}",
        )

    @staticmethod
    def _client():
        """与图片 S3 适配器复用惰性 boto3 导入与部署凭据读取。"""

        return S3GeneratedImageDelivery._client()


class DirectGeneratedImageDelivery:
    """本地开发默认实现：保持图片供应商返回的地址，不进行网络复制。"""

    mode = "direct"

    def persist(
        self,
        *,
        project_id: str,
        storyboard_package_id: str,
        shot_number: int,
        version: int,
        source_url: str,
    ) -> str:
        """直接返回 URL；参数保留是为了与 S3 实现维持一致的业务契约。"""

        return source_url


class S3GeneratedImageDelivery:
    """把远程图片结果复制到 S3/MinIO 兼容存储并返回公共 HTTPS 地址。

    S3 SDK 采用标准环境变量 ``AWS_ACCESS_KEY_ID``、``AWS_SECRET_ACCESS_KEY``
    和可选 ``AWS_DEFAULT_REGION`` 读取凭据；它们从不写入数据库、日志或 API。
    ``S3_PUBLIC_BASE_URL`` 必须是第三方视频模型可访问的 HTTPS CDN/网关地址。
    """

    mode = "s3"

    def __init__(self) -> None:
        """读取已注入的部署配置，并在缺项时及早给出明确错误。"""

        if not settings.s3_bucket:
            raise RuntimeError("S3 转存需要配置 S3_BUCKET")
        if not settings.s3_public_base_url or not settings.s3_public_base_url.startswith("https://"):
            raise RuntimeError("S3 转存需要可公网访问的 HTTPS S3_PUBLIC_BASE_URL")
        self.bucket = settings.s3_bucket
        self.public_base_url = settings.s3_public_base_url.rstrip("/")

    def persist(
        self,
        *,
        project_id: str,
        storyboard_package_id: str,
        shot_number: int,
        version: int,
        source_url: str,
    ) -> str:
        """下载一次模型图片，受大小和类型限制后上传为不可冲突的新对象。"""

        if not isinstance(source_url, str) or not source_url.startswith("https://"):
            raise RuntimeError("S3 转存只接受图片供应商返回的 HTTPS 地址")
        data, content_type = self._download_image(source_url)
        suffix = self._suffix(source_url, content_type)
        object_key = (
            f"projects/{project_id}/storyboards/{storyboard_package_id}/"
            f"shots/{shot_number}/v{version}-{uuid4()}{suffix}"
        )
        self._client().put_object(
            Bucket=self.bucket,
            Key=object_key,
            Body=data,
            ContentType=content_type,
        )
        return f"{self.public_base_url}/{object_key}"

    def _download_image(self, source_url: str) -> tuple[bytes, str]:
        """读取图片响应，限制下载字节数并拒绝非图片 Content-Type。"""

        timeout = min(max(settings.generated_image_download_timeout_seconds, 1), 300)
        request = Request(source_url, headers={"Accept": "image/*"}, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                raw_content_type = response.headers.get_content_type()
                if not raw_content_type.startswith("image/"):
                    raise RuntimeError("图片供应商响应不是 image/* 类型，已拒绝转存")
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > settings.generated_image_max_bytes:
                    raise RuntimeError("模型图片超过 GENERATED_IMAGE_MAX_BYTES 限制")
                data = response.read(settings.generated_image_max_bytes + 1)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("无法下载模型图片以转存到对象存储") from exc
        if len(data) > settings.generated_image_max_bytes:
            raise RuntimeError("模型图片超过 GENERATED_IMAGE_MAX_BYTES 限制")
        if not data:
            raise RuntimeError("模型图片响应为空，无法转存")
        return data, raw_content_type

    @staticmethod
    def _suffix(source_url: str, content_type: str) -> str:
        """优先保留安全图片后缀，缺失时依据 Content-Type 推断。"""

        suffix = Path(urlparse(source_url).path).suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            return suffix
        return {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(content_type, ".img")

    @staticmethod
    def _client():
        """按需导入 boto3，确保未启用 S3 的本地开发不需要连接对象存储。"""

        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("S3 转存依赖 boto3；请按 server/requirements.txt 安装依赖") from exc
        return boto3.client("s3", endpoint_url=settings.s3_endpoint_url)


def build_generated_image_delivery() -> GeneratedImageDelivery:
    """按部署配置创建唯一的图片交付适配器，未知模式直接失败而不降级丢数据。"""

    if settings.generated_image_delivery_mode == "direct":
        return DirectGeneratedImageDelivery()
    if settings.generated_image_delivery_mode == "s3":
        return S3GeneratedImageDelivery()
    raise RuntimeError("GENERATED_IMAGE_DELIVERY_MODE 仅支持 direct 或 s3")


def build_source_video_storage() -> SourceVideoStorage:
    """选择参考视频存储实现；未知模式必须在启动时明确失败。"""

    if settings.source_video_storage_mode == "local":
        return local_asset_storage
    if settings.source_video_storage_mode == "s3":
        return S3SourceVideoStorage()
    raise RuntimeError("SOURCE_VIDEO_STORAGE_MODE 仅支持 local 或 s3")


def build_final_video_delivery() -> FinalVideoDelivery:
    """按环境构造成片交付方式；未知模式不得静默回退本地磁盘。"""

    if settings.final_video_delivery_mode == "local":
        return LocalFinalVideoDelivery()
    if settings.final_video_delivery_mode == "s3":
        return S3FinalVideoDelivery()
    raise RuntimeError("FINAL_VIDEO_DELIVERY_MODE 仅支持 local 或 s3")


local_asset_storage = LocalAssetStorage()
asset_storage = build_source_video_storage()
generated_image_delivery = build_generated_image_delivery()
final_video_delivery = build_final_video_delivery()
