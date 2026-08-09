"""带货短剧领域校验。

本阶段不提供 API 或调度能力。这里仅放置不能由单表 CHECK 表达的跨表规则，后续
创建分镜服务必须经过这些函数，避免子镜头越过所属视频片段的目标时长。
"""

from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import SubShotPlan, VideoSegmentPlan


class CommerceDomainValidationError(ValueError):
    """调用方提供的 Commerce 领域数据不满足生产约束。"""


def validate_sub_shot_within_segment(segment: VideoSegmentPlan, *, start_ms: int, end_ms: int) -> None:
    """校验子镜头相对时间不超过父片段目标时长。

    ``SubShotPlan`` 自身的 start/end 基础关系由数据库 CHECK 约束保证；父表时长
    无法用通用 SQL CHECK 跨表读取，因此在这个领域边界显式验证并由测试覆盖。
    """

    if start_ms < 0:
        raise CommerceDomainValidationError("子镜头开始时间不能小于 0ms")
    if end_ms <= start_ms:
        raise CommerceDomainValidationError("子镜头结束时间必须晚于开始时间")
    if end_ms > segment.target_duration_ms:
        raise CommerceDomainValidationError(
            f"子镜头结束时间 {end_ms}ms 超出片段目标时长 {segment.target_duration_ms}ms"
        )


def add_sub_shot_plan(
    db: Session,
    segment: VideoSegmentPlan,
    *,
    shot_number: int,
    start_ms: int,
    end_ms: int,
    character_refs: Optional[list[dict[str, Any]]] = None,
    action: str,
    emotion: str,
    shot_scale: str,
    camera_move: str,
    lighting: str,
    visual_description: str,
) -> SubShotPlan:
    """经过跨表校验后追加一个子镜头，绝不修改已有镜头时间线。"""

    validate_sub_shot_within_segment(segment, start_ms=start_ms, end_ms=end_ms)
    sub_shot = SubShotPlan(
        video_segment_id=segment.id,
        shot_number=shot_number,
        start_ms=start_ms,
        end_ms=end_ms,
        character_refs=character_refs or [],
        action=action,
        emotion=emotion,
        shot_scale=shot_scale,
        camera_move=camera_move,
        lighting=lighting,
        visual_description=visual_description,
    )
    db.add(sub_shot)
    return sub_shot
