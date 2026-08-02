"""参考视频抽帧的纯函数规则测试，不依赖本机 FFmpeg。"""

from app.services.video_frame_service import _sample_timestamps


def test_sample_timestamps_are_uniform_and_avoid_zero_second() -> None:
    """抽帧位置均匀覆盖视频，避免仅分析可能黑场的第 0 秒。"""

    assert _sample_timestamps(12.0, 3) == [3.0, 6.0, 9.0]
