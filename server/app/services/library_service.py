"""爆点元素库与爆款开头库的业务规则。"""

from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CreativeLibraryItem, LibraryItemKind, LibraryItemSource


def _parse_kind(value: str) -> LibraryItemKind:
    """把接口字符串转换为受控枚举，防止库中出现无法识别的资产类型。"""

    try:
        return LibraryItemKind(value)
    except ValueError as exc:
        accepted_values = ", ".join(kind.value for kind in LibraryItemKind)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"kind 必须为：{accepted_values}",
        ) from exc


def _normalize_tags(tags: list[str]) -> list[str]:
    """清洗标签，保留顺序、去重并避免无意义的空字符串。"""

    normalized: list[str] = []
    for tag in tags:
        clean_tag = tag.strip()
        if clean_tag and clean_tag not in normalized:
            normalized.append(clean_tag[:40])
    return normalized


def create_library_item(
    db: Session,
    *,
    kind: str,
    title: str,
    content: str,
    group_name: Optional[str],
    tags: list[str],
) -> CreativeLibraryItem:
    """创建人工维护的抽象创作资产。"""

    item = CreativeLibraryItem(
        kind=_parse_kind(kind),
        title=title.strip(),
        content=content.strip(),
        group_name=group_name.strip() if group_name and group_name.strip() else None,
        tags=_normalize_tags(tags),
        source=LibraryItemSource.MANUAL,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def list_library_items(
    db: Session,
    *,
    kind: Optional[str] = None,
    active_only: bool = True,
) -> list[CreativeLibraryItem]:
    """按类型和启用状态读取创作资产，供选题提示词与管理页面使用。"""

    statement = select(CreativeLibraryItem).order_by(CreativeLibraryItem.updated_at.desc())
    if kind:
        statement = statement.where(CreativeLibraryItem.kind == _parse_kind(kind))
    if active_only:
        statement = statement.where(CreativeLibraryItem.is_active.is_(True))
    return list(db.scalars(statement).all())


def get_library_item_or_404(db: Session, item_id: str) -> CreativeLibraryItem:
    """读取单个资产并统一返回 404 错误。"""

    item = db.get(CreativeLibraryItem, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="创作资产不存在")
    return item


def update_library_item(db: Session, item_id: str, changes: dict) -> CreativeLibraryItem:
    """按已提交的字段更新资产，允许把分组清空为 null。"""

    item = get_library_item_or_404(db, item_id)
    if "kind" in changes:
        item.kind = _parse_kind(changes["kind"])
    if "title" in changes:
        item.title = changes["title"].strip()
    if "content" in changes:
        item.content = changes["content"].strip()
    if "group_name" in changes:
        raw_group_name = changes["group_name"]
        item.group_name = raw_group_name.strip() if raw_group_name and raw_group_name.strip() else None
    if "tags" in changes:
        item.tags = _normalize_tags(changes["tags"])
    if "is_active" in changes:
        item.is_active = bool(changes["is_active"])
    db.commit()
    db.refresh(item)
    return item


def deactivate_library_item(db: Session, item_id: str) -> None:
    """软停用资产，保留历史任务对它的引用和审计记录。"""

    item = get_library_item_or_404(db, item_id)
    item.is_active = False
    db.commit()
