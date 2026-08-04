"""pytest 进程级隔离配置，必须早于 app 模块导入执行。"""

from pathlib import Path
from functools import lru_cache
from base64 import b64decode
import os
import shutil


RUNTIME_DIRECTORY = Path(__file__).parent / ".runtime"
shutil.rmtree(RUNTIME_DIRECTORY, ignore_errors=True)
RUNTIME_DIRECTORY.mkdir(parents=True, exist_ok=True)

# 配置模块在首次导入时读取环境变量，因此测试需在此处完成隔离。
os.environ["DATABASE_URL"] = f"sqlite:///{RUNTIME_DIRECTORY / 'test.db'}"
os.environ["LOCAL_STORAGE_PATH"] = str(RUNTIME_DIRECTORY / "assets")
os.environ["SIMULATED_STEP_DELAY_SECONDS"] = "0"
os.environ["TASK_EXECUTION_MODE"] = "inline"


@lru_cache(maxsize=1)
def real_video_bytes() -> bytes:
    """返回无版权、真实可解码的 H.264 MP4，而不是用文本冒充视频。

    夹具是仓库内无版权纯色 H.264 MP4 的 Base64 编码；测试运行时解码为字节，
    既能让 Git 差异可审阅，也不会把文本当作 `.mp4` 上传。
    """

    encoded = (Path(__file__).parent / "fixtures" / "real-video.mp4.base64").read_text(encoding="ascii")
    payload = b64decode(encoded)
    if len(payload) < 128:
        raise RuntimeError("真实 MP4 测试夹具生成失败")
    return payload
