"""参考视频音轨提取服务。

语音转写接口通常只接收单独音频文件。本模块在后台 Worker 中把用户授权的参考
视频压缩为受时长、大小限制的单声道 MP3，并仅以内存字节交给转写适配器；临时
文件会在函数返回前自动删除，音频和转写原文都不写入数据库。
"""

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

from app.services.storage import asset_storage


@dataclass(frozen=True)
class ExtractedVideoAudio:
    """仅在当前 Worker 任务内存在的受限音频载荷。"""

    filename: str
    content_type: str
    data: bytes
    duration_seconds: float


def extract_reference_audio(
    storage_key: str,
    *,
    max_duration_seconds: int,
    timeout_seconds: float,
    max_audio_bytes: int,
) -> ExtractedVideoAudio:
    """提取受限 MP3，拒绝超长、空白或超出请求体预算的音频。

    只在启用了真实语音转写配置时调用。默认模拟流程不会依赖 FFmpeg，也不会读取
    原视频内容。``-t`` 让 V1 只取开头一段音频，符合“分析开头机制”而非复制全片
    语言内容的业务边界。
    """

    if not isinstance(max_duration_seconds, int) or not 5 <= max_duration_seconds <= 600:
        raise RuntimeError("audio_max_duration_seconds 必须是 5 至 600 的整数")
    if not isinstance(timeout_seconds, (int, float)) or not 5 <= float(timeout_seconds) <= 300:
        raise RuntimeError("audio_extraction_timeout_seconds 必须在 5 至 300 秒之间")
    if not isinstance(max_audio_bytes, int) or not 64 * 1024 <= max_audio_bytes <= 50 * 1024 * 1024:
        raise RuntimeError("audio_max_bytes 必须在 64KB 至 50MB 之间")
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("真实语音分析需要安装系统依赖：ffmpeg")

    with asset_storage.source_video_file(storage_key) as video_path:
        with TemporaryDirectory(prefix="ai-drama-audio-") as temporary_directory:
            output_path = Path(temporary_directory) / "reference-opening.mp3"
            _extract_mp3(
                video_path,
                output_path,
                max_duration_seconds=max_duration_seconds,
                timeout_seconds=float(timeout_seconds),
            )
            try:
                data = output_path.read_bytes()
            except OSError as exc:
                raise RuntimeError("FFmpeg 未生成可读取的音频文件") from exc
    if not data:
        raise RuntimeError("参考视频没有可用音轨，无法执行语音分析")
    if len(data) > max_audio_bytes:
        raise RuntimeError("提取音频超过 audio_max_bytes 限制")
    return ExtractedVideoAudio(
        filename="reference-opening.mp3",
        content_type="audio/mpeg",
        data=data,
        duration_seconds=float(max_duration_seconds),
    )


def _extract_mp3(
    video_path: Path,
    output_path: Path,
    *,
    max_duration_seconds: int,
    timeout_seconds: float,
) -> None:
    """固定编码为 16kHz 单声道 MP3，控制网络请求大小和 ASR 成本。"""

    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-vn",
                "-t",
                str(max_duration_seconds),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "64k",
                str(output_path),
            ],
            capture_output=True,
            check=True,
            timeout=timeout_seconds,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("FFmpeg 提取音轨失败；请确认视频包含可解码音频") from exc
