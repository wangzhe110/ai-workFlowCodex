"""跨项目角色/场景资产中心接口。

资产版本只允许通过 POST 追加。项目内的锁图与主工作流仍由 production 路由负责，
这里不允许绕过审核直接改变项目的当前采用资产。
"""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import CharacterAsset, CharacterAssetVersion, SceneAsset, SceneAssetVersion
from app.schemas import (
    CharacterAssetCreateRequest,
    CharacterAssetResponse,
    CharacterAssetVersionCreateRequest,
    CharacterAssetVersionResponse,
    SceneAssetCreateRequest,
    SceneAssetResponse,
    SceneAssetVersionCreateRequest,
    SceneAssetVersionResponse,
)
from app.services.asset_library_service import (
    append_character_asset_version,
    append_scene_asset_version,
    create_character_asset,
    create_scene_asset,
)


router = APIRouter(prefix="/api/v1/asset-library", tags=["资产中心"])


def _character_version_response(item: CharacterAssetVersion) -> CharacterAssetVersionResponse:
    return CharacterAssetVersionResponse(
        id=item.id,
        character_asset_id=item.character_asset_id,
        version=item.version,
        description=item.description,
        age=item.age,
        gender=item.gender,
        personality=item.personality,
        style=item.style,
        appearance=item.appearance,
        costume=item.costume,
        reference_images=item.reference_images,
        created_at=item.created_at,
    )


def _scene_version_response(item: SceneAssetVersion) -> SceneAssetVersionResponse:
    return SceneAssetVersionResponse(
        id=item.id,
        scene_asset_id=item.scene_asset_id,
        version=item.version,
        description=item.description,
        style=item.style,
        weather=item.weather,
        time_of_day=item.time_of_day,
        location=item.location,
        environment=item.environment,
        mood=item.mood,
        reference_images=item.reference_images,
        created_at=item.created_at,
    )


def _character_asset_response(item: CharacterAsset, versions: list[CharacterAssetVersion]) -> CharacterAssetResponse:
    return CharacterAssetResponse(
        id=item.id,
        library_id=item.library_id,
        name=item.name,
        description=item.description,
        status=item.status,
        versions=[_character_version_response(version) for version in versions],
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _scene_asset_response(item: SceneAsset, versions: list[SceneAssetVersion]) -> SceneAssetResponse:
    return SceneAssetResponse(
        id=item.id,
        library_id=item.library_id,
        name=item.name,
        description=item.description,
        status=item.status,
        versions=[_scene_version_response(version) for version in versions],
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.get("/characters", response_model=list[CharacterAssetResponse])
def list_character_assets(db: Session = Depends(get_db)) -> list[CharacterAssetResponse]:
    """列出全部角色资产和版本，便于在不同项目间挑选已经验收的参考资产。"""

    assets = list(db.scalars(select(CharacterAsset).order_by(CharacterAsset.updated_at.desc())).all())
    versions_by_asset: dict[str, list[CharacterAssetVersion]] = defaultdict(list)
    for version in db.scalars(
        select(CharacterAssetVersion).order_by(CharacterAssetVersion.character_asset_id, CharacterAssetVersion.version.desc())
    ).all():
        versions_by_asset[version.character_asset_id].append(version)
    return [_character_asset_response(item, versions_by_asset[item.id]) for item in assets]


@router.post("/characters", response_model=CharacterAssetResponse, status_code=status.HTTP_201_CREATED)
def create_character_asset_endpoint(
    payload: CharacterAssetCreateRequest, db: Session = Depends(get_db)
) -> CharacterAssetResponse:
    """手动建立可复用角色；可一次提交正面、侧面、全身和表情参考图。"""

    asset, version = create_character_asset(
        db,
        name=payload.name,
        description=payload.description,
        age=payload.age,
        gender=payload.gender,
        personality=payload.personality,
        style=payload.style,
        appearance=payload.appearance,
        costume=payload.costume,
        reference_images=[item.model_dump() for item in payload.reference_images],
        library_id=payload.library_id,
    )
    return _character_asset_response(asset, [version])


@router.post("/characters/{character_asset_id}/versions", response_model=CharacterAssetVersionResponse, status_code=status.HTTP_201_CREATED)
def append_character_asset_version_endpoint(
    character_asset_id: str,
    payload: CharacterAssetVersionCreateRequest,
    db: Session = Depends(get_db),
) -> CharacterAssetVersionResponse:
    """以新版本更新角色设定或参考图，不允许 PATCH 覆盖历史版本。"""

    version = append_character_asset_version(
        db,
        character_asset_id=character_asset_id,
        description=payload.description,
        age=payload.age,
        gender=payload.gender,
        personality=payload.personality,
        style=payload.style,
        appearance=payload.appearance,
        costume=payload.costume,
        reference_images=[item.model_dump() for item in payload.reference_images],
    )
    return _character_version_response(version)


@router.get("/scenes", response_model=list[SceneAssetResponse])
def list_scene_assets(db: Session = Depends(get_db)) -> list[SceneAssetResponse]:
    """列出场景资产和版本，覆盖农村、城市、办公室、医院、古代、未来等复用场景。"""

    assets = list(db.scalars(select(SceneAsset).order_by(SceneAsset.updated_at.desc())).all())
    versions_by_asset: dict[str, list[SceneAssetVersion]] = defaultdict(list)
    for version in db.scalars(
        select(SceneAssetVersion).order_by(SceneAssetVersion.scene_asset_id, SceneAssetVersion.version.desc())
    ).all():
        versions_by_asset[version.scene_asset_id].append(version)
    return [_scene_asset_response(item, versions_by_asset[item.id]) for item in assets]


@router.post("/scenes", response_model=SceneAssetResponse, status_code=status.HTTP_201_CREATED)
def create_scene_asset_endpoint(payload: SceneAssetCreateRequest, db: Session = Depends(get_db)) -> SceneAssetResponse:
    """手动建立一个可跨项目使用的场景资产首版。"""

    asset, version = create_scene_asset(
        db,
        name=payload.name,
        description=payload.description,
        style=payload.style,
        weather=payload.weather,
        time_of_day=payload.time_of_day,
        location=payload.location,
        environment=payload.environment,
        mood=payload.mood,
        reference_images=[item.model_dump() for item in payload.reference_images],
        library_id=payload.library_id,
    )
    return _scene_asset_response(asset, [version])


@router.post("/scenes/{scene_asset_id}/versions", response_model=SceneAssetVersionResponse, status_code=status.HTTP_201_CREATED)
def append_scene_asset_version_endpoint(
    scene_asset_id: str,
    payload: SceneAssetVersionCreateRequest,
    db: Session = Depends(get_db),
) -> SceneAssetVersionResponse:
    """以追加版本的方式更新场景风格、天气、时段或参考图。"""

    version = append_scene_asset_version(
        db,
        scene_asset_id=scene_asset_id,
        description=payload.description,
        style=payload.style,
        weather=payload.weather,
        time_of_day=payload.time_of_day,
        location=payload.location,
        environment=payload.environment,
        mood=payload.mood,
        reference_images=[item.model_dump() for item in payload.reference_images],
    )
    return _scene_version_response(version)
