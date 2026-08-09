"""Phase 4 资产中心的版本化服务。

此模块只管理可跨项目复用的角色/场景资产及项目引用。它不调用模型、不改变 V1 的
Workflow 状态，也不覆盖任何版本：模型生成的项目参考图在人工锁定后会被物化为一份
资产中心版本，后续导演、关键帧、视频任务冻结该版本快照。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterable

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AssetLibrary,
    AssetLibraryKind,
    CharacterAsset,
    CharacterAssetVersion,
    CharacterDefinition,
    CharacterReferenceImage,
    ProjectCharacterAssetReference,
    ProjectSceneAssetReference,
    SceneAsset,
    SceneAssetVersion,
    SceneDefinition,
    SceneReferenceImage,
    RunStatus,
    WorkflowRun,
    WorkflowStep,
)


DEFAULT_CHARACTER_LIBRARY_NAME = "LemonFlow 角色资产库"
DEFAULT_SCENE_LIBRARY_NAME = "LemonFlow 场景资产库"


def utcnow() -> datetime:
    """资产和项目锁定时间统一保存为 UTC。"""

    return datetime.now(timezone.utc)


def _not_found(label: str) -> None:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{label}不存在")


def normalize_reference_images(value: Iterable[dict[str, Any]] | None) -> list[dict[str, str]]:
    """校验多视图参考图的最小安全结构，不接受二进制或任何浏览器执行内容。"""

    allowed_views = {"front", "side", "full_body", "expression", "wide", "detail", "generated"}
    normalized: list[dict[str, str]] = []
    for item in value or []:
        if not isinstance(item, dict):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="参考图必须是对象数组")
        raw_url = item.get("url")
        if not isinstance(raw_url, str) or not raw_url.strip() or len(raw_url.strip()) > 4000:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="参考图 URL 无效")
        # 资产中心会在浏览器中提供“打开参考图”链接，必须拒绝 javascript: 等可执行
        # 协议。mock:// 只用于本地无密钥闭环，不会在生产环境由浏览器实际加载。
        if not raw_url.strip().startswith(("https://", "http://", "mock://")):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="参考图 URL 仅支持 http、https 或本地 mock 协议",
            )
        view = str(item.get("view") or "generated").strip().lower()
        if view not in allowed_views:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="参考图视角不受支持")
        row = {"view": view, "url": raw_url.strip()}
        label = item.get("label")
        if isinstance(label, str) and label.strip():
            row["label"] = label.strip()[:160]
        normalized.append(row)
    if len(normalized) > 16:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="每个资产版本最多保存 16 张参考图")
    return normalized


def get_or_create_default_library(db: Session, kind: AssetLibraryKind) -> AssetLibrary:
    """获取 V1 默认资料库；重复调用只读取，不覆盖名称或描述。"""

    name = DEFAULT_CHARACTER_LIBRARY_NAME if kind == AssetLibraryKind.CHARACTER else DEFAULT_SCENE_LIBRARY_NAME
    library = db.scalars(
        select(AssetLibrary).where(AssetLibrary.kind == kind, AssetLibrary.name == name)
    ).first()
    if library is None:
        library = AssetLibrary(
            kind=kind,
            name=name,
            description="由 LemonFlow V1 自动沉淀的可跨项目复用生产资产。",
            status="ACTIVE",
        )
        db.add(library)
        db.flush()
    return library


def _next_version(db: Session, entity_type, foreign_field, asset_id: str) -> int:
    return int(db.scalar(select(func.max(entity_type.version)).where(foreign_field == asset_id)) or 0) + 1


def create_character_asset(
    db: Session,
    *,
    name: str,
    description: str,
    age: str | None,
    gender: str | None,
    personality: str | None,
    style: str | None,
    appearance: str | None,
    costume: str | None,
    reference_images: list[dict[str, Any]],
    library_id: str | None = None,
) -> tuple[CharacterAsset, CharacterAssetVersion]:
    """人工在资产中心新建角色与首个不可变版本。"""

    library = db.get(AssetLibrary, library_id) if library_id else get_or_create_default_library(db, AssetLibraryKind.CHARACTER)
    if library is None or library.kind != AssetLibraryKind.CHARACTER:
        _not_found("角色资产库")
    if db.scalars(
        select(CharacterAsset.id).where(CharacterAsset.library_id == library.id, CharacterAsset.name == name.strip())
    ).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="同一资产库中已存在同名角色；请新增版本或使用不同名称")
    asset = CharacterAsset(library_id=library.id, name=name.strip(), description=description.strip(), status="ACTIVE")
    db.add(asset)
    db.flush()
    version = CharacterAssetVersion(
        character_asset_id=asset.id,
        version=1,
        description=description.strip(),
        age=age.strip() if age else None,
        gender=gender.strip() if gender else None,
        personality=personality.strip() if personality else None,
        style=style.strip() if style else None,
        appearance=appearance.strip() if appearance else None,
        costume=costume.strip() if costume else None,
        reference_images=normalize_reference_images(reference_images),
    )
    db.add(version)
    db.commit()
    db.refresh(asset)
    db.refresh(version)
    return asset, version


def append_character_asset_version(
    db: Session,
    *,
    character_asset_id: str,
    description: str,
    age: str | None,
    gender: str | None,
    personality: str | None,
    style: str | None,
    appearance: str | None,
    costume: str | None,
    reference_images: list[dict[str, Any]],
) -> CharacterAssetVersion:
    """仅追加角色版本；从不提供修改旧版本的接口。"""

    asset = db.get(CharacterAsset, character_asset_id)
    if asset is None:
        _not_found("角色资产")
    version = CharacterAssetVersion(
        character_asset_id=asset.id,
        version=_next_version(db, CharacterAssetVersion, CharacterAssetVersion.character_asset_id, asset.id),
        description=description.strip(),
        age=age.strip() if age else None,
        gender=gender.strip() if gender else None,
        personality=personality.strip() if personality else None,
        style=style.strip() if style else None,
        appearance=appearance.strip() if appearance else None,
        costume=costume.strip() if costume else None,
        reference_images=normalize_reference_images(reference_images),
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def create_scene_asset(
    db: Session,
    *,
    name: str,
    description: str,
    style: str | None,
    weather: str | None,
    time_of_day: str | None,
    location: str | None,
    environment: str | None,
    mood: str | None,
    reference_images: list[dict[str, Any]],
    library_id: str | None = None,
) -> tuple[SceneAsset, SceneAssetVersion]:
    """人工在资产中心新建场景和首版可复用设定。"""

    library = db.get(AssetLibrary, library_id) if library_id else get_or_create_default_library(db, AssetLibraryKind.SCENE)
    if library is None or library.kind != AssetLibraryKind.SCENE:
        _not_found("场景资产库")
    if db.scalars(
        select(SceneAsset.id).where(SceneAsset.library_id == library.id, SceneAsset.name == name.strip())
    ).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="同一资产库中已存在同名场景；请新增版本或使用不同名称")
    asset = SceneAsset(library_id=library.id, name=name.strip(), description=description.strip(), status="ACTIVE")
    db.add(asset)
    db.flush()
    version = SceneAssetVersion(
        scene_asset_id=asset.id,
        version=1,
        description=description.strip(),
        style=style.strip() if style else None,
        weather=weather.strip() if weather else None,
        time_of_day=time_of_day.strip() if time_of_day else None,
        location=location.strip() if location else None,
        environment=environment.strip() if environment else None,
        mood=mood.strip() if mood else None,
        reference_images=normalize_reference_images(reference_images),
    )
    db.add(version)
    db.commit()
    db.refresh(asset)
    db.refresh(version)
    return asset, version


def append_scene_asset_version(
    db: Session,
    *,
    scene_asset_id: str,
    description: str,
    style: str | None,
    weather: str | None,
    time_of_day: str | None,
    location: str | None,
    environment: str | None,
    mood: str | None,
    reference_images: list[dict[str, Any]],
) -> SceneAssetVersion:
    """仅追加场景版本；历史场景版本永远不可覆盖。"""

    asset = db.get(SceneAsset, scene_asset_id)
    if asset is None:
        _not_found("场景资产")
    version = SceneAssetVersion(
        scene_asset_id=asset.id,
        version=_next_version(db, SceneAssetVersion, SceneAssetVersion.scene_asset_id, asset.id),
        description=description.strip(),
        style=style.strip() if style else None,
        weather=weather.strip() if weather else None,
        time_of_day=time_of_day.strip() if time_of_day else None,
        location=location.strip() if location else None,
        environment=environment.strip() if environment else None,
        mood=mood.strip() if mood else None,
        reference_images=normalize_reference_images(reference_images),
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def _generated_character_asset(db: Session, character: CharacterDefinition) -> CharacterAsset:
    if character.asset_library_character_id:
        asset = db.get(CharacterAsset, character.asset_library_character_id)
        if asset is not None:
            return asset
    library = get_or_create_default_library(db, AssetLibraryKind.CHARACTER)
    # 同名角色在不同故事中可能不是同一个人，名称附加稳定定义短 ID 防止“自动合并”。
    asset = CharacterAsset(
        library_id=library.id,
        name=f"{character.name} · {character.id[:8]}",
        description=f"来自项目角色 {character.character_code} 的自动沉淀资产。",
        status="ACTIVE",
    )
    db.add(asset)
    db.flush()
    character.asset_library_character_id = asset.id
    return asset


def _generated_scene_asset(db: Session, scene: SceneDefinition) -> SceneAsset:
    if scene.asset_library_scene_id:
        asset = db.get(SceneAsset, scene.asset_library_scene_id)
        if asset is not None:
            return asset
    library = get_or_create_default_library(db, AssetLibraryKind.SCENE)
    asset = SceneAsset(
        library_id=library.id,
        name=f"{scene.name} · {scene.id[:8]}",
        description=f"来自项目场景 {scene.scene_code} 的自动沉淀资产。",
        status="ACTIVE",
    )
    db.add(asset)
    db.flush()
    scene.asset_library_scene_id = asset.id
    return asset


def ensure_character_asset_version_for_image(
    db: Session, character: CharacterDefinition, image: CharacterReferenceImage
) -> CharacterAssetVersion:
    """让项目角色图拥有一个资产中心版本；可安全用于旧数据的惰性补齐。"""

    if image.asset_version_id:
        existing = db.get(CharacterAssetVersion, image.asset_version_id)
        if existing is not None:
            return existing
    asset = _generated_character_asset(db, character)
    version = CharacterAssetVersion(
        character_asset_id=asset.id,
        version=_next_version(db, CharacterAssetVersion, CharacterAssetVersion.character_asset_id, asset.id),
        description=f"{character.name} 的项目锁图版本。",
        age=character.age_description,
        gender=None,
        personality=character.temperament,
        style="原创短剧角色设定",
        appearance=character.appearance,
        costume=character.costume,
        reference_images=normalize_reference_images(
            [{"view": "full_body", "url": image.image_url, "label": "项目生成角色图"}] if image.image_url else []
        ),
    )
    db.add(version)
    db.flush()
    image.asset_version_id = version.id
    return version


def ensure_scene_asset_version_for_image(
    db: Session, scene: SceneDefinition, image: SceneReferenceImage
) -> SceneAssetVersion:
    """让项目场景图拥有一个资产中心版本；历史图片也可安全补齐。"""

    if image.asset_version_id:
        existing = db.get(SceneAssetVersion, image.asset_version_id)
        if existing is not None:
            return existing
    asset = _generated_scene_asset(db, scene)
    version = SceneAssetVersion(
        scene_asset_id=asset.id,
        version=_next_version(db, SceneAssetVersion, SceneAssetVersion.scene_asset_id, asset.id),
        description=f"{scene.name} 的项目锁图版本。",
        style=scene.visual_style,
        weather=None,
        time_of_day=None,
        location=scene.location,
        environment=scene.environment,
        mood=scene.mood,
        reference_images=normalize_reference_images(
            [{"view": "wide", "url": image.image_url, "label": "项目生成场景图"}] if image.image_url else []
        ),
    )
    db.add(version)
    db.flush()
    image.asset_version_id = version.id
    return version


def select_character_asset_version_for_project(
    db: Session,
    *,
    character: CharacterDefinition,
    asset_version: CharacterAssetVersion,
    source_reference_image_id: str | None,
    locked: bool,
) -> ProjectCharacterAssetReference:
    """选择项目角色采用版本；只切换引用记录，不修改资产版本。"""

    db.query(ProjectCharacterAssetReference).filter(
        ProjectCharacterAssetReference.character_definition_id == character.id,
        ProjectCharacterAssetReference.is_selected.is_(True),
    ).update({"is_selected": False}, synchronize_session=False)
    row = db.scalars(
        select(ProjectCharacterAssetReference).where(
            ProjectCharacterAssetReference.character_definition_id == character.id,
            ProjectCharacterAssetReference.character_asset_version_id == asset_version.id,
        )
    ).first()
    if row is None:
        row = ProjectCharacterAssetReference(
            project_id=character.project_id,
            character_definition_id=character.id,
            character_asset_id=asset_version.character_asset_id,
            character_asset_version_id=asset_version.id,
            source_reference_image_id=source_reference_image_id,
            is_selected=True,
            locked_at=utcnow() if locked else None,
        )
        db.add(row)
    else:
        row.is_selected = True
        row.source_reference_image_id = source_reference_image_id
        if locked:
            row.locked_at = utcnow()
    return row


def select_scene_asset_version_for_project(
    db: Session,
    *,
    scene: SceneDefinition,
    asset_version: SceneAssetVersion,
    source_reference_image_id: str | None,
    locked: bool,
) -> ProjectSceneAssetReference:
    """选择项目场景采用版本；保留所有历史项目引用。"""

    db.query(ProjectSceneAssetReference).filter(
        ProjectSceneAssetReference.scene_definition_id == scene.id,
        ProjectSceneAssetReference.is_selected.is_(True),
    ).update({"is_selected": False}, synchronize_session=False)
    row = db.scalars(
        select(ProjectSceneAssetReference).where(
            ProjectSceneAssetReference.scene_definition_id == scene.id,
            ProjectSceneAssetReference.scene_asset_version_id == asset_version.id,
        )
    ).first()
    if row is None:
        row = ProjectSceneAssetReference(
            project_id=scene.project_id,
            scene_definition_id=scene.id,
            scene_asset_id=asset_version.scene_asset_id,
            scene_asset_version_id=asset_version.id,
            source_reference_image_id=source_reference_image_id,
            is_selected=True,
            locked_at=utcnow() if locked else None,
        )
        db.add(row)
    else:
        row.is_selected = True
        row.source_reference_image_id = source_reference_image_id
        if locked:
            row.locked_at = utcnow()
    return row


def ensure_library_backing_for_locked_assets(
    db: Session,
    *,
    characters: Iterable[CharacterDefinition],
    scenes: Iterable[SceneDefinition],
) -> None:
    """兼容旧项目：在继续进入导演阶段前补齐锁图的资产中心版本和项目引用。"""

    for character in characters:
        if not character.locked_reference_image_id:
            continue
        image = db.get(CharacterReferenceImage, character.locked_reference_image_id)
        if image is None:
            continue
        version = ensure_character_asset_version_for_image(db, character, image)
        select_character_asset_version_for_project(
            db,
            character=character,
            asset_version=version,
            source_reference_image_id=image.id,
            locked=True,
        )
    for scene in scenes:
        if not scene.locked_reference_image_id:
            continue
        image = db.get(SceneReferenceImage, scene.locked_reference_image_id)
        if image is None:
            continue
        version = ensure_scene_asset_version_for_image(db, scene, image)
        select_scene_asset_version_for_project(
            db,
            scene=scene,
            asset_version=version,
            source_reference_image_id=image.id,
            locked=True,
        )
    db.flush()


def character_asset_snapshot(db: Session, version_id: str | None) -> dict[str, Any] | None:
    """返回可安全写入 WorkflowRun 的角色资产版本快照。"""

    if not version_id:
        return None
    version = db.get(CharacterAssetVersion, version_id)
    if version is None:
        return None
    asset = db.get(CharacterAsset, version.character_asset_id)
    if asset is None:
        return None
    return {
        "asset_id": asset.id,
        "asset_name": asset.name,
        "asset_version_id": version.id,
        "version": version.version,
        "reference_images": deepcopy(version.reference_images),
        "age": version.age,
        "gender": version.gender,
        "personality": version.personality,
        "style": version.style,
    }


def scene_asset_snapshot(db: Session, version_id: str | None) -> dict[str, Any] | None:
    """返回可安全写入 WorkflowRun 的场景资产版本快照。"""

    if not version_id:
        return None
    version = db.get(SceneAssetVersion, version_id)
    if version is None:
        return None
    asset = db.get(SceneAsset, version.scene_asset_id)
    if asset is None:
        return None
    return {
        "asset_id": asset.id,
        "asset_name": asset.name,
        "asset_version_id": version.id,
        "version": version.version,
        "reference_images": deepcopy(version.reference_images),
        "style": version.style,
        "weather": version.weather,
        "time_of_day": version.time_of_day,
    }


def _preferred_reference_image_url(reference_images: list[dict[str, Any]]) -> str:
    """按全身/正面优先选择一张图作为项目锁图候选。"""

    for expected_view in ("full_body", "front", "wide", "generated", "side", "expression", "detail"):
        for item in reference_images:
            if item.get("view") == expected_view and isinstance(item.get("url"), str) and item["url"]:
                return item["url"]
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="资产版本没有可供项目采用的参考图")


def create_character_image_candidate_from_asset_version(
    db: Session,
    *,
    character: CharacterDefinition,
    asset_version: CharacterAssetVersion,
) -> CharacterReferenceImage:
    """将资产中心角色版本作为项目待锁定候选，不重新调用模型或复制资产内容。"""

    asset = db.get(CharacterAsset, asset_version.character_asset_id)
    if asset is None:
        _not_found("角色资产")
    image_url = _preferred_reference_image_url(asset_version.reference_images)
    run = WorkflowRun(
        project_id=character.project_id,
        workflow_key="v1_character_asset_adoption",
        status=RunStatus.SUCCEEDED,
        started_at=utcnow(),
        finished_at=utcnow(),
        input_snapshot={
            "source": "ASSET_LIBRARY",
            "character_definition_id": character.id,
            "character_asset_id": asset.id,
            "character_asset_version_id": asset_version.id,
            "asset_version": character_asset_snapshot(db, asset_version.id),
        },
    )
    db.add(run)
    db.flush()
    db.add(
        WorkflowStep(
            workflow_run_id=run.id,
            step_key="CHARACTER_ASSET_ADOPT",
            position=1,
            status=RunStatus.SUCCEEDED,
            progress=100,
            input_payload=deepcopy(run.input_snapshot or {}),
            output_payload={"character_asset_version_id": asset_version.id},
            started_at=run.started_at,
            finished_at=run.finished_at,
        )
    )
    image = CharacterReferenceImage(
        character_id=character.id,
        project_id=character.project_id,
        generation_run_id=run.id,
        asset_version_id=asset_version.id,
        version=(
            int(
                db.scalar(
                    select(func.max(CharacterReferenceImage.version)).where(CharacterReferenceImage.character_id == character.id)
                )
                or 0
            )
            + 1
        ),
        prompt_snapshot=f"资产中心采用：{asset.name} v{asset_version.version}",
        image_url=image_url,
        generation_status=RunStatus.SUCCEEDED,
    )
    db.add(image)
    db.flush()
    return image


def create_scene_image_candidate_from_asset_version(
    db: Session,
    *,
    scene: SceneDefinition,
    asset_version: SceneAssetVersion,
) -> SceneReferenceImage:
    """将资产中心场景版本作为项目待锁定候选，不重新调用图片模型。"""

    asset = db.get(SceneAsset, asset_version.scene_asset_id)
    if asset is None:
        _not_found("场景资产")
    image_url = _preferred_reference_image_url(asset_version.reference_images)
    run = WorkflowRun(
        project_id=scene.project_id,
        workflow_key="v1_scene_asset_adoption",
        status=RunStatus.SUCCEEDED,
        started_at=utcnow(),
        finished_at=utcnow(),
        input_snapshot={
            "source": "ASSET_LIBRARY",
            "scene_definition_id": scene.id,
            "scene_asset_id": asset.id,
            "scene_asset_version_id": asset_version.id,
            "asset_version": scene_asset_snapshot(db, asset_version.id),
        },
    )
    db.add(run)
    db.flush()
    db.add(
        WorkflowStep(
            workflow_run_id=run.id,
            step_key="SCENE_ASSET_ADOPT",
            position=1,
            status=RunStatus.SUCCEEDED,
            progress=100,
            input_payload=deepcopy(run.input_snapshot or {}),
            output_payload={"scene_asset_version_id": asset_version.id},
            started_at=run.started_at,
            finished_at=run.finished_at,
        )
    )
    image = SceneReferenceImage(
        scene_id=scene.id,
        project_id=scene.project_id,
        generation_run_id=run.id,
        asset_version_id=asset_version.id,
        version=(
            int(
                db.scalar(select(func.max(SceneReferenceImage.version)).where(SceneReferenceImage.scene_id == scene.id)) or 0)
            + 1
        ),
        prompt_snapshot=f"资产中心采用：{asset.name} v{asset_version.version}",
        image_url=image_url,
        generation_status=RunStatus.SUCCEEDED,
    )
    db.add(image)
    db.flush()
    return image
