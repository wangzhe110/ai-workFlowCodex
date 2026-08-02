"""pytest 进程级隔离配置，必须早于 app 模块导入执行。"""

from pathlib import Path
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
