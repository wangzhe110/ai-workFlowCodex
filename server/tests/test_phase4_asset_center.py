"""Phase 4 资产中心、跨项目引用与结构化导演分镜回归测试。"""

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import SessionLocal
from app.main import app
from app.models import (
    CharacterAsset,
    CharacterDefinition,
    ProjectCharacterAssetReference,
    ProjectSceneAssetReference,
    SceneAsset,
    VideoClipAssetBinding,
)
from conftest import real_video_bytes


def _run(client: TestClient, project_id: str, key: str) -> None:
    payload: dict[str, str] = {}
    if key == "reference_analysis":
        project = client.get(f"/api/v1/projects/{project_id}").json()
        payload["source_asset_id"] = next(asset["id"] for asset in project["assets"] if asset["kind"] == "SOURCE_VIDEO")
    response = client.post(f"/api/v1/production/projects/{project_id}/generation-runs/{key}", json=payload)
    assert response.status_code == 202, response.text


def _project_at_character_assets(client: TestClient, title: str) -> str:
    project_id = client.post("/api/v1/projects", json={"title": title}).json()["id"]
    upload = client.post(
        f"/api/v1/projects/{project_id}/source-video",
        files={"file": ("reference.mp4", real_video_bytes(), "video/mp4")},
    )
    assert upload.status_code == 201
    _run(client, project_id, "reference_analysis")
    analysis = client.get(f"/api/v1/production/projects/{project_id}/reference-analyses").json()[0]
    assert client.post(f"/api/v1/production/reference-analyses/{analysis['id']}/lock", json={}).status_code == 200
    _run(client, project_id, "story_generation")
    proposal = client.get(f"/api/v1/production/projects/{project_id}/story-proposals").json()[0]
    assert client.post(f"/api/v1/production/story-proposals/{proposal['id']}/select", json={}).status_code == 200
    _run(client, project_id, "character_design")
    return project_id


def test_asset_center_appends_versions_without_overwriting_existing_data() -> None:
    """角色和场景资产支持多参考图与 v2 追加，v1 内容保持原样。"""

    suffix = uuid4().hex[:8]
    with TestClient(app) as client:
        character = client.post(
            "/api/v1/asset-library/characters",
            json={
                "name": f"资产中心角色 {suffix}",
                "description": "用于跨项目复用的女主角",
                "age": "29 岁",
                "gender": "女",
                "personality": "克制但坚定",
                "style": "写实都市短剧",
                "appearance": "短发，清晰轮廓",
                "costume": "深色风衣",
                "reference_images": [
                    {"view": "front", "url": "https://example.invalid/character-front.png"},
                    {"view": "side", "url": "https://example.invalid/character-side.png"},
                    {"view": "full_body", "url": "https://example.invalid/character-full.png"},
                    {"view": "expression", "url": "https://example.invalid/character-expression.png"},
                ],
            },
        )
        assert character.status_code == 201, character.text
        assert len(character.json()["versions"][0]["reference_images"]) == 4
        character_id = character.json()["id"]
        original_description = character.json()["versions"][0]["description"]
        v2 = client.post(
            f"/api/v1/asset-library/characters/{character_id}/versions",
            json={
                "description": "雨夜剧情版本，保留人物识别特征",
                "style": "写实雨夜短剧",
                "reference_images": [{"view": "full_body", "url": "https://example.invalid/character-rain.png"}],
            },
        )
        assert v2.status_code == 201, v2.text
        assert v2.json()["version"] == 2

        scene = client.post(
            "/api/v1/asset-library/scenes",
            json={
                "name": f"资产中心场景 {suffix}",
                "description": "可重复使用的医院走廊",
                "style": "冷色写实",
                "weather": "小雨",
                "time_of_day": "夜晚",
                "location": "城市医院",
                "environment": "空旷走廊与冷白灯",
                "mood": "压抑紧张",
                "reference_images": [{"view": "wide", "url": "https://example.invalid/hospital-wide.png"}],
            },
        )
        assert scene.status_code == 201, scene.text
        scene_v2 = client.post(
            f"/api/v1/asset-library/scenes/{scene.json()['id']}/versions",
            json={"description": "黎明版本", "time_of_day": "黎明", "reference_images": []},
        )
        assert scene_v2.status_code == 201
        assert scene_v2.json()["version"] == 2

        listed = client.get("/api/v1/asset-library/characters")
        assert listed.status_code == 200
        listed_asset = next(item for item in listed.json() if item["id"] == character_id)
        assert [item["version"] for item in listed_asset["versions"]] == [2, 1]
        assert next(item for item in listed_asset["versions"] if item["version"] == 1)["description"] == original_description


