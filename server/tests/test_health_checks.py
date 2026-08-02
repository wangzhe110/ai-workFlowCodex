"""进程存活与生产依赖就绪探针的接口测试。"""

from fastapi.testclient import TestClient

from app.main import app


def test_liveness_and_readiness_checks_are_available() -> None:
    """本地 inline 模式下，存活检查不查依赖，就绪检查确认 SQLite 可用。"""

    with TestClient(app) as client:
        liveness = client.get("/health")
        readiness = client.get("/ready")

    assert liveness.status_code == 200
    assert liveness.json() == {"status": "ok", "service": "ai-drama-workflow-api"}
    assert readiness.status_code == 200
    assert readiness.json() == {
        "status": "ok",
        "service": "ai-drama-workflow-api",
        "dependencies": {"database": "ok"},
    }
