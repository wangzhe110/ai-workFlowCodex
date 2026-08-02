"""创作资产库的 API 回归测试。"""

from fastapi.testclient import TestClient

from app.main import app


def test_library_item_can_be_created_updated_and_soft_deactivated() -> None:
    """验证资产库支持人工维护，且停用不等于物理删除。"""

    with TestClient(app) as client:
        create_response = client.post(
            "/api/v1/creative-library",
            json={
                "kind": "VIRAL_ELEMENT",
                "title": "目标与阻碍同屏",
                "content": "在开场明确角色马上要完成的目标，再给出无法绕开的阻碍。",
                "group_name": "开场冲突",
                "tags": ["冲突", "开场", "冲突"],
            },
        )
        assert create_response.status_code == 201
        item = create_response.json()
        assert item["tags"] == ["冲突", "开场"]

        update_response = client.patch(
            f"/api/v1/creative-library/{item['id']}",
            json={
                "kind": "VIRAL_ELEMENT",
                "title": "目标与阻碍同屏（更新）",
                "content": "用原创角色目标制造第一处紧张关系。",
                "group_name": "开场冲突",
                "tags": ["原创", "冲突"],
            },
        )
        assert update_response.status_code == 200
        assert update_response.json()["title"].endswith("更新）")

        list_response = client.get("/api/v1/creative-library?kind=VIRAL_ELEMENT")
        assert list_response.status_code == 200
        assert any(row["id"] == item["id"] for row in list_response.json())

        deactivate_response = client.delete(f"/api/v1/creative-library/{item['id']}")
        assert deactivate_response.status_code == 204
        active_list_response = client.get("/api/v1/creative-library?kind=VIRAL_ELEMENT")
        assert all(row["id"] != item["id"] for row in active_list_response.json())
        full_list_response = client.get("/api/v1/creative-library?kind=VIRAL_ELEMENT&active_only=false")
        assert any(row["id"] == item["id"] and not row["is_active"] for row in full_list_response.json())