def test_asset_center_version_can_be_adopted_by_multiple_projects_through_review_gate() -> None:
    """同一角色版本可被两个项目采用，但每个项目仍形成自己的待锁图和引用审计。"""

    suffix = uuid4().hex[:8]
    with TestClient(app) as client:
        asset_response = client.post(
            "/api/v1/asset-library/characters",
            json={
                "name": f"共享角色 {suffix}",
                "description": "跨项目共用角色",
                "reference_images": [{"view": "full_body", "url": "https://example.invalid/shared-character.png"}],
            },
        )
        assert asset_response.status_code == 201
        version_id = asset_response.json()["versions"][0]["id"]

        first_project = _project_at_character_assets(client, f"采用角色一 {suffix}")
        second_project = _project_at_character_assets(client, f"采用角色二 {suffix}")
        db = SessionLocal()
        try:
            first_character = db.scalars(select(CharacterDefinition).where(CharacterDefinition.project_id == first_project)).first()
            second_character = db.scalars(select(CharacterDefinition).where(CharacterDefinition.project_id == second_project)).first()
            assert first_character and second_character
            first_character_id, second_character_id = first_character.id, second_character.id
        finally:
            db.close()

        for project_id, character_id in ((first_project, first_character_id), (second_project, second_character_id)):
            adopted = client.post(
                f"/api/v1/production/projects/{project_id}/characters/{character_id}/asset-versions/{version_id}/adopt"
            )
            assert adopted.status_code == 201, adopted.text
            assert adopted.json()["asset_version_id"] == version_id
            locked = client.post(f"/api/v1/production/character-reference-images/{adopted.json()['id']}/lock", json={})
            assert locked.status_code == 200, locked.text

        db = SessionLocal()
        try:
            references = db.scalars(
                select(ProjectCharacterAssetReference).where(
                    ProjectCharacterAssetReference.character_asset_version_id == version_id,
                    ProjectCharacterAssetReference.is_selected.is_(True),
                )
            ).all()
            assert {item.project_id for item in references} >= {first_project, second_project}
        finally:
            db.close()


def test_production_freezes_asset_versions_and_exposes_structured_director_shots() -> None:
    """项目锁图后，导演/关键帧/视频均能追溯资产版本，且导演镜头字段完整。"""

    with TestClient(app) as client:
        project_id = _project_at_character_assets(client, "结构化导演分镜")
        _run(client, project_id, "character_images")
        for image in client.get(f"/api/v1/production/projects/{project_id}/character-reference-images").json():
            assert image["asset_version_id"]
            assert client.post(f"/api/v1/production/character-reference-images/{image['id']}/lock", json={}).status_code == 200
        _run(client, project_id, "scene_design")
        _run(client, project_id, "scene_images")
        for image in client.get(f"/api/v1/production/projects/{project_id}/scene-reference-images").json():
            assert image["asset_version_id"]
            assert client.post(f"/api/v1/production/scene-reference-images/{image['id']}/lock", json={}).status_code == 200

        _run(client, project_id, "director_plan")
        director = client.get(f"/api/v1/production/projects/{project_id}/director-plan")
        assert director.status_code == 200, director.text
        payload = director.json()
        assert payload and len(payload["shots"]) == 3
        for shot in payload["shots"]:
            assert shot["character_asset_version_ids"]
            assert shot["scene_asset_version_id"]
            assert all(shot[key] for key in ("action", "emotion", "camera_type", "camera_move", "lighting", "image_prompt", "video_prompt", "sound_prompt"))

        _run(client, project_id, "shot_keyframes")
        frames = client.get(f"/api/v1/production/projects/{project_id}/shot-keyframes").json()
        assert frames and all(frame["input_asset_snapshot"]["character_asset_version_ids"] for frame in frames)
        for frame in frames:
            assert client.post(f"/api/v1/production/shot-keyframes/{frame['id']}/lock", json={}).status_code == 200
        _run(client, project_id, "video_generation")

        db = SessionLocal()
        try:
            assert db.scalars(select(CharacterAsset).where(CharacterAsset.id.is_not(None))).first() is not None
            assert db.scalars(select(SceneAsset).where(SceneAsset.id.is_not(None))).first() is not None
            assert db.scalars(select(ProjectCharacterAssetReference).where(ProjectCharacterAssetReference.project_id == project_id)).first() is not None
            assert db.scalars(select(ProjectSceneAssetReference).where(ProjectSceneAssetReference.project_id == project_id)).first() is not None
            bindings = db.scalars(
                select(VideoClipAssetBinding).where(
                    VideoClipAssetBinding.character_asset_version_id.is_not(None)
                )
            ).all()
            assert bindings
        finally:
            db.close()
