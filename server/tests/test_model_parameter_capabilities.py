"""模型能力、质量预设与参数冻结的安全边界回归测试。

测试只构造 Profile 快照或走模型中心的配置 API；所有 Adapter 网络入口均不被调用。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

from app.main import app
from app.services.model_parameter_service import (
    resolve_effective_model_parameters,
    validate_parameter_config,
)


def _video_parameter_config(
    *,
    resolutions: list[str] | None = None,
    duration_range: tuple[int, int] = (2, 12),
    include_high: bool = True,
) -> dict:
    """返回一个只缩小现有 Seedance 协议边界的 Profile 参数版本。"""

    base = {
        "schema_version": 1,
        "capability": "video",
        "supported_parameters": {
            "input_mode": {"values": ["first_frame"]},
            "duration": {"minimum": duration_range[0], "maximum": duration_range[1]},
            "resolution": {"values": resolutions or ["480p"]},
            "aspect_ratio": {"values": ["9:16", "16:9"]},
            "generate_audio": {"values": [False]},
            "watermark": {"values": [False]},
        },
        "defaults": {
            "input_mode": "first_frame",
            "duration": max(duration_range[0], min(5, duration_range[1])),
            "resolution": (resolutions or ["480p"])[0],
            "aspect_ratio": "9:16",
            "generate_audio": False,
            "watermark": False,
        },
        "presets": {
            "preview": {"duration": max(duration_range[0], min(5, duration_range[1])), "resolution": (resolutions or ["480p"])[0], "generate_audio": False},
            "standard": {"duration": max(duration_range[0], min(5, duration_range[1])), "resolution": (resolutions or ["480p"])[0], "generate_audio": False},
        },
    }
    if include_high:
        base["presets"]["high"] = {"duration": max(duration_range[0], min(5, duration_range[1])), "resolution": (resolutions or ["480p"])[0], "generate_audio": False}
    return base


def _video_snapshot(parameter_config: dict) -> dict:
    return {
        "adapter_key": "volcengine_ark_video",
        "model_key": "doubao-seedance-2-5-260628",
        "provider_config": {
            "api_base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "secret_env_name": "ARK_API_KEY",
            "duration": 5,
            "resolution": "480p",
            "generate_audio": False,
        },
        "parameter_config": parameter_config,
    }


def test_parameter_config_rejects_unknown_and_recursive_sensitive_fields() -> None:
    """能力 JSON 不是任意供应商请求体，嵌套敏感字段也不能绕过检查。"""

    config = _video_parameter_config()
    config["presets"]["standard"]["credential"] = "must-not-be-stored"
    with pytest.raises(HTTPException) as sensitive:
        validate_parameter_config("volcengine_ark_video", config)
    assert sensitive.value.status_code == 422
    assert "敏感" in str(sensitive.value.detail)

    config = _video_parameter_config()
    config["defaults"]["unknown_vendor_flag"] = True
    with pytest.raises(HTTPException) as unknown:
        validate_parameter_config("volcengine_ark_video", config)
    assert unknown.value.status_code == 422
    assert "未被当前 Profile 声明支持" in str(unknown.value.detail)


def test_video_parameter_resolution_rejects_unsupported_resolution_and_omits_legacy_ratio() -> None:
    """1080p 不可被静默降为 480p；首帧模式会审计性地省略 ratio。"""

    snapshot = _video_snapshot(_video_parameter_config(resolutions=["480p"], include_high=False))
    with pytest.raises(HTTPException) as unsupported:
        resolve_effective_model_parameters(snapshot, preset="standard", run_overrides={"resolution": "1080p"})
    assert unsupported.value.status_code == 422
    assert "不支持值" in str(unsupported.value.detail)

    resolved = resolve_effective_model_parameters(
        snapshot,
        preset="preview",
        run_overrides={"ratio": "16:9", "duration": 5},
        execution_context={"operation": "VIDEO_RENDER", "input_mode": "first_frame"},
    )
    assert resolved["selected_preset"] == "preview"
    assert resolved["parameter_sources"]["duration"] == "run_override"
    assert "aspect_ratio" not in resolved["effective_parameters"]
    assert {item["parameter"] for item in resolved["omitted_parameters"]} == {"aspect_ratio", "ratio"}

    with pytest.raises(HTTPException) as no_high:
        resolve_effective_model_parameters(snapshot, preset="high")
    assert no_high.value.status_code == 422
    assert "不支持 high" in str(no_high.value.detail)


def test_video_profiles_can_expose_different_safe_duration_ranges() -> None:
    """Profile 只能收窄协议边界，前端可据此展示模型各自的时长选项。"""

    short = _video_snapshot(_video_parameter_config(duration_range=(2, 5)))
    long = _video_snapshot(_video_parameter_config(duration_range=(6, 12)))
    assert resolve_effective_model_parameters(short)["effective_parameters"]["duration"] == 5
    assert resolve_effective_model_parameters(long)["effective_parameters"]["duration"] == 6
    with pytest.raises(HTTPException) as unsupported_short_duration:
        resolve_effective_model_parameters(short, run_overrides={"duration": 6})
    assert unsupported_short_duration.value.status_code == 422


def test_profile_copy_preserves_complete_parameter_config_without_network_calls() -> None:
    """复制的 Draft 必须完整携带版本化能力配置，且全过程不做模型调用。"""

    config = _video_parameter_config(resolutions=["480p", "720p"])
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/production/v1-model-profiles",
            json={
                "slot_key": "VIDEO_GENERATE",
                "adapter_key": "volcengine_ark_video",
                "model_key": "doubao-seedance-2-5-260628",
                "display_name": "参数能力复制测试",
                "provider_config": {
                    "api_base_url": "https://ark.cn-beijing.volces.com/api/v3",
                    "secret_env_name": "ARK_API_KEY",
                    "duration": 5,
                    "resolution": "480p",
                    "generate_audio": False,
                },
                "parameter_config": config,
                "enable_in_slot": False,
            },
        )
        assert created.status_code == 201, created.text
        source = created.json()
        try:
            assert source["parameter_config_complete"] is True
            copied = client.post(f"/api/v1/production/v1-model-profiles/{source['id']}/copy")
            assert copied.status_code == 201, copied.text
            assert copied.json()["parameter_config"] == source["parameter_config"]
            assert copied.json()["version"] == source["version"] + 1
        finally:
            for profile_id in [item["id"] for item in client.get("/api/v1/production/v1-model-profiles").json() if item["display_name"].startswith("参数能力复制测试")]:
                response = client.delete(f"/api/v1/production/v1-model-profiles/{profile_id}")
                assert response.status_code == 204, response.text


def test_copy_legacy_profile_materializes_config_and_replaces_one_parallel_candidate() -> None:
    """并行故事槽位只替换指定旧版本，其他并行候选不能被误停用。"""

    with TestClient(app) as client:
        profiles = client.get("/api/v1/production/v1-model-profiles").json()
        source = next(
            item
            for item in profiles
            if item["slot_key"] == "STORY_GENERATE"
            and item["is_enabled_in_slot"]
            and not item["parameter_config_complete"]
        )
        other_enabled_ids = {
            item["id"]
            for item in profiles
            if item["slot_key"] == "STORY_GENERATE" and item["is_enabled_in_slot"] and item["id"] != source["id"]
        }
        copied = client.post(f"/api/v1/production/v1-model-profiles/{source['id']}/copy")
        assert copied.status_code == 201, copied.text
        draft = copied.json()
        try:
            assert draft["parameter_config_complete"] is True
            switched = client.post(
                "/api/v1/production/model-slots/STORY_GENERATE/bindings",
                json={
                    "model_profile_id": draft["id"],
                    "enabled": True,
                    "replace_existing": True,
                    "replace_profile_id": source["id"],
                    "priority": source["priority"] or 100,
                },
            )
            assert switched.status_code == 201, switched.text
            after = client.get("/api/v1/production/v1-model-profiles").json()
            states = {item["id"]: item["is_enabled_in_slot"] for item in after}
            assert states[source["id"]] is False
            assert states[draft["id"]] is True
            assert all(states[profile_id] is True for profile_id in other_enabled_ids)
        finally:
            client.post(
                "/api/v1/production/model-slots/STORY_GENERATE/bindings",
                json={
                    "model_profile_id": source["id"],
                    "enabled": True,
                    "replace_existing": True,
                    "replace_profile_id": draft["id"],
                    "priority": source["priority"] or 100,
                },
            )
            # 源版本来自系统保留的 mock 种子，复制稿沿用该命名会被删除保护拦截。
            # 仅为测试清理改成非保留命名；能力配置与槽位替换断言已经在上方完成。
            renamed = client.patch(
                f"/api/v1/production/v1-model-profiles/{draft['id']}",
                json={
                    "adapter_key": draft["adapter_key"],
                    "model_key": "test-legacy-copy-cleanup",
                    "display_name": "参数能力复制清理稿",
                    "model_version": "test-legacy-copy-cleanup",
                    # 历史 mock 种子可带有已被安全层禁止回传的旧字段；清理时仅使用
                    # 当前 mock Adapter 允许的本地标识配置，不接触任何鉴权字段。
                    "provider_config": {"display_name": "参数能力复制清理稿", "local_only": True},
                    "parameter_config": draft["parameter_config"],
                },
            )
            assert renamed.status_code == 200, renamed.text
            response = client.delete(f"/api/v1/production/v1-model-profiles/{draft['id']}")
            assert response.status_code == 204, response.text


def test_0022_sqlite_upgrade_downgrade_and_reupgrade(tmp_path: Path) -> None:
    """0022 只增加能力字段，SQLite 往返不会遗留 schema 变化。"""

    server_root = Path(__file__).resolve().parents[1]
    database_url = f"sqlite:///{tmp_path / 'model-parameters.db'}"
    config = Config(str(server_root / "alembic.ini"))
    config.set_main_option("script_location", str(server_root / "migrations"))
    def migrate(action: str, revision: str) -> None:
        migration_engine = create_engine(database_url)
        try:
            with migration_engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()
                config.attributes["connection"] = connection
                getattr(command, action)(config, revision)
                config.attributes.pop("connection", None)
        finally:
            migration_engine.dispose()

    migrate("upgrade", "0021_commerce_story_run_rerun")
    engine = create_engine(database_url)
    try:
        # 0001 的历史动态 ORM 元数据在当前代码下会提前带入新列；真实 0021
        # 持久库没有该列。这里明确模拟既有 0021 结构，再验证 0022 的实际迁移。
        with engine.begin() as connection:
            if "parameter_config" in {column["name"] for column in inspect(connection).get_columns("model_profiles")}:
                connection.exec_driver_sql("ALTER TABLE model_profiles DROP COLUMN parameter_config")
        assert "parameter_config" not in {column["name"] for column in inspect(engine).get_columns("model_profiles")}
    finally:
        engine.dispose()

    migrate("upgrade", "head")
    engine = create_engine(database_url)
    try:
        columns = {column["name"]: column for column in inspect(engine).get_columns("model_profiles")}
        assert "parameter_config" in columns
        assert columns["parameter_config"]["nullable"] is False
    finally:
        engine.dispose()

    migrate("downgrade", "0021_commerce_story_run_rerun")
    engine = create_engine(database_url)
    try:
        assert "parameter_config" not in {column["name"] for column in inspect(engine).get_columns("model_profiles")}
    finally:
        engine.dispose()

    migrate("upgrade", "head")
    engine = create_engine(database_url)
    try:
        assert "parameter_config" in {column["name"] for column in inspect(engine).get_columns("model_profiles")}
    finally:
        engine.dispose()
