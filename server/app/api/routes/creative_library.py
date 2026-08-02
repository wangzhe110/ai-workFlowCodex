"""爆点元素库和爆款开头库的管理接口。"""

from typing import Optional

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas import CreativeLibraryItemCreateRequest, CreativeLibraryItemResponse
from app.services.library_service import (
    create_library_item,
    deactivate_library_item,
    list_library_items,
    update_library_item,
)


router = APIRouter(prefix="/api/v1/creative-library", tags=["创作资产库"])


def _response(item) -> CreativeLibraryItemResponse:
    """编码资产库实体，保持数据库与 HTTP 字段隔离。"""

    return CreativeLibraryItemResponse(
        id=item.id,
        kind=item.kind.value,
        title=item.title,
        content=item.content,
        group_name=item.group_name,
        tags=item.tags,
        source=item.source.value,
        is_active=item.is_active,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.get("", response_model=list[CreativeLibraryItemResponse])
def list_creative_library_endpoint(
    kind: Optional[str] = None,
    active_only: bool = True,
    db: Session = Depends(get_db),
) -> list[CreativeLibraryItemResponse]:
    """列出爆点元素或开头模式，默认不返回已停用项目。"""

    return [_response(item) for item in list_library_items(db, kind=kind, active_only=active_only)]


@router.post("", response_model=CreativeLibraryItemResponse, status_code=status.HTTP_201_CREATED)
def create_creative_library_endpoint(
    payload: CreativeLibraryItemCreateRequest,
    db: Session = Depends(get_db),
) -> CreativeLibraryItemResponse:
    """人工新增可被选题工作流引用的抽象机制。"""

    return _response(
        create_library_item(
            db,
            kind=payload.kind,
            title=payload.title,
            content=payload.content,
            group_name=payload.group_name,
            tags=payload.tags,
        )
    )


@router.patch("/{item_id}", response_model=CreativeLibraryItemResponse)
def update_creative_library_endpoint(
    item_id: str,
    payload: CreativeLibraryItemCreateRequest,
    db: Session = Depends(get_db),
) -> CreativeLibraryItemResponse:
    """更新资产内容；Day 2 前端将以编辑表单提交完整字段。"""

    return _response(update_library_item(db, item_id, payload.model_dump()))


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def deactivate_creative_library_endpoint(item_id: str, db: Session = Depends(get_db)) -> Response:
    """停用而非物理删除资产，保障历史工作流审计完整。"""

    deactivate_library_item(db, item_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
