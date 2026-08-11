"""Commerce Phase 3 爆款知识库的项目隔离、版本和归档规则。"""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    KnowledgeChunk,
    KnowledgeResourceType,
    Project,
    ViralCase,
    ViralKnowledgeStatus,
    ViralPattern,
    ViralPatternType,
)
from app.models.entities import new_id


def _error(detail: str, code: int = status.HTTP_422_UNPROCESSABLE_CONTENT) -> None:
    raise HTTPException(status_code=code, detail=detail)


def _project_or_404(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        _error("项目不存在", status.HTTP_404_NOT_FOUND)
    return project


def _case_or_404(db: Session, project_id: str, case_id: str) -> ViralCase:
    case = db.scalar(select(ViralCase).where(ViralCase.id == case_id, ViralCase.project_id == project_id))
    if case is None:
        _error("爆款案例不存在", status.HTTP_404_NOT_FOUND)
    return case


def _pattern_or_404(db: Session, project_id: str, pattern_id: str) -> ViralPattern:
    pattern = db.scalar(select(ViralPattern).where(ViralPattern.id == pattern_id, ViralPattern.project_id == project_id))
    if pattern is None:
        _error("爆款模式不存在", status.HTTP_404_NOT_FOUND)
    return pattern


def _normalize_tags(tags: list[str]) -> list[str]:
    result: list[str] = []
    for item in tags:
        tag = item.strip()
        if tag and tag not in result:
            result.append(tag[:80])
    return result


def _parse_case_status(value: str) -> ViralKnowledgeStatus:
    try:
        return ViralKnowledgeStatus(value)
    except ValueError as exc:
        _error("知识状态不合法")
        raise AssertionError("unreachable") from exc


def _parse_pattern_type(value: str) -> ViralPatternType:
    try:
        return ViralPatternType(value)
    except ValueError as exc:
        _error("爆款模式类型不合法")
        raise AssertionError("unreachable") from exc


def _case_fields(payload: dict[str, Any]) -> dict[str, Any]:
    source_type = str(payload.get("source_type") or "").strip()
    source_identifier = str(payload.get("source_identifier") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not source_type or not source_identifier or not title:
        _error("source_type、source_identifier 和 title 不能为空")
    return {
        "source_type": source_type[:60],
        "source_identifier": source_identifier[:512],
        "source_url": (str(payload["source_url"]).strip()[:1024] if payload.get("source_url") else None),
        "title": title[:240],
        "summary": (str(payload["summary"]).strip() if payload.get("summary") else None),
        "raw_text": (str(payload["raw_text"]).strip() if payload.get("raw_text") else None),
        "transcript_reference": (str(payload["transcript_reference"]).strip()[:512] if payload.get("transcript_reference") else None),
        "raw_analysis": dict(payload.get("raw_analysis") or {}),
        "structured_analysis": dict(payload.get("structured_analysis") or {}),
        "tags": _normalize_tags(list(payload.get("tags") or [])),
        "category": (str(payload["category"]).strip()[:100] if payload.get("category") else None),
    }


def create_viral_case(db: Session, *, project_id: str, payload: dict[str, Any], created_by: str | None = None) -> ViralCase:
    _project_or_404(db, project_id)
    item = ViralCase(project_id=project_id, created_by=created_by, **_case_fields(payload))
    try:
        with db.begin_nested():
            db.add(item)
            db.flush()
    except IntegrityError as exc:
        message = str(exc.orig).lower()
        if "uq_viral_case_project_source" in message or "viral_cases.project_id, viral_cases.source_type" in message:
            _error("同项目已存在相同来源的爆款案例", status.HTTP_409_CONFLICT)
        raise
    db.commit()
    db.refresh(item)
    return item


def list_viral_cases(
    db: Session,
    *,
    project_id: str,
    page: int = 1,
    page_size: int = 20,
    status_value: Optional[str] = None,
    category: Optional[str] = None,
    tag: Optional[str] = None,
    keyword: Optional[str] = None,
) -> tuple[list[ViralCase], int]:
    _project_or_404(db, project_id)
    statement = select(ViralCase).where(ViralCase.project_id == project_id)
    if status_value:
        statement = statement.where(ViralCase.status == _parse_case_status(status_value))
    if category:
        statement = statement.where(ViralCase.category == category.strip())
    if keyword:
        term = f"%{keyword.strip()}%"
        statement = statement.where((ViralCase.title.ilike(term)) | (ViralCase.summary.ilike(term)))
    # JSON array membership is intentionally applied in Python so SQLite and
    # PostgreSQL return the same semantics without coupling the API to either.
    rows = list(db.scalars(statement.order_by(ViralCase.created_at.desc(), ViralCase.id.desc())).all())
    if tag:
        rows = [item for item in rows if tag.strip() in (item.tags or [])]
    total = len(rows)
    offset = (page - 1) * page_size
    return rows[offset: offset + page_size], total


def get_viral_case(db: Session, *, project_id: str, case_id: str) -> ViralCase:
    _project_or_404(db, project_id)
    return _case_or_404(db, project_id, case_id)


def update_viral_case(db: Session, *, project_id: str, case_id: str, changes: dict[str, Any]) -> ViralCase:
    item = _case_or_404(db, project_id, case_id)
    if item.status == ViralKnowledgeStatus.ARCHIVED:
        _error("已归档爆款案例不能修改", status.HTTP_409_CONFLICT)
    allowed = {"title", "summary", "raw_text", "transcript_reference", "raw_analysis", "structured_analysis", "tags", "category", "source_url"}
    unknown = set(changes) - allowed
    if unknown:
        _error("包含不允许修改的爆款案例字段")
    if "title" in changes:
        title = str(changes["title"] or "").strip()
        if not title:
            _error("title 不能为空")
        item.title = title[:240]
    for key in ("summary", "raw_text", "transcript_reference", "source_url", "category"):
        if key in changes:
            value = changes[key]
            setattr(item, key, str(value).strip() if value else None)
    for key in ("raw_analysis", "structured_analysis"):
        if key in changes:
            if not isinstance(changes[key], dict):
                _error(f"{key} 必须是对象")
            setattr(item, key, dict(changes[key]))
    if "tags" in changes:
        if not isinstance(changes["tags"], list):
            _error("tags 必须是数组")
        item.tags = _normalize_tags(changes["tags"])
    db.commit()
    db.refresh(item)
    return item


def archive_viral_case(db: Session, *, project_id: str, case_id: str) -> ViralCase:
    item = _case_or_404(db, project_id, case_id)
    if item.status == ViralKnowledgeStatus.ARCHIVED:
        _error("爆款案例已经归档", status.HTTP_409_CONFLICT)
    item.status = ViralKnowledgeStatus.ARCHIVED
    db.commit()
    db.refresh(item)
    return item


def _pattern_fields(db: Session, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        _error("name 不能为空")
    source_case_id = payload.get("source_case_id")
    if source_case_id:
        _case_or_404(db, project_id, str(source_case_id))
    rules = payload.get("structured_rules") or {}
    if not isinstance(rules, dict):
        _error("structured_rules 必须是对象")
    scenarios = payload.get("applicable_scenarios") or []
    if not isinstance(scenarios, list):
        _error("applicable_scenarios 必须是数组")
    return {
        "source_case_id": str(source_case_id) if source_case_id else None,
        "pattern_type": _parse_pattern_type(str(payload.get("pattern_type") or "")),
        "name": name[:240],
        "summary": (str(payload["summary"]).strip() if payload.get("summary") else None),
        "structured_rules": dict(rules),
        "applicable_scenarios": _normalize_tags([str(value) for value in scenarios]),
        "tags": _normalize_tags([str(value) for value in payload.get("tags") or []]),
    }


def create_viral_pattern(db: Session, *, project_id: str, payload: dict[str, Any], created_by: str | None = None) -> ViralPattern:
    _project_or_404(db, project_id)
    item = ViralPattern(
        project_id=project_id,
        pattern_key=new_id(),
        version=1,
        is_current=True,
        status=ViralKnowledgeStatus.DRAFT,
        created_by=created_by,
        **_pattern_fields(db, project_id, payload),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def list_viral_patterns(
    db: Session,
    *,
    project_id: str,
    page: int = 1,
    page_size: int = 20,
    status_value: Optional[str] = None,
    pattern_type: Optional[str] = None,
    tag: Optional[str] = None,
    keyword: Optional[str] = None,
) -> tuple[list[ViralPattern], int]:
    _project_or_404(db, project_id)
    statement = select(ViralPattern).where(ViralPattern.project_id == project_id)
    if status_value:
        statement = statement.where(ViralPattern.status == _parse_case_status(status_value))
    if pattern_type:
        statement = statement.where(ViralPattern.pattern_type == _parse_pattern_type(pattern_type))
    if keyword:
        term = f"%{keyword.strip()}%"
        statement = statement.where((ViralPattern.name.ilike(term)) | (ViralPattern.summary.ilike(term)))
    rows = list(db.scalars(statement.order_by(ViralPattern.updated_at.desc(), ViralPattern.id.desc())).all())
    if tag:
        rows = [item for item in rows if tag.strip() in (item.tags or [])]
    total = len(rows)
    offset = (page - 1) * page_size
    return rows[offset: offset + page_size], total


def get_viral_pattern(db: Session, *, project_id: str, pattern_id: str) -> ViralPattern:
    _project_or_404(db, project_id)
    return _pattern_or_404(db, project_id, pattern_id)


def update_viral_pattern_draft(db: Session, *, project_id: str, pattern_id: str, changes: dict[str, Any]) -> ViralPattern:
    item = _pattern_or_404(db, project_id, pattern_id)
    if item.status != ViralKnowledgeStatus.DRAFT or not item.is_current:
        _error("只有当前草稿模式可以修改", status.HTTP_409_CONFLICT)
    merged = {
        "source_case_id": changes.get("source_case_id", item.source_case_id),
        "pattern_type": changes.get("pattern_type", item.pattern_type.value),
        "name": changes.get("name", item.name),
        "summary": changes.get("summary", item.summary),
        "structured_rules": changes.get("structured_rules", item.structured_rules),
        "applicable_scenarios": changes.get("applicable_scenarios", item.applicable_scenarios),
        "tags": changes.get("tags", item.tags),
    }
    for key, value in _pattern_fields(db, project_id, merged).items():
        setattr(item, key, value)
    db.commit()
    db.refresh(item)
    return item


def publish_viral_pattern_version(
    db: Session,
    *,
    project_id: str,
    pattern_id: str,
    payload: Optional[dict[str, Any]] = None,
    created_by: str | None = None,
) -> ViralPattern:
    """发布草稿，或从当前已发布版本追加一个新的不可覆盖版本。"""

    current = _pattern_or_404(db, project_id, pattern_id)
    if current.status == ViralKnowledgeStatus.ARCHIVED and not current.is_current:
        _error("历史归档模式不能直接发布", status.HTTP_409_CONFLICT)
    if current.status == ViralKnowledgeStatus.DRAFT:
        if payload:
            update_viral_pattern_draft(db, project_id=project_id, pattern_id=pattern_id, changes=payload)
        current.status = ViralKnowledgeStatus.ACTIVE
        current.is_current = True
        db.commit()
        db.refresh(current)
        return current
    if current.status != ViralKnowledgeStatus.ACTIVE or not current.is_current:
        _error("只能从当前已发布模式创建新版本", status.HTTP_409_CONFLICT)
    data = {
        "source_case_id": current.source_case_id,
        "pattern_type": current.pattern_type.value,
        "name": current.name,
        "summary": current.summary,
        "structured_rules": current.structured_rules,
        "applicable_scenarios": current.applicable_scenarios,
        "tags": current.tags,
    }
    data.update(payload or {})
    fields = _pattern_fields(db, project_id, data)
    next_version = int(
        db.scalar(
            select(func.max(ViralPattern.version)).where(
                ViralPattern.project_id == project_id, ViralPattern.pattern_key == current.pattern_key
            )
        ) or 0
    ) + 1
    current.status = ViralKnowledgeStatus.ARCHIVED
    current.is_current = False
    next_item = ViralPattern(
        project_id=project_id,
        pattern_key=current.pattern_key,
        version=next_version,
        is_current=True,
        status=ViralKnowledgeStatus.ACTIVE,
        created_by=created_by,
        **fields,
    )
    db.add(next_item)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _error("爆款模式版本冲突，请重新读取后发布", status.HTTP_409_CONFLICT)
        raise AssertionError("unreachable") from exc
    db.refresh(next_item)
    return next_item


def archive_viral_pattern(db: Session, *, project_id: str, pattern_id: str) -> ViralPattern:
    item = _pattern_or_404(db, project_id, pattern_id)
    if item.status == ViralKnowledgeStatus.ARCHIVED:
        _error("爆款模式已经归档", status.HTTP_409_CONFLICT)
    item.status = ViralKnowledgeStatus.ARCHIVED
    item.is_current = False
    db.commit()
    db.refresh(item)
    return item


def create_knowledge_chunk(db: Session, *, project_id: str, payload: dict[str, Any]) -> KnowledgeChunk:
    _project_or_404(db, project_id)
    case_id, pattern_id = payload.get("viral_case_id"), payload.get("viral_pattern_id")
    if bool(case_id) == bool(pattern_id):
        _error("KnowledgeChunk 必须且只能关联一个来源")
    resource_type = KnowledgeResourceType.VIRAL_CASE if case_id else KnowledgeResourceType.VIRAL_PATTERN
    source_id = str(case_id or pattern_id)
    if case_id:
        _case_or_404(db, project_id, source_id)
    else:
        _pattern_or_404(db, project_id, source_id)
    content = str(payload.get("content") or "").strip()
    if not content:
        _error("KnowledgeChunk content 不能为空")
    chunk_index = payload.get("chunk_index")
    if not isinstance(chunk_index, int) or chunk_index < 0:
        _error("chunk_index 必须是非负整数")
    embedding_dimension = payload.get("embedding_dimension")
    if embedding_dimension is not None and (not isinstance(embedding_dimension, int) or embedding_dimension <= 0):
        _error("embedding_dimension 必须是正整数或为空")
    item = KnowledgeChunk(
        viral_case_id=str(case_id) if case_id else None,
        viral_pattern_id=str(pattern_id) if pattern_id else None,
        resource_type=resource_type,
        resource_id=source_id,
        chunk_index=chunk_index,
        content=content,
        content_hash=sha256(content.encode("utf-8")).hexdigest(),
        metadata_json=dict(payload.get("metadata") or {}),
        embedding_provider=payload.get("embedding_provider"),
        embedding_model=payload.get("embedding_model"),
        embedding_dimension=embedding_dimension,
        external_vector_id=payload.get("external_vector_id"),
    )
    try:
        with db.begin_nested():
            db.add(item)
            db.flush()
    except IntegrityError as exc:
        _error("同一知识资源的切片序号已存在", status.HTTP_409_CONFLICT)
        raise AssertionError("unreachable") from exc
    db.commit()
    db.refresh(item)
    return item
