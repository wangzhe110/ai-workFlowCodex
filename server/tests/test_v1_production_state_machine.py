"""LemonFlow V1 审核闸门、状态机和配置版本接口测试。"""

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.main import app
from app.models import ReferenceAnalysis, RunStatus, StoryGenerationBatch, StoryProposal, WorkflowRun
from app.services.v1_production_service import mark_reference_analysis_ready, mark_story_batch_ready


def _create_reviewable_story_project(client: TestClient) -> tuple[str, str]:
    """建立已由 Worker 完成的分析和故事候选，只让 HTTP 接口执行人工决定。"""

    project_id = client.post("/api/v1/projects", json={"title": "V1 审核状态机"}).json()["id"]
    db = SessionLocal()
    try:
        run = WorkflowRun(project_id=project_id, workflow_key="v1_reference_analysis", status=RunStatus.SUCCEEDED)
        db.add(run)
        db.flush()
        analysis = ReferenceAnalysis(
            project_id=project_id,
            workflow_run_id=run.id,
            version=1,
            video_script_structure={"theme": "误会反转"},
            opening_analysis={"hook": "前 3 秒身份错位"},
            viral_elements=[{"name": "情绪反转"}],
            scene_analysis=[{"name": "医院走廊"}],
            creative_brief={"principle": "只借鉴结构，不复刻表达"},
            generation_status=RunStatus.SUCCEEDED,
        )
        db.add(analysis)
        db.commit()
        mark_reference_analysis_ready(db, analysis.id)
        return project_id, analysis.id
    finally:
        db.close()


def test_v1_analysis_lock_and_story_selection_are_formal_state_transitions() -> None:
    """锁定分析、故事候选选择和同批次防覆盖必须由 API 状态机控制。"""

    with TestClient(app) as client:
        project_id, analysis_id = _create_reviewable_story_project(client)
        before = client.get(f"/api/v1/production/projects/{project_id}/state")
        assert before.status_code == 200
        assert before.json()["active_stage"] == "ANALYSIS_REVIEW"

        locked = client.post(
            f"/api/v1/production/reference-analyses/{analysis_id}/lock",
            json={"reviewer_label": "制作人", "note": "采用创作简报"},
        )
        assert locked.status_code == 200
        assert locked.json()["review_status"] == "LOCKED"
        assert locked.json()["locked_snapshot"]["creative_brief"]["principle"] == "只借鉴结构，不复刻表达"
        assert client.get(f"/api/v1/production/projects/{project_id}/state").json()["active_stage"] == "STORY_GENERATION"

        db = SessionLocal()
        try:
            run = WorkflowRun(project_id=project_id, workflow_key="v1_story_generation", status=RunStatus.SUCCEEDED)
            db.add(run)
            db.flush()
            batch = StoryGenerationBatch(
                project_id=project_id,
                reference_analysis_id=analysis_id,
                workflow_run_id=run.id,
                request_snapshot={"analysis_id": analysis_id},
                status=RunStatus.SUCCEEDED,
            )
            db.add(batch)
            db.flush()
            candidate_one = StoryProposal(
                batch_id=batch.id,
                project_id=project_id,
                candidate_number=1,
                content={"title": "原创方案一"},
            )
            candidate_two = StoryProposal(
                batch_id=batch.id,
                project_id=project_id,
                candidate_number=2,
                content={"title": "原创方案二"},
            )
            db.add_all([candidate_one, candidate_two])
            db.commit()
            mark_story_batch_ready(db, batch.id)
            first_id, second_id = candidate_one.id, candidate_two.id
        finally:
            db.close()

        selected = client.post(
            f"/api/v1/production/story-proposals/{first_id}/select",
            json={"reviewer_label": "编剧审核"},
        )
        assert selected.status_code == 200
        assert selected.json()["status"] == "SELECTED"
        assert client.get(f"/api/v1/production/projects/{project_id}/state").json()["active_stage"] == "CHARACTER_ASSETS"

        cannot_overwrite = client.post(
            f"/api/v1/production/story-proposals/{second_id}/select",
            json={"reviewer_label": "编剧审核"},
        )
        assert cannot_overwrite.status_code == 409


def test_v1_model_slots_and_prompt_versions_are_configurable_without_model_names_in_workflow() -> None:
    """模型槽位和 Prompt 以可版本化配置提供，V1 不自动执行模型切换。"""

    with TestClient(app) as client:
        slots = client.get("/api/v1/production/model-slots")
        assert slots.status_code == 200
        slot_by_key = {item["slot_key"]: item for item in slots.json()}
        assert slot_by_key["VIDEO_ANALYSIS"]["selection_mode"] == "SINGLE"
        assert slot_by_key["STORY_GENERATE"]["selection_mode"] == "MULTI_PARALLEL"

        blocked = client.post(
            "/api/v1/production/model-slots/VIDEO_ANALYSIS/strategy",
            json={"selection_mode": "MULTI_PARALLEL"},
        )
        assert blocked.status_code == 409

        first = client.post(
            "/api/v1/production/prompt-templates",
            json={
                "task_type": "STORY_GENERATE",
                "name": "原创故事生成",
                "content": "基于 {{creative_brief}} 生成原创故事，不复制原人物。",
                "variables_schema": {"type": "object", "properties": {"creative_brief": {"type": "object"}}},
            },
        )
        assert first.status_code == 201
        assert first.json()["version"] == 1
        activated = client.post(f"/api/v1/production/prompt-templates/{first.json()['id']}/activate")
        assert activated.status_code == 200
        assert activated.json()["status"] == "ACTIVE"

        second = client.post(
            "/api/v1/production/prompt-templates",
            json={
                "task_type": "STORY_GENERATE",
                "name": "原创故事生成",
                "content": "基于 {{creative_brief}} 和 {{audience}} 生成全新故事。",
                "variables_schema": {"type": "object", "required": ["creative_brief", "audience"]},
            },
        )
        assert second.status_code == 201
        assert second.json()["version"] == 2
        # 无论模板名称是否变化，同一生产任务只能有一个 ACTIVE 版本；否则 Worker
        # 会无法确定本次生产应该冻结哪一份 Prompt。
        switched = client.post(f"/api/v1/production/prompt-templates/{second.json()['id']}/activate")
        assert switched.status_code == 200
        templates = client.get("/api/v1/production/prompt-templates?task_type=STORY_GENERATE")
        assert templates.status_code == 200
        assert len([item for item in templates.json() if item["status"] == "ACTIVE"]) == 1
        assert next(item for item in templates.json() if item["id"] == second.json()["id"])["status"] == "ACTIVE"

        # 读取模型槽位会触发 V1 配置初始化；初始化不得因默认模板名称不同而重新
        # 写入一个 ACTIVE Prompt，覆盖制作人刚刚做出的明确选择。
        assert client.get("/api/v1/production/model-slots").status_code == 200
        after_reinitialize = client.get("/api/v1/production/prompt-templates?task_type=STORY_GENERATE")
        assert len([item for item in after_reinitialize.json() if item["status"] == "ACTIVE"]) == 1
        assert next(item for item in after_reinitialize.json() if item["id"] == second.json()["id"])["status"] == "ACTIVE"

        cannot_archive_active = client.post(f"/api/v1/production/prompt-templates/{second.json()['id']}/archive")
        assert cannot_archive_active.status_code == 409
