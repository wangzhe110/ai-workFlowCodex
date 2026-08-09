"""带货短剧（Commerce）工作流定义的初始化服务。

本模块刻意不调用 ``ensure_v1_foundation``，也不初始化模型、Prompt 或项目状态。
Commerce 与 LemonFlow V1 是并存工作流：本阶段只发布一份不可变定义，后续业务服务
在创建 ``StoryRun`` 时显式冻结和引用它。
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import WorkflowDefinition, WorkflowDefinitionStatus


COMMERCE_WORKFLOW_CODE = "LEMONFLOW_COMMERCE"
COMMERCE_WORKFLOW_VERSION = "LemonFlow_Commerce_V1"


def utcnow() -> datetime:
    """为定义发布记录生成 UTC 时间。"""

    return datetime.now(timezone.utc)


def commerce_definition_payload() -> dict[str, Any]:
    """返回带货短剧第一阶段的固定七节点定义快照。"""

    return {
        "name": "LemonFlow 带货短剧工作流",
        "nodes": [
            {"key": "TOPIC", "name": "选题", "position": 1},
            {"key": "OUTLINE", "name": "故事大纲", "position": 2},
            {"key": "CHAPTERS", "name": "章节规划", "position": 3},
            {"key": "STORYBOARD", "name": "分镜与场景映射", "position": 4},
            {"key": "VISUAL_ASSETS", "name": "场景图与关键帧", "position": 5},
            {"key": "VIDEO_PROMPTS", "name": "视频提示词", "position": 6},
            {"key": "SEGMENT_RENDER", "name": "批量片段生成", "position": 7},
        ],
        "run_modes": ["STEPWISE", "AUTO"],
        "scheduling": "REUSE_WORKFLOW_RUN_AND_STEP",
    }


def ensure_commerce_foundation(db: Session) -> WorkflowDefinition:
    """幂等创建 Commerce 工作流定义，绝不覆盖旧 V1 或既有定义。"""

    definition = db.scalars(
        select(WorkflowDefinition).where(
            WorkflowDefinition.workflow_code == COMMERCE_WORKFLOW_CODE,
            WorkflowDefinition.version == COMMERCE_WORKFLOW_VERSION,
        )
    ).first()
    if definition is None:
        definition = WorkflowDefinition(
            workflow_code=COMMERCE_WORKFLOW_CODE,
            version=COMMERCE_WORKFLOW_VERSION,
            definition_json=commerce_definition_payload(),
            status=WorkflowDefinitionStatus.PUBLISHED,
            published_at=utcnow(),
        )
        db.add(definition)
        db.commit()
        db.refresh(definition)
    return definition
