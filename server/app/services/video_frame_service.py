"""参考视频抽帧服务。

视觉模型通常无法直接读取平台本地的原视频文件。本模块以 FFmpeg 在 Worker 中抽取
少量均匀分布的 JPEG 帧，并返回仅供本次模型调用的 data URL；帧数据不会写入数据
库或返回前端。源视频存储层可以是本地或 S3/MinIO，后者会在 Worker 内临时下载。
"""

from base64 import b64encode
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

from app.services.storage import asset_storage


@dataclass(frozen=True)
class SampledVideoFrame:
    """一帧图片及其在原视频中的时间位置，供视觉适配器组装多模态请求。"""

    timestamp_seconds: float
    data_url: str


def extract_sampled_video_frames(
    storage_key: str,
    *,
    frame_count: int,
    timeout_seconds: float,
    max_frame_bytes: int,
) -> list[SampledVideoFrame]:
    """从本地视频均匀抽取受尺寸/字节预算限制的 JPEG 帧。

    该函数只应在启用了真实视觉模型的后台 Worker 执行。默认模拟分析不调用它，
    因而本地开发不安装 FFmpeg 仍可完成完整流程演示。
    """

    if not isinstance(frame_count, int) or not 1 <= frame_count <= 12:
        raise RuntimeError("frame_sample_count 必须是 1 至 12 的整数")
    if not isinstance(timeout_seconds, (int, float)) or not 5 <= float(timeout_seconds) <= 300:
        raise RuntimeError("frame_extraction_timeout_seconds 必须在 5 至 300 秒之间")
    if not isinstance(max_frame_bytes, int) or not 64 * 1024 <= max_frame_bytes <= 8 * 1024 * 1024:
        raise RuntimeError("frame_max_bytes 必须在 64KB 至 8MB 之间")
    _require_ffmpeg()
    frames: list[SampledVideoFrame] = []
    with asset_storage.source_video_file(storage_key) as video_path:
        duration_seconds = _video_duration_seconds(video_path, float(timeout_seconds))
        timestamps = _sample_timestamps(duration_seconds, frame_count)
        with TemporaryDirectory(prefix="ai-drama-frames-") as temporary_directory:
            temporary_path = Path(temporary_directory)
            for position, timestamp in enumerate(timestamps, start=1):
                output_path = temporary_path / f"frame-{position}.jpg"
                _extract_one_frame(video_path, output_path, timestamp, float(timeout_seconds))
                try:
                    image_bytes = output_path.read_bytes()
                except OSError as exc:
                    raise RuntimeError("FFmpeg 未生成可读取的抽帧图片") from exc
                if not image_bytes:
                    raise RuntimeError("FFmpeg 生成了空白抽帧图片")
                if len(image_bytes) > max_frame_bytes:
                    raise RuntimeError("抽帧图片超过 frame_max_bytes 限制；请降低分辨率或调整配置")
                frames.append(
                    SampledVideoFrame(
                        timestamp_seconds=round(timestamp, 3),
                        data_url="data:image/jpeg;base64," + b64encode(image_bytes).decode("ascii"),
                    )
                )
    return frames


def _require_ffmpeg() -> None:
    """在开始任务前确认 FFmpeg/FFprobe 已部署，避免抛出难读的系统异常。"""

    missing = [binary for binary in ("ffmpeg", "ffprobe") if shutil.which(binary) is None]
    if missing:
        raise RuntimeError(f"真实视频分析需要安装系统依赖：{', '.join(missing)}")


def _video_duration_seconds(video_path: Path, timeout_seconds: float) -> float:
    """通过 FFprobe 获取时长，避免以文件大小或固定秒数猜测抽帧位置。"""

    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=timeout_seconds,
        )
        duration = float(completed.stdout.strip())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
        raise RuntimeError("无法读取参考视频时长；请确认上传的是可解码视频") from exc
    if duration <= 0:
        raise RuntimeError("参考视频时长无效，无法抽帧")
    return duration


def _sample_timestamps(duration_seconds: float, frame_count: int) -> list[float]:
    """按视频时长均匀选择帧，避开第 0 秒可能出现的黑场或编码关键帧问题。"""

    return [duration_seconds * position / (frame_count + 1) for position in range(1, frame_count + 1)]


def _extract_one_frame(
    video_path: Path,
    output_path: Path,
    timestamp_seconds: float,
    timeout_seconds: float,
) -> None:
    """以单帧 768px 宽 JPEG 方式调用 FFmpeg，避免 4K 原帧撑大视觉请求。"""

    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{timestamp_seconds:.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                # 统一限制宽度；视觉模型不需要完整 4K 原帧，较小的图片能显著降低
                # 中转请求体大小和模型计费，同时保留短剧构图与镜头功能所需信息。
                "-vf",
                "scale=768:-2",
                "-q:v",
                "3",
                str(output_path),
            ],
            capture_output=True,
            check=True,
            timeout=timeout_seconds,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("FFmpeg 抽帧失败；请确认视频编码和系统 FFmpeg 可用") from exc
