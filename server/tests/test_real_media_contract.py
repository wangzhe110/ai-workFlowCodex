"""真实可解码媒体的生产前回归契约。

夹具为由开源 FFmpeg 编码的无版权纯色 H.264 画面；真正的 ffprobe/ffmpeg 断言
只在部署了 FFmpeg 的环境执行。本机缺少该系统依赖时会明确 skip，而不是用文本
伪装成 MP4 通过测试。
"""

from pathlib import Path
from base64 import b64decode
import shutil
import subprocess
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services.video_frame_service import extract_sampled_video_frames


FIXTURE_BASE64 = Path(__file__).parent / "fixtures" / "real-video.mp4.base64"
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _real_mp4(tmp_path: Path, name: str = "real.mp4") -> Path:
    """解码一段真实 H.264 MP4；不依赖网络、版权素材或第三方模型。"""

    target = tmp_path / name
    target.write_bytes(b64decode(FIXTURE_BASE64.read_text(encoding="ascii")))
    assert target.is_file() and target.stat().st_size > 0
    return target


def test_upload_rejects_mime_and_extension_forgery() -> None:
    """客户端伪造 MIME 或扩展名时必须在写入存储前被拒绝。"""

    with TestClient(app) as client:
        project_id = client.post("/api/v1/projects", json={"title": "媒体类型校验"}).json()["id"]
        wrong_mime = client.post(
            f"/api/v1/projects/{project_id}/source-video",
            files={"file": ("video.mp4", b"not-a-video", "application/octet-stream")},
        )
        wrong_suffix = client.post(
            f"/api/v1/projects/{project_id}/source-video",
            files={"file": ("video.txt", b"not-a-video", "video/mp4")},
        )
        assert wrong_mime.status_code == 415
        assert wrong_suffix.status_code == 415


def test_upload_enforces_maximum_file_size(monkeypatch) -> None:
    """超过上限时 LocalAssetStorage 会清理部分文件且接口返回 413。"""

    monkeypatch.setattr("app.services.storage.settings", replace(settings, max_upload_bytes=8))
    with TestClient(app) as client:
        project_id = client.post("/api/v1/projects", json={"title": "媒体大小校验"}).json()["id"]
        response = client.post(
            f"/api/v1/projects/{project_id}/source-video",
            files={"file": ("large.mp4", b"0123456789", "video/mp4")},
        )
        assert response.status_code == 413
        source_dir = settings.local_storage_path / "projects" / project_id / "source"
        assert not source_dir.exists() or not list(source_dir.iterdir())


@pytest.mark.skipif(not HAS_FFMPEG, reason="需要系统 FFmpeg/FFprobe；当前环境未安装")
def test_real_mp4_can_be_probed_framed_and_reencoded(tmp_path: Path) -> None:
    """真实夹具必须可读时长、可抽帧、且两段可重新编码拼接。"""

    first = _real_mp4(tmp_path, "first.mp4")
    second = _real_mp4(tmp_path, "second.mp4")
    duration = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(first)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert float(duration.stdout.strip()) > 0
    frame = tmp_path / "frame.jpg"
    subprocess.run(["ffmpeg", "-y", "-i", str(first), "-frames:v", "1", str(frame)], check=True, capture_output=True)
    assert frame.is_file() and frame.stat().st_size > 0
    manifest = tmp_path / "concat.txt"
    manifest.write_text(f"file '{first}'\nfile '{second}'\n", encoding="utf-8")
    output = tmp_path / "joined.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(manifest), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output)],
        check=True,
        capture_output=True,
    )
    assert output.is_file() and output.stat().st_size > 0


@pytest.mark.skipif(not HAS_FFMPEG, reason="需要系统 FFmpeg/FFprobe；当前环境未安装")
def test_corrupted_media_returns_clear_decode_error_and_temp_frames_are_cleaned(tmp_path: Path) -> None:
    """损坏媒体不能伪装成功；正常抽帧结束后临时目录不能残留。"""

    storage_key = "projects/media-test/source/corrupt.mp4"
    destination = settings.local_storage_path / storage_key
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"\x00\x00\x00\x18ftypisomcorrupt")
    with pytest.raises(RuntimeError, match="可解码视频|视频时长|FFmpeg"):
        extract_sampled_video_frames(storage_key, frame_count=1, timeout_seconds=5, max_frame_bytes=128 * 1024)
    assert not list(Path("/tmp").glob("ai-drama-frames-*"))
