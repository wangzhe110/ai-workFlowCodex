"""视频分析与创作模型的可替换适配接口。

业务服务只能依赖这里的标准输入输出。第三方中转站的鉴权、HTTP 协议、模型
名称与响应格式在本模块归一化，避免供应商细节渗入选题、故事或分镜流程。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
import struct
from typing import Any, Optional, Protocol
from urllib.parse import quote, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from app.services.video_audio_service import ExtractedVideoAudio
from app.services.video_frame_service import SampledVideoFrame
from app.core.config import settings
from app.services.sensitive_data import sanitize_error_summary
from app.services.storage import LocalImageReference


@dataclass(frozen=True)
class VideoAnalysisInput:
    """传给视频理解适配器的业务输入。

    `sampled_frames` 仅在真实视觉模型调用的内存中存在，数据库只保存最终的抽象
    分析结果和素材 ID，不保存原视频帧或将其返回给浏览器。
    """

    asset_id: str
    filename: str
    content_type: str
    sampled_frames: list[SampledVideoFrame] = field(default_factory=list)
    # 转写原文可能包含受著作权保护的表达，因此只允许在本次 Worker 内存中短暂
    # 传递给综合分析，不得持久化进 WorkflowStep.output_payload 或 API 响应。
    transcript_for_mechanism_analysis: Optional[str] = None


@dataclass(frozen=True)
class TranscriptionResult:
    """语音转写的内存结果。

    ``text`` 只在当前任务内用于分析语音钩子、情绪和节奏；任何调用方都不能把它
    直接写入数据库。``audio_seconds`` 用于记录非内容性的运行元数据。
    """

    text: str
    audio_seconds: float


class VideoAnalysisProvider(Protocol):
    """所有视频理解平台必须实现的最小能力。

    返回值必须是平台标准化结果，禁止将第三方原始响应透传到业务或前端。
    """

    provider_key: str
    model_key: str

    def analyze(
        self,
        request: VideoAnalysisInput,
        *,
        system_instruction: str = "",
        user_instruction: str = "",
    ) -> dict[str, Any]:
        """返回抽象创作特征，不返回可直接复刻原视频的具体表达。"""


class MockVideoAnalysisProvider:
    """开发期模拟模型。

    它使接口、状态机、前端和结果数据在没有 API Key 时仍可联调。真实供应商
    接入后以相同接口替换本类，不应修改工作流服务。
    """

    provider_key = "mock_provider"
    model_key = "mock-video-understanding-v1"

    def analyze(
        self,
        request: VideoAnalysisInput,
        *,
        system_instruction: str = "",
        user_instruction: str = "",
    ) -> dict[str, Any]:
        """生成固定但符合最终契约的示例结果。"""

        return {
            "summary": "示例分析：视频以高信息密度开场，通过角色目标与阻碍建立冲突，并在段落末保留悬念。",
            "opening_mechanism": ["前 3 秒给出异常事件", "明确人物即时目标", "用未解问题制造继续观看动机"],
            "viral_elements": ["明确冲突", "身份反差", "段末悬念", "短句节奏"],
            "pacing_notes": "建议原创作品在每 8 至 15 秒推进一次信息或关系变化，避免逐镜复刻参考内容。",
            "compliance_note": "本结果为抽象创作机制，不包含或建议复用原视频的具体人物、台词、画面、音乐或镜头素材。",
            "source": {"asset_id": request.asset_id, "filename": request.filename},
        }


class OpenAICompatibleVisionAnalysisProvider:
    """以抽样视频帧调用 OpenAI 兼容视觉模型的抽象机制分析适配器。

    视频文件不会直接传给中转站：Worker 先在本地抽取少量 JPEG 帧，再以标准
    多模态 Chat Completions 内容发送给用户配置的视觉模型。模型输出只保留开头
    机制、冲突、节奏等抽象创作规律，明确禁止复述人物、台词、画面、音乐或情节。
    """

    provider_key = "openai_compatible_vision"

    def __init__(self, model_profile_snapshot: dict[str, Any]) -> None:
        """从任务冻结的模型配置创建客户端，避免运行中切换配置影响结果。"""

        self.model_key = str(model_profile_snapshot["model_key"])
        self.provider_config = model_profile_snapshot.get("provider_config") or {}

    def analyze(
        self,
        request: VideoAnalysisInput,
        *,
        system_instruction: str = "",
        user_instruction: str = "",
    ) -> dict[str, Any]:
        """发送采样帧并严格归一化成平台的合规分析契约。"""

        if not request.sampled_frames:
            raise RuntimeError("真实视频分析缺少采样帧")
        api_key = self._api_key()
        # 旧流程仍使用精简的“创作机制分析”契约；V1 配置 ``result_contract`` 后，
        # 同一视觉协议会返回完整的可审核创作简报。这样业务层不需要知道模型名称，
        # 历史接口的返回结构也不会被破坏。
        v1_contract = self.provider_config.get("result_contract") == "V1_REFERENCE_ANALYSIS"
        output_contract = (
            '{"video_script_structure":{"theme":"string","structure":["string"]},'
            '"opening_analysis":{"time_window":"string","hook_type":"string","mechanism":"string"},'
            '"viral_elements":[{"type":"string","description":"string"}],'
            '"scene_analysis":[{"role":"string","visual_style":"string"}],'
            '"creative_brief":{"originality_rule":"string","recommended_rhythm":"string","target_format":"string"}}'
            if v1_contract
            else '{"summary":"string","opening_mechanism":["string"],'
            '"viral_elements":["string"],"pacing_notes":"string",'
            '"compliance_note":"string"}'
        )
        task_description = (
            "输出视频脚本结构、爆款开头、爆款元素、场景分析和创作简报。"
            "所有内容必须是抽象机制，供原创故事使用。"
            if v1_contract
            else "分析用户有权使用的参考短视频，提炼可迁移的创作机制。"
        )
        user_content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"{user_instruction}\n\n"
                    "请严格只返回合法 JSON，不要使用 Markdown 代码块。\n"
                    f"任务：{task_description}\n"
                    "合规要求：只分析开头信息释放、冲突类型、节奏、情绪曲线、镜头功能；"
                    "不得复述或建议复用原视频的人物身份、外貌、台词、屏幕文字、音乐、"
                    "具体画面、情节或镜头素材。不得识别真实人物。\n"
                    f"输出契约：{output_contract}\n"
                    f"素材元数据：{json.dumps({'asset_id': request.asset_id, 'filename': request.filename, 'content_type': request.content_type}, ensure_ascii=False)}\n"
                    "以下是按时间采样的画面帧："
                ),
            }
        ]
        for frame in request.sampled_frames:
            user_content.append(
                {
                    "type": "text",
                    "text": f"采样时间：{frame.timestamp_seconds:.3f} 秒",
                }
            )
            user_content.append(
                {"type": "image_url", "image_url": {"url": frame.data_url}},
            )
        if request.transcript_for_mechanism_analysis:
            user_content.append(
                {
                    "type": "text",
                    "text": (
                        "以下是仅供本次分析的语音转写内容。只可用于判断开场信息密度、"
                        "情绪、语速和悬念功能；不得复述、引用、保存或输出其中的具体语句：\n"
                        + request.transcript_for_mechanism_analysis
                    ),
                }
            )

        payload: dict[str, Any] = {
            "model": self.model_key,
            "messages": [
                {
                    "role": "system",
                    "content": system_instruction,
                },
                {"role": "user", "content": user_content},
            ],
            "temperature": self.provider_config.get("temperature", 0.2),
            "response_format": {"type": "json_object"},
        }
        if "max_tokens" in self.provider_config:
            payload["max_tokens"] = self.provider_config["max_tokens"]
        options = self.provider_config.get("vision_request_options", {})
        if not isinstance(options, dict):
            raise RuntimeError("vision_request_options 必须为 JSON 对象")
        reserved = {"model", "messages", "temperature", "response_format", "max_tokens"}
        if reserved.intersection(options):
            raise RuntimeError("vision_request_options 不能覆盖模型、消息或 JSON 输出参数")
        payload.update(options)

        response_payload = _post_json(
            _chat_completions_url(str(self.provider_config.get("api_base_url", ""))),
            api_key,
            payload,
            _request_timeout_seconds(self.provider_config),
        )
        try:
            content = response_payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("视觉模型响应缺少 choices[0].message.content") from exc
        if not isinstance(content, str):
            raise RuntimeError("视觉模型返回的内容不是文本 JSON")
        parsed = _parse_model_json(content)
        if v1_contract:
            return _normalize_v1_reference_analysis(parsed)
        return _normalize_reference_analysis(parsed, request)

    def _api_key(self) -> str:
        """从部署环境读取密钥；不把密钥写入模型配置或日志。"""

        api_base_url = self.provider_config.get("api_base_url")
        secret_env_name = self.provider_config.get("secret_env_name")
        if not isinstance(api_base_url, str) or not api_base_url.startswith("https://"):
            raise RuntimeError("视觉模型配置需要 https:// api_base_url")
        if not isinstance(secret_env_name, str) or not secret_env_name.strip():
            raise RuntimeError("视觉模型配置缺少 secret_env_name")
        api_key = os.getenv(secret_env_name)
        if not api_key:
            raise RuntimeError(f"服务器环境变量 {secret_env_name} 未设置")
        return api_key


class OpenAICompatibleTranscriptionProvider:
    """OpenAI 兼容音频转写适配器。

    此适配器只负责将 Worker 已提取的受限 MP3 转成内存文本。它不做内容分析，
    不保存原文，也不将原始供应商响应透传到业务层。后续要更换 ASR 服务时，只需
    新增同一契约的适配器并为该步骤启用另一版模型配置。
    """

    provider_key = "openai_compatible_transcription"

    def __init__(self, model_profile_snapshot: dict[str, Any]) -> None:
        """从冻结配置初始化，确保排队任务不会受之后配置切换影响。"""

        self.model_key = str(model_profile_snapshot["model_key"])
        self.provider_config = model_profile_snapshot.get("provider_config") or {}

    def transcribe(self, audio: ExtractedVideoAudio) -> TranscriptionResult:
        """调用标准 ``audio/transcriptions`` 接口并只返回受限文本结果。"""

        options = self.provider_config.get("transcription_request_options", {})
        if not isinstance(options, dict):
            raise RuntimeError("transcription_request_options 必须为 JSON 对象")
        reserved = {"file", "model", "response_format"}
        if reserved.intersection(options):
            raise RuntimeError("transcription_request_options 不能覆盖 file、model 或 response_format")
        fields: dict[str, Any] = {
            "model": self.model_key,
            # 统一要求 JSON，才能稳定读取 text 字段并避免供应商返回原文日志。
            "response_format": "json",
            **options,
        }
        response_payload = _post_multipart(
            url=_audio_transcriptions_url(self._api_base_url()),
            api_key=self._api_key(),
            fields=fields,
            file_field="file",
            filename=audio.filename,
            content_type=audio.content_type,
            file_bytes=audio.data,
            timeout=_request_timeout_seconds(self.provider_config),
        )
        text = response_payload.get("text")
        if not isinstance(text, str):
            raise RuntimeError("语音转写响应缺少 text 字段")
        # 长度门限控制第三方异常输出和后续多模态请求体积，而不是试图保留完整台词。
        return TranscriptionResult(text=text.strip()[:12000], audio_seconds=audio.duration_seconds)

    def _api_base_url(self) -> str:
        """读取并验证转写端点的 HTTPS 基础地址。"""

        api_base_url = self.provider_config.get("api_base_url")
        if not isinstance(api_base_url, str) or not api_base_url.startswith("https://"):
            raise RuntimeError("语音转写配置需要 https:// api_base_url")
        return api_base_url

    def _api_key(self) -> str:
        """只从部署环境读取 Key，避免密钥进入模型配置和任务记录。"""

        secret_env_name = self.provider_config.get("secret_env_name")
        if not isinstance(secret_env_name, str) or not secret_env_name.strip():
            raise RuntimeError("语音转写配置缺少 secret_env_name")
        api_key = os.getenv(secret_env_name)
        if not api_key:
            raise RuntimeError(f"服务器环境变量 {secret_env_name} 未设置")
        return api_key


def _normalize_reference_analysis(result: Any, request: VideoAnalysisInput) -> dict[str, Any]:
    """校验视觉模型 JSON 并移除未定义字段，防止原始响应渗入业务数据。"""

    if not isinstance(result, dict):
        raise RuntimeError("视觉模型必须返回 JSON 对象")
    text_fields = ("summary", "pacing_notes", "compliance_note")
    list_fields = ("opening_mechanism", "viral_elements")
    normalized: dict[str, Any] = {}
    for field_name in text_fields:
        value = result.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"视觉模型结果缺少有效字段：{field_name}")
        normalized[field_name] = value.strip()[:2000]
    for field_name in list_fields:
        value = result.get(field_name)
        if not isinstance(value, list):
            raise RuntimeError(f"视觉模型结果缺少数组字段：{field_name}")
        items = [item.strip()[:500] for item in value if isinstance(item, str) and item.strip()]
        if not items:
            raise RuntimeError(f"视觉模型结果字段不能为空：{field_name}")
        normalized[field_name] = items[:12]
    normalized["source"] = {
        "asset_id": request.asset_id,
        "filename": request.filename,
        "sampled_frame_timestamps": [frame.timestamp_seconds for frame in request.sampled_frames],
        "audio_mechanism_considered": bool(request.transcript_for_mechanism_analysis),
    }
    return normalized


def _normalize_v1_reference_analysis(result: Any) -> dict[str, Any]:
    """校验 V1 创作简报的五类结果，拒绝不完整或未经结构化的视觉模型输出。"""

    if not isinstance(result, dict):
        raise RuntimeError("V1 视觉模型必须返回 JSON 对象")
    structure = result.get("video_script_structure")
    opening = result.get("opening_analysis")
    viral_elements = result.get("viral_elements")
    scene_analysis = result.get("scene_analysis")
    creative_brief = result.get("creative_brief")
    if not isinstance(structure, dict) or not isinstance(opening, dict) or not isinstance(creative_brief, dict):
        raise RuntimeError("V1 视频分析缺少结构、开头或创作简报对象")
    for field_name, value in (
        ("video_script_structure.theme", structure.get("theme")),
        ("opening_analysis.time_window", opening.get("time_window")),
        ("opening_analysis.hook_type", opening.get("hook_type")),
        ("opening_analysis.mechanism", opening.get("mechanism")),
        ("creative_brief.originality_rule", creative_brief.get("originality_rule")),
        ("creative_brief.recommended_rhythm", creative_brief.get("recommended_rhythm")),
        ("creative_brief.target_format", creative_brief.get("target_format")),
    ):
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"V1 视频分析缺少有效字段：{field_name}")
    sequence = structure.get("structure")
    if not isinstance(sequence, list) or not any(isinstance(item, str) and item.strip() for item in sequence):
        raise RuntimeError("V1 视频分析的 video_script_structure.structure 必须是非空字符串数组")
    if not isinstance(viral_elements, list) or not viral_elements:
        raise RuntimeError("V1 视频分析的 viral_elements 必须是非空数组")
    if not isinstance(scene_analysis, list) or not scene_analysis:
        raise RuntimeError("V1 视频分析的 scene_analysis 必须是非空数组")

    def clean_object(value: Any, fields: tuple[str, ...], label: str) -> dict[str, str]:
        if not isinstance(value, dict):
            raise RuntimeError(f"V1 视频分析的 {label} 项必须是对象")
        normalized: dict[str, str] = {}
        for field_name in fields:
            field_value = value.get(field_name)
            if not isinstance(field_value, str) or not field_value.strip():
                raise RuntimeError(f"V1 视频分析的 {label}.{field_name} 必须是非空文本")
            normalized[field_name] = field_value.strip()[:1200]
        return normalized

    return {
        "video_script_structure": {
            "theme": structure["theme"].strip()[:1200],
            "structure": [item.strip()[:800] for item in sequence if isinstance(item, str) and item.strip()][:12],
        },
        "opening_analysis": {
            "time_window": opening["time_window"].strip()[:200],
            "hook_type": opening["hook_type"].strip()[:400],
            "mechanism": opening["mechanism"].strip()[:1200],
        },
        "viral_elements": [
            clean_object(item, ("type", "description"), "viral_elements") for item in viral_elements[:12]
        ],
        "scene_analysis": [
            clean_object(item, ("role", "visual_style"), "scene_analysis") for item in scene_analysis[:12]
        ],
        "creative_brief": {
            "originality_rule": creative_brief["originality_rule"].strip()[:1600],
            "recommended_rhythm": creative_brief["recommended_rhythm"].strip()[:800],
            "target_format": creative_brief["target_format"].strip()[:200],
        },
    }


@dataclass(frozen=True)
class TopicGenerationInput:
    """选题模型只接收抽象分析、用户方向和已审核的创作资产快照。"""

    creative_direction: str
    analysis_snapshot: dict[str, Any]
    library_snapshot: list[dict[str, Any]]


class TopicGenerationProvider(Protocol):
    """选题供应商适配接口，返回原创候选而非参考视频的复刻方案。"""

    provider_key: str
    model_key: str

    def generate(self, request: TopicGenerationInput) -> list[dict[str, Any]]:
        """生成候选标题、开头、概要和辅助评分。"""


class MockTopicGenerationProvider:
    """Day 2 联调用模拟选题模型，真实模型仅替换此适配器。"""

    provider_key = "mock_provider"
    model_key = "mock-topic-generation-v1"

    def generate(self, request: TopicGenerationInput) -> list[dict[str, Any]]:
        """返回原创题材候选，避免沿用参考素材中的具体内容。"""

        direction = request.creative_direction or "都市情感"
        return [
            {
                "title": "倒计时里的陌生来电",
                "opening_hook": "女主在婚礼前十分钟接到一通电话：来电者准确说出了她从未告诉任何人的秘密。",
                "synopsis": f"围绕“{direction}”展开，主角为查明电话来源主动设定目标，并在每次接近真相时付出新的代价。",
                "score": 88,
                "scoring_notes": "开场冲突明确，人物目标清晰，适合用原创人物关系继续展开。",
            },
            {
                "title": "只剩一晚的合约室友",
                "opening_hook": "男主回家发现门锁被换，而新室友说：她只住一夜，却已经知道他所有的生活习惯。",
                "synopsis": "两人因一份临时合约被迫合作，在不断升级的误会与互相试探中发现各自隐藏的真实目标。",
                "score": 84,
                "scoring_notes": "身份反差与即时阻碍适合短剧节奏，后续可扩展多次反转。",
            },
            {
                "title": "失物招领处的第二个我",
                "opening_hook": "女主领回丢失的包，却在里面发现一张拍摄于明天的照片，照片中的她正站在案发现场。",
                "synopsis": "主角必须在有限时间内验证照片真伪，同时面对亲友关系中不断出现的矛盾线索。",
                "score": 82,
                "scoring_notes": "悬念强，适合每个段落推进一个新信息点。",
            },
        ]


@dataclass(frozen=True)
class StoryGenerationInput:
    """故事模型只消费用户确认的原创选题快照。"""

    topic: dict[str, Any]


class MockStoryGenerationProvider:
    """开发期故事适配器；真实文本模型必须输出同一标准故事包结构。"""

    provider_key = "mock_provider"
    model_key = "mock-story-generation-v1"

    def generate(self, request: StoryGenerationInput) -> dict[str, Any]:
        """生成原创大纲、角色和场景，不复用参考视频具体表达。"""

        return {
            "title": request.topic["title"],
            "premise": f"{request.topic['opening_hook']} 主角必须主动追查真相，并在代价不断升级前作出选择。",
            "outline": [
                {"act": "开端", "content": "异常事件发生，主角目标与首个阻碍同时出现。"},
                {"act": "升级", "content": "主角获得关键线索，却发现最信任的人可能隐瞒事实。"},
                {"act": "反转", "content": "线索指向新的真相，主角必须在关系与目标之间选择。"},
                {"act": "收束", "content": "主角完成代价明确的决定，并留下可延展的后续悬念。"},
            ],
            "roles": [
                {"name": "林知夏", "role": "主角", "goal": "查明异常事件真相", "conflict": "每条线索都要求她牺牲一段信任关系"},
                {"name": "周予安", "role": "关键关系人", "goal": "保护隐藏的事实", "conflict": "越想保护主角，越显得可疑"},
            ],
            "scenes": [
                {"name": "临时仪式现场", "purpose": "建立倒计时压力与异常事件"},
                {"name": "深夜便利店", "purpose": "主角获得第一条可验证线索"},
                {"name": "旧公寓走廊", "purpose": "关系冲突与关键反转发生"},
            ],
        }


class OpenAICompatibleJsonProvider:
    """OpenAI 兼容文本协议的 JSON 生成适配器。

    云雾等中转站可用同一实现接入。它只支持文本创作步骤，不把“OpenAI 兼容”
    错误地推广到视频理解、图片和图生视频等各自协议不同的能力。
    """

    provider_key = "openai_compatible"

    def __init__(self, model_profile_snapshot: dict[str, Any]) -> None:
        """从已冻结的非敏感配置创建客户端，不读取数据库中的实时配置。"""

        self.model_key = str(model_profile_snapshot["model_key"])
        self.provider_config = model_profile_snapshot.get("provider_config") or {}

    def generate_json(
        self,
        *,
        system_instruction: str,
        user_instruction: str | None = None,
        user_payload: dict[str, Any],
        output_contract: str,
    ) -> Any:
        """调用兼容 Chat Completions 接口，并将 Markdown 包裹的 JSON 规范化。"""

        api_base_url = self.provider_config.get("api_base_url")
        secret_env_name = self.provider_config.get("secret_env_name")
        if not isinstance(api_base_url, str) or not api_base_url.strip():
            raise RuntimeError("模型配置缺少 api_base_url")
        if not isinstance(secret_env_name, str) or not secret_env_name.strip():
            raise RuntimeError("模型配置缺少 secret_env_name")
        api_key = os.getenv(secret_env_name)
        if not api_key:
            raise RuntimeError(f"服务器环境变量 {secret_env_name} 未设置")

        payload: dict[str, Any] = {
            "model": self.model_key,
            "messages": [
                {"role": "system", "content": system_instruction},
                {
                    "role": "user",
                    "content": (
                        f"{user_instruction.strip()}\n\n" if user_instruction else ""
                    ) + (
                        "请严格只返回合法 JSON，不要使用 Markdown 代码块。\n"
                        f"输出契约：{output_contract}\n"
                        f"业务输入：{json.dumps(user_payload, ensure_ascii=False)}"
                    ),
                },
            ],
            "temperature": self.provider_config.get("temperature", 0.7),
            "response_format": {"type": "json_object"},
        }
        if "max_tokens" in self.provider_config:
            payload["max_tokens"] = self.provider_config["max_tokens"]

        response_payload = _post_json(
            _chat_completions_url(api_base_url),
            api_key,
            payload,
            _request_timeout_seconds(self.provider_config),
        )

        try:
            content = response_payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("中转站响应缺少 choices[0].message.content") from exc
        if not isinstance(content, str):
            raise RuntimeError("中转站返回的内容不是文本 JSON")
        return _parse_model_json(content)


class OpenAICompatibleImageProvider:
    """OpenAI 兼容图片生成协议的适配器。

    它与文本适配器共享鉴权、超时和错误处理，但调用独立的 images/generations
    端点。图片模型参数差异较大，因此扩展参数只从 `image_request_options` 读取。
    """

    provider_key = "openai_compatible_image"

    def __init__(self, model_profile_snapshot: dict[str, Any]) -> None:
        """从任务保存的配置快照初始化，确保同一批图片使用同一个模型版本。"""

        self.model_key = str(model_profile_snapshot["model_key"])
        self.provider_config = model_profile_snapshot.get("provider_config") or {}

    def generate(self, prompt: str, *, reference_image_urls: Optional[list[str]] = None) -> str:
        """生成一张图片，并归一化为可被前端直接使用的 URL 或 data URL。

        标准 OpenAI 图片接口没有统一的“参考图”字段，因此 V1 不会猜测某个供应商
        的私有参数。需要角色/场景参考生图时，模型配置可声明
        ``reference_image_field``（例如 ``images``），Adapter 才会把已锁定的图 URL
        写入该字段。未声明时只提交文本提示词，避免无声丢弃或错误扣费。
        """

        api_base_url = self.provider_config.get("api_base_url")
        secret_env_name = self.provider_config.get("secret_env_name")
        if not isinstance(api_base_url, str) or not api_base_url.strip():
            raise RuntimeError("模型配置缺少 api_base_url")
        if not isinstance(secret_env_name, str) or not secret_env_name.strip():
            raise RuntimeError("模型配置缺少 secret_env_name")
        api_key = os.getenv(secret_env_name)
        if not api_key:
            raise RuntimeError(f"服务器环境变量 {secret_env_name} 未设置")

        payload: dict[str, Any] = {
            "model": self.model_key,
            "prompt": prompt,
            "n": 1,
            "response_format": self.provider_config.get("image_response_format", "url"),
        }
        image_size = self.provider_config.get("image_size")
        if image_size:
            payload["size"] = image_size
        urls = reference_image_urls or []
        if not isinstance(urls, list) or not all(isinstance(item, str) and item for item in urls):
            raise RuntimeError("reference_image_urls 必须是非空字符串数组")
        reference_image_field = self.provider_config.get("reference_image_field")
        if urls:
            if reference_image_field is None:
                # 不是所有图片模型都支持参考图；这里显式拒绝以保证“锁图”确实参与
                # 后续生成，而不是在用户不知情的情况下退化成纯文生图。
                raise RuntimeError("当前图片模型未配置 reference_image_field，不能使用锁定参考图生成")
            if (
                not isinstance(reference_image_field, str)
                or not reference_image_field
                or not reference_image_field.replace("_", "").isalnum()
            ):
                raise RuntimeError("reference_image_field 必须由字母、数字或下划线组成")
            payload[reference_image_field] = urls
        options = self.provider_config.get("image_request_options", {})
        if not isinstance(options, dict):
            raise RuntimeError("image_request_options 必须为 JSON 对象")
        reserved = {"model", "prompt", "n", "response_format", "size"}
        if isinstance(reference_image_field, str) and reference_image_field:
            reserved.add(reference_image_field)
        if reserved.intersection(options):
            raise RuntimeError("image_request_options 不能覆盖 Adapter 管理的模型、提示词、输出或参考图字段")
        payload.update(options)

        response_payload = _post_json(
            _images_generations_url(api_base_url),
            api_key,
            payload,
            _request_timeout_seconds(self.provider_config),
        )
        try:
            item = response_payload["data"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("图片中转站响应缺少 data[0]") from exc
        if not isinstance(item, dict):
            raise RuntimeError("图片中转站返回的 data[0] 格式无效")
        image_url = item.get("url")
        if isinstance(image_url, str) and image_url:
            return image_url
        b64_json = item.get("b64_json")
        if isinstance(b64_json, str) and b64_json:
            return f"data:image/png;base64,{b64_json}"
        raise RuntimeError("图片中转站响应缺少 url 或 b64_json")


@dataclass(frozen=True)
class ImageTaskResult:
    """异步图片供应商任务的标准化状态。

    图片队列和视频队列一样会先返回 ``provider_task_id``。业务层把它落入
    ``ModelInvocation`` 后才进行轮询，Worker 重新执行时便能继续查询而不会再次
    提交已经可能扣费的请求。同步 OpenAI 兼容图片协议也用同一结构返回成功态，
    这样调用方无需依赖供应商协议分支。
    """

    provider_task_id: str | None
    status: str
    image_url: Optional[str] = None
    error_message: Optional[str] = None
    content_type: Optional[str] = None
    byte_size: Optional[int] = None
    sha256: Optional[str] = None
    # 方舟只返回会过期的签名 URL。图片二进制仅在当前 Worker 内存中短暂保存，
    # 交给资产存储后即丢弃，绝不能把该 URL 写入审计、错误或业务资产记录。
    image_bytes: bytes | None = field(default=None, repr=False)
    width: int | None = None
    height: int | None = None


class ImageGenerationProvider(Protocol):
    """图片任务的最小异步边界；不把 Fal 的响应结构泄漏到工作流。"""

    provider_key: str
    model_key: str

    def submit(self, prompt: str, *, reference_image_urls: Optional[list[str]] = None) -> ImageTaskResult:
        """提交一次图片任务，返回已标准化的任务状态。"""

    def poll(self, provider_task_id: str) -> ImageTaskResult:
        """查询一次已提交图片任务，绝不再次提交。"""


class FalQueueImageProvider:
    """云雾 Nano Banana 的最小 Fal 队列适配器。

    当前只实现 ``/fal-ai/nano-banana`` 的提交、状态查询、结果读取与单张图片
    下载校验，不把它扩展成通用 Fal SDK。鉴权请求永远发往 ``https://yunwu.ai``；
    返回的 Fal 队列路径会被换源回云雾，最终图片下载则不携带模型鉴权头。
    """

    provider_key = "fal_queue_image"
    _ORIGIN = "https://yunwu.ai"
    _TASK_PREFIX = "/fal-ai/nano-banana/requests/"
    _SUBMIT_PATH = "/fal-ai/nano-banana"

    def __init__(self, model_profile_snapshot: dict[str, Any]) -> None:
        self.model_key = str(model_profile_snapshot["model_key"])
        self.provider_config = model_profile_snapshot.get("provider_config") or {}
        self._task_paths: dict[str, tuple[str, str]] = {}

    def submit(self, prompt: str, *, reference_image_urls: Optional[list[str]] = None) -> ImageTaskResult:
        """向云雾提交一次 Nano Banana 队列任务，并只接受稳定的 request_id。"""

        if not isinstance(prompt, str) or not prompt.strip():
            raise RuntimeError("图片生成提示词不能为空")
        payload: dict[str, Any] = {"prompt": prompt.strip()}
        references = reference_image_urls or []
        if not isinstance(references, list) or not all(isinstance(item, str) and item for item in references):
            raise RuntimeError("reference_image_urls 必须是非空字符串数组")
        if references:
            for value in references:
                self._require_https(value, "参考图地址")
            # Nano Banana 队列协议只接受这一项固定字段，避免让业务层猜测 Fal 参数。
            payload["image_urls"] = references

        response = _post_json(
            self._endpoint(self._SUBMIT_PATH),
            self._api_key(),
            payload,
            _request_timeout_seconds(self.provider_config),
        )
        task_id = self._task_id(response)
        status_path = self._fal_path(response.get("status_url"), self._status_path(task_id))
        result_path = self._fal_path(response.get("response_url"), self._result_path(task_id))
        self._task_paths[task_id] = (status_path, result_path)
        return self._result_from_status(response, task_id=task_id, result_path=result_path)

    def poll(self, provider_task_id: str) -> ImageTaskResult:
        """查询已存在的任务；恢复执行时按 request_id 使用固定云雾队列路径。"""

        task_id = self._require_task_id(provider_task_id)
        status_path, result_path = self._task_paths.get(
            task_id,
            (self._status_path(task_id), self._result_path(task_id)),
        )
        response = _get_json(
            self._endpoint(status_path),
            self._api_key(),
            _request_timeout_seconds(self.provider_config),
        )
        return self._result_from_status(response, task_id=task_id, result_path=result_path)

    def _result_from_status(
        self,
        payload: dict[str, Any],
        *,
        task_id: str,
        result_path: str,
    ) -> ImageTaskResult:
        state = self._state(payload)
        if state == "PENDING":
            return ImageTaskResult(provider_task_id=task_id, status="PENDING")
        if state == "FAILED":
            return ImageTaskResult(
                provider_task_id=task_id,
                status="FAILED",
                error_message=self._error_message(payload),
            )
        result_payload = _get_json(
            self._endpoint(result_path),
            self._api_key(),
            _request_timeout_seconds(self.provider_config),
        )
        image_url = self._image_url(result_payload)
        content_type, byte_size, digest = self._download_and_validate(image_url)
        return ImageTaskResult(
            provider_task_id=task_id,
            status="SUCCEEDED",
            image_url=image_url,
            content_type=content_type,
            byte_size=byte_size,
            sha256=digest,
        )

    def _endpoint(self, path: str) -> str:
        """仅把 Authorization 发送到云雾固定 HTTPS 域名。"""

        base = self.provider_config.get("api_base_url")
        if not isinstance(base, str):
            raise RuntimeError("Fal 图片模型配置缺少 api_base_url")
        parts = urlsplit(base)
        if (
            parts.scheme != "https"
            or parts.netloc != "yunwu.ai"
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise RuntimeError("Fal 图片模型 api_base_url 必须是 https://yunwu.ai")
        if not isinstance(path, str) or not path.startswith("/") or not path.startswith("/fal-ai/nano-banana"):
            raise RuntimeError("Fal 图片队列路径无效")
        return f"{self._ORIGIN}{path}"

    def _api_key(self) -> str:
        secret_env_name = self.provider_config.get("secret_env_name")
        if not isinstance(secret_env_name, str) or not secret_env_name.strip():
            raise RuntimeError("Fal 图片模型配置缺少 secret_env_name")
        api_key = os.getenv(secret_env_name)
        if not api_key:
            raise RuntimeError(f"服务器环境变量 {secret_env_name} 未设置")
        return api_key

    @classmethod
    def _require_task_id(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 255:
            raise RuntimeError("Fal 图片任务缺少有效 request_id")
        return value.strip()

    @classmethod
    def _task_id(cls, payload: dict[str, Any]) -> str:
        for key in ("request_id", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return cls._require_task_id(value)
        raise RuntimeError("Fal 图片提交响应缺少 request_id")

    @classmethod
    def _status_path(cls, task_id: str) -> str:
        return f"{cls._TASK_PREFIX}{quote(task_id, safe='')}/status"

    @classmethod
    def _result_path(cls, task_id: str) -> str:
        return f"{cls._TASK_PREFIX}{quote(task_id, safe='')}"

    @classmethod
    def _fal_path(cls, raw_url: Any, fallback: str) -> str:
        """把 Fal 返回地址限定为同一模型路径，并以云雾作为唯一鉴权源。"""

        if raw_url is None:
            return fallback
        if not isinstance(raw_url, str) or not raw_url.strip():
            raise RuntimeError("Fal 图片队列返回了无效状态地址")
        parts = urlsplit(raw_url)
        if parts.scheme != "https" or not parts.netloc or not parts.path.startswith(cls._TASK_PREFIX):
            raise RuntimeError("Fal 图片队列返回了不受支持的状态地址")
        # 绝不携带返回 URL 的 query，避免把可能的临时签名传播到审计或日志。
        return parts.path

    @staticmethod
    def _state(payload: dict[str, Any]) -> str:
        raw = payload.get("status", payload.get("state"))
        if not isinstance(raw, str) or not raw.strip():
            # 提交接口部分实现不会给初始状态；此时安全地当作待处理，而不是重复提交。
            return "PENDING"
        normalized = raw.strip().lower()
        if normalized in {"completed", "succeeded", "success", "done"}:
            return "SUCCEEDED"
        if normalized in {"failed", "error", "cancelled", "canceled"}:
            return "FAILED"
        return "PENDING"

    @staticmethod
    def _error_message(payload: dict[str, Any]) -> str:
        for value in (payload.get("error"), payload.get("detail"), payload.get("message")):
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
            if isinstance(value, dict):
                nested = value.get("message") or value.get("detail")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()[:500]
        return "Fal 图片任务失败"

    @classmethod
    def _image_url(cls, payload: dict[str, Any]) -> str:
        candidates = (
            (payload.get("images") or [{}])[0] if isinstance(payload.get("images"), list) and payload.get("images") else None,
            ((payload.get("data") or {}).get("images") or [{}])[0]
            if isinstance(payload.get("data"), dict) and isinstance((payload.get("data") or {}).get("images"), list) and (payload.get("data") or {}).get("images")
            else None,
            ((payload.get("output") or {}).get("images") or [{}])[0]
            if isinstance(payload.get("output"), dict) and isinstance((payload.get("output") or {}).get("images"), list) and (payload.get("output") or {}).get("images")
            else None,
            payload.get("image"),
        )
        for item in candidates:
            value = item.get("url") if isinstance(item, dict) else item
            if isinstance(value, str) and value.strip():
                cls._require_https(value, "Fal 图片结果地址")
                return value.strip()
        raise RuntimeError("Fal 图片结果缺少单张图片 URL")

    @staticmethod
    def _require_https(value: str, label: str) -> None:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.netloc:
            raise RuntimeError(f"{label}必须是 HTTPS 地址")

    @classmethod
    def _download_and_validate(cls, image_url: str) -> tuple[str, int, str]:
        """无鉴权下载一张图片，并同时校验 MIME、文件头和大小。"""

        cls._require_https(image_url, "Fal 图片结果地址")
        timeout = min(max(settings.generated_image_download_timeout_seconds, 1), 300)
        request = Request(image_url, headers={"Accept": "image/*"}, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                final_url = response.geturl()
                cls._require_https(final_url, "图片下载重定向地址")
                content_type = response.headers.get_content_type()
                if content_type not in {"image/png", "image/jpeg", "image/webp"}:
                    raise RuntimeError("Fal 图片下载响应 MIME 类型不受支持")
                length = response.headers.get("Content-Length")
                if length and int(length) > settings.generated_image_max_bytes:
                    raise RuntimeError("Fal 图片超过 GENERATED_IMAGE_MAX_BYTES 限制")
                content = response.read(settings.generated_image_max_bytes + 1)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("无法下载 Fal 图片结果") from exc
        if not content or len(content) > settings.generated_image_max_bytes:
            raise RuntimeError("Fal 图片为空或超过 GENERATED_IMAGE_MAX_BYTES 限制")
        detected = cls._image_mime_from_magic(content)
        if detected != content_type:
            raise RuntimeError("Fal 图片 MIME 类型与文件头不一致")
        return content_type, len(content), sha256(content).hexdigest()

    @staticmethod
    def _image_mime_from_magic(content: bytes) -> str:
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return "image/webp"
        raise RuntimeError("Fal 图片文件头不是受支持的 PNG、JPEG 或 WebP")


class VolcengineArkImageProvider:
    """火山方舟 Seedream 单图与本地参考图适配器。

    对纯文本请求仍维持官方 ``/api/v3/images/generations`` 的单图协议；参考图仅
    接受 :class:`LocalImageReference`，其 Data URL 只能由本地存储边界在 Worker
    内存中构造。临时 Data URL 与供应商结果 URL 都不会进入任何持久化对象。
    """

    provider_key = "volcengine_ark_image"
    _BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
    _SUPPORTED_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})

    def __init__(self, model_profile_snapshot: dict[str, Any]) -> None:
        self.model_key = str(model_profile_snapshot["model_key"])
        self.provider_config = model_profile_snapshot.get("provider_config") or {}

    def generate(
        self,
        prompt: str,
        *,
        reference_image_urls: Optional[list[str]] = None,
        reference_images: Optional[list[LocalImageReference]] = None,
    ) -> ImageTaskResult:
        """只提交一次 Seedream 文生图或本地参考图生图请求并下载结果进内存。

        图片 POST 不做自动重试。发生网络、协议、下载或解析错误时由工作流记录
        失败，操作者通过现有的“重新生成”动作新建一次明确的付费调用。
        """

        if not isinstance(prompt, str) or not prompt.strip():
            raise RuntimeError("图片生成提示词不能为空")
        legacy_references = reference_image_urls or []
        if not isinstance(legacy_references, list) or not all(
            isinstance(item, str) and item for item in legacy_references
        ):
            raise RuntimeError("reference_image_urls 必须是非空字符串数组")
        if legacy_references:
            # 不能让 127.0.0.1、Docker 地址、本机路径或任意用户 URL 进入方舟；图
            # 生图必须经 LocalAssetStorage 验证后成为 LocalImageReference。
            raise RuntimeError("方舟参考图必须使用经本地资产校验的 reference_images")
        references = self._validate_reference_images(reference_images or [])

        prompt_text = prompt.strip()
        if references:
            prompt_text = self._reference_aware_prompt(prompt_text, references)

        payload = {
            "model": self.model_key,
            "prompt": prompt_text,
            "size": self._config_value("size", "2K"),
            "sequential_image_generation": self._config_value("sequential_image_generation", "disabled"),
            "response_format": self._config_value("response_format", "url"),
            "watermark": self._config_value("watermark", False),
        }
        if len(references) == 1:
            payload["image"] = references[0].data_url
        elif references:
            payload["image"] = [reference.data_url for reference in references]
        response_payload = _post_json(
            self._endpoint(),
            self._api_key(),
            payload,
            _request_timeout_seconds(self.provider_config),
        )
        image_url = self._result_url(response_payload)
        content_type, content, width, height = self._download_and_validate(image_url)
        return ImageTaskResult(
            provider_task_id=None,
            status="SUCCEEDED",
            content_type=content_type,
            byte_size=len(content),
            sha256=sha256(content).hexdigest(),
            image_bytes=content,
            width=width,
            height=height,
        )

    @staticmethod
    def _validate_reference_images(references: list[LocalImageReference]) -> list[LocalImageReference]:
        """验证内存参考图数量、类型与稳定的角色→场景排序。"""

        if len(references) > 14:
            raise RuntimeError("方舟参考图最多允许 14 张")
        if not all(isinstance(item, LocalImageReference) for item in references):
            raise RuntimeError("方舟参考图必须由本地资产存储解析")
        seen_hashes: set[str] = set()
        normalized: list[LocalImageReference] = []
        for item in references:
            if item.role not in {"character", "scene"}:
                raise RuntimeError("方舟参考图角色只能是 character 或 scene")
            if item.sha256 in seen_hashes:
                continue
            seen_hashes.add(item.sha256)
            normalized.append(item)
        if any(item.role == "scene" for item in normalized):
            first_scene = next(index for index, item in enumerate(normalized) if item.role == "scene")
            if any(item.role != "scene" for item in normalized[first_scene:]):
                raise RuntimeError("方舟参考图必须按角色图在前、场景图在后的稳定顺序传入")
        return normalized

    @staticmethod
    def _reference_aware_prompt(prompt: str, references: list[LocalImageReference]) -> str:
        """明确规定当前 Run 1 中两类参考图的视觉职责。"""

        character_count = sum(item.role == "character" for item in references)
        scene_count = sum(item.role == "scene" for item in references)
        if len(references) == 2 and character_count == 1 and scene_count == 1:
            instruction = (
                "参考图1是角色参考图：严格保持其外貌、发型、服装和配色；"
                "参考图2是场景参考图：严格保持地点、布局、光线、色调与镜头方向。"
                "把参考图1中的角色自然置入参考图2的场景，生成一个连续的电影分镜关键帧。"
            )
        else:
            instruction = (
                f"前 {character_count} 张参考图是角色参考图，必须保持人物外观和服装；"
                f"后 {scene_count} 张参考图是场景参考图，必须保持环境、布局与光线。"
                "按照该固定顺序融合为连续的电影分镜关键帧。"
            )
        return f"{prompt}\n\n{instruction}"

    def _endpoint(self) -> str:
        configured = self.provider_config.get("api_base_url")
        if not isinstance(configured, str) or configured.rstrip("/") != self._BASE_URL:
            raise RuntimeError("方舟 Seedream 图片模型 API 地址必须为 https://ark.cn-beijing.volces.com/api/v3")
        # 不复用通用 OpenAI 地址拼接器，避免意外生成 /api/v3/v1/images/generations。
        return f"{configured.rstrip('/')}/images/generations"

    def _api_key(self) -> str:
        secret_env_name = self.provider_config.get("secret_env_name")
        if secret_env_name != "ARK_API_KEY":
            raise RuntimeError("方舟 Seedream 图片模型必须使用 ARK_API_KEY")
        api_key = os.getenv(secret_env_name)
        if not api_key:
            raise RuntimeError("服务器环境变量 ARK_API_KEY 未设置")
        return api_key

    def _config_value(self, field_name: str, default: Any) -> Any:
        value = self.provider_config.get(field_name, default)
        if field_name == "size" and value != "2K":
            raise RuntimeError("方舟 Seedream V1 图片尺寸必须为 2K")
        if field_name == "sequential_image_generation" and value != "disabled":
            raise RuntimeError("方舟 Seedream V1 必须关闭 sequential_image_generation")
        if field_name == "response_format" and value != "url":
            raise RuntimeError("方舟 Seedream V1 必须使用 response_format=url")
        if field_name == "watermark" and value is not False:
            raise RuntimeError("方舟 Seedream V1 必须设置 watermark=false")
        return value

    @staticmethod
    def _result_url(payload: dict[str, Any]) -> str:
        try:
            image_url = payload["data"][0]["url"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("方舟 Seedream 响应缺少 data[0].url") from exc
        if not isinstance(image_url, str) or not image_url.strip():
            raise RuntimeError("方舟 Seedream 返回的图片 URL 无效")
        VolcengineArkImageProvider._require_https(image_url, "方舟图片结果地址")
        return image_url.strip()

    @classmethod
    def _download_and_validate(cls, image_url: str) -> tuple[str, bytes, int, int]:
        """无鉴权下载临时图片，校验后只返回内存字节，不返回临时 URL。"""

        cls._require_https(image_url, "方舟图片结果地址")
        timeout = min(max(settings.generated_image_download_timeout_seconds, 1), 300)
        request = Request(image_url, headers={"Accept": "image/*"}, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                if response.getcode() != 200:
                    raise RuntimeError("方舟图片下载未返回 HTTP 200")
                cls._require_https(response.geturl(), "图片下载重定向地址")
                content_type = response.headers.get_content_type()
                if content_type not in cls._SUPPORTED_MIME_TYPES:
                    raise RuntimeError("方舟图片下载响应 MIME 类型不受支持")
                length = response.headers.get("Content-Length")
                if length and int(length) > settings.generated_image_max_bytes:
                    raise RuntimeError("方舟图片超过 GENERATED_IMAGE_MAX_BYTES 限制")
                content = response.read(settings.generated_image_max_bytes + 1)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("无法下载方舟 Seedream 图片结果") from exc
        if not content or len(content) > settings.generated_image_max_bytes:
            raise RuntimeError("方舟图片为空或超过 GENERATED_IMAGE_MAX_BYTES 限制")
        detected = cls._image_mime_from_magic(content)
        if detected != content_type:
            raise RuntimeError("方舟图片 MIME 类型与文件头不一致")
        width, height = cls._image_dimensions(content, detected)
        return content_type, content, width, height

    @staticmethod
    def _require_https(value: str, label: str) -> None:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.netloc:
            raise RuntimeError(f"{label}必须是 HTTPS 地址")

    @staticmethod
    def _image_mime_from_magic(content: bytes) -> str:
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return "image/webp"
        raise RuntimeError("方舟图片文件头不是受支持的 PNG、JPEG 或 WebP")

    @classmethod
    def _image_dimensions(cls, content: bytes, content_type: str) -> tuple[int, int]:
        if content_type == "image/png":
            if len(content) < 24 or content[12:16] != b"IHDR":
                raise RuntimeError("PNG 图片缺少 IHDR 尺寸信息")
            width, height = struct.unpack(">II", content[16:24])
        elif content_type == "image/jpeg":
            width, height = cls._jpeg_dimensions(content)
        else:
            width, height = cls._webp_dimensions(content)
        if width <= 0 or height <= 0:
            raise RuntimeError("图片尺寸无效")
        return width, height

    @staticmethod
    def _jpeg_dimensions(content: bytes) -> tuple[int, int]:
        cursor = 2
        sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while cursor + 9 <= len(content):
            if content[cursor] != 0xFF:
                cursor += 1
                continue
            while cursor < len(content) and content[cursor] == 0xFF:
                cursor += 1
            if cursor >= len(content):
                break
            marker = content[cursor]
            cursor += 1
            if marker in {0xD8, 0xD9}:
                continue
            if cursor + 2 > len(content):
                break
            length = int.from_bytes(content[cursor : cursor + 2], "big")
            if length < 2 or cursor + length > len(content):
                break
            if marker in sof_markers and length >= 7:
                return (
                    int.from_bytes(content[cursor + 5 : cursor + 7], "big"),
                    int.from_bytes(content[cursor + 3 : cursor + 5], "big"),
                )
            cursor += length
        raise RuntimeError("JPEG 图片缺少尺寸信息")

    @staticmethod
    def _webp_dimensions(content: bytes) -> tuple[int, int]:
        if len(content) < 30:
            raise RuntimeError("WebP 图片缺少尺寸信息")
        chunk = content[12:16]
        if chunk == b"VP8X":
            return (int.from_bytes(content[24:27], "little") + 1, int.from_bytes(content[27:30], "little") + 1)
        if chunk == b"VP8L" and len(content) >= 25 and content[20] == 0x2F:
            value = int.from_bytes(content[21:25], "little")
            return ((value & 0x3FFF) + 1, ((value >> 14) & 0x3FFF) + 1)
        if chunk == b"VP8 ":
            marker = content.find(b"\x9d\x01\x2a")
            if marker >= 0 and marker + 7 <= len(content):
                return (
                    int.from_bytes(content[marker + 3 : marker + 5], "little") & 0x3FFF,
                    int.from_bytes(content[marker + 5 : marker + 7], "little") & 0x3FFF,
                )
        raise RuntimeError("WebP 图片缺少尺寸信息")


def _chat_completions_url(api_base_url: str) -> str:
    """兼容填写根地址、/v1 地址或完整 chat/completions 地址三种配置方式。"""

    normalized = api_base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


def _images_generations_url(api_base_url: str) -> str:
    """兼容根地址、/v1 地址和完整 images/generations 地址三种配置方式。"""

    normalized = api_base_url.rstrip("/")
    if normalized.endswith("/images/generations"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/images/generations"
    return f"{normalized}/v1/images/generations"


def _audio_transcriptions_url(api_base_url: str) -> str:
    """兼容根地址、/v1 地址和完整 audio/transcriptions 地址。"""

    normalized = api_base_url.rstrip("/")
    if normalized.endswith("/audio/transcriptions"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/audio/transcriptions"
    return f"{normalized}/v1/audio/transcriptions"


def _configured_endpoint(
    provider_config: dict[str, Any],
    config_key: str,
    configured_path: Optional[str] = None,
) -> str:
    """根据基础地址和配置路径生成 HTTPS 端点。

    路径既可使用相对路径（推荐，便于迁移中转站），也可填写完整 HTTPS 地址。
    任务提交与任务查询都从这一处取地址，避免适配器悄悄写死供应商域名。
    """

    api_base_url = provider_config.get("api_base_url")
    if not isinstance(api_base_url, str) or not api_base_url.startswith("https://"):
        raise RuntimeError("视频模型配置需要 https:// api_base_url")
    raw_path = configured_path if configured_path is not None else provider_config.get(config_key)
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise RuntimeError(f"视频模型配置缺少 {config_key}")
    if raw_path.startswith("https://"):
        return raw_path
    if raw_path.startswith("http://"):
        raise RuntimeError(f"{config_key} 必须使用 HTTPS 地址")
    return f"{api_base_url.rstrip('/')}/{raw_path.lstrip('/')}"


def _read_json_path(payload: dict[str, Any], path: Any) -> Any:
    """读取简单点分 JSON 路径，路径不存在时返回 None。

    配置中心只需要覆盖常见 ``id``、``state``、``video.url`` 等结构；不支持
    数组索引或表达式，以免配置演变为不可审计的脚本执行入口。
    """

    if not isinstance(path, str) or not path:
        raise RuntimeError("JSON 路径必须是非空字符串")
    current: Any = payload
    for part in path.split("."):
        if not part or not part.replace("_", "").isalnum():
            raise RuntimeError(f"JSON 路径格式无效：{path}")
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _post_json(url: str, api_key: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """发送带 Bearer 鉴权的 JSON 请求，且绝不记录密钥或完整请求头。"""

    return _authorized_json_request(
        url=url,
        api_key=api_key,
        method="POST",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=timeout,
    )


class ProviderTransportError(RuntimeError):
    """供应商 HTTP 传输暂时不可用，消息不包含 URL、请求头或鉴权信息。"""


class ProviderPollNetworkError(ProviderTransportError):
    """已创建供应商任务后的 GET 轮询暂时失败，可安全恢复同一任务号。"""


def _post_multipart(
    *,
    url: str,
    api_key: str,
    fields: dict[str, Any],
    file_field: str,
    filename: str,
    content_type: str,
    file_bytes: bytes,
    timeout: float,
) -> dict[str, Any]:
    """提交受限音频文件的 multipart 请求，并复用统一脱敏错误语义。

    标准库没有高层 multipart client，因此在此显式组装固定结构。字段名由适配器
    控制，文件名来自临时提取文件，二进制不会写入日志或工作流记录。
    """

    boundary = f"----aiDramaBoundary{uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("ascii"),
            (
                f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8"),
            file_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode("ascii"),
        ]
    )
    request = Request(
        url,
        data=b"".join(chunks),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        error_body = sanitize_error_summary(exc.read().decode("utf-8", errors="replace"), max_length=500)
        raise RuntimeError(f"语音转写请求失败（HTTP {exc.code}）：{error_body}") from exc
    except URLError as exc:
        raise RuntimeError("无法连接语音转写服务，请检查 api_base_url 或网络") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("语音转写服务返回的不是 JSON 响应") from exc
    if not isinstance(response_payload, dict):
        raise RuntimeError("语音转写服务返回的 JSON 顶层必须是对象")
    return response_payload


def _get_json(url: str, api_key: str, timeout: float) -> dict[str, Any]:
    """发送视频任务查询请求，复用与提交请求相同的脱敏错误处理。"""

    return _authorized_json_request(
        url=url,
        api_key=api_key,
        method="GET",
        data=None,
        timeout=timeout,
    )


def _authorized_json_request(
    *,
    url: str,
    api_key: str,
    method: str,
    data: Optional[bytes],
    timeout: float,
) -> dict[str, Any]:
    """执行受鉴权的 JSON HTTP 请求，不记录密钥、请求头或完整业务提示词。"""

    request = Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        error_body = sanitize_error_summary(exc.read().decode("utf-8", errors="replace"), max_length=500)
        raise RuntimeError(f"供应商请求失败（HTTP {exc.code}）：{error_body}") from exc
    except (URLError, OSError) as exc:
        # 不在异常内携带底层 URL、请求头或系统网络信息。调用方根据是在创建还是
        # 轮询阶段进一步投影为安全、可恢复的领域错误码。
        raise ProviderTransportError("供应商接口暂时无法连接，请检查网络后重试") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("供应商返回的不是 JSON 响应") from exc
    if not isinstance(response_payload, dict):
        raise RuntimeError("供应商返回的 JSON 顶层必须是对象")
    return response_payload


def _request_timeout_seconds(provider_config: dict[str, Any]) -> float:
    """读取可调超时并限制在安全范围，避免单个模型任务无限占用 Worker。"""

    raw_timeout = provider_config.get("timeout_seconds", 90)
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("timeout_seconds 必须为数字") from exc
    return min(max(timeout, 1), 1800)


def _parse_model_json(content: str) -> Any:
    """解析模型可能带有 Markdown 围栏的 JSON，并把格式错误转为可见任务失败。"""

    normalized = content.strip()
    if normalized.startswith("```"):
        normalized = normalized.split("\n", 1)[1] if "\n" in normalized else ""
        if normalized.endswith("```"):
            normalized = normalized[:-3].strip()
    try:
        return json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise RuntimeError("模型未按约定返回合法 JSON") from exc

@dataclass(frozen=True)
class StoryboardGenerationInput:
    story: dict[str, Any]; shot_count: int

class MockStoryboardGenerationProvider:
    """分镜模型适配器；返回平台统一的镜头字段而非供应商原始响应。"""
    provider_key = "mock_provider"; model_key = "mock-storyboard-generation-v1"
    def generate(self, request: StoryboardGenerationInput) -> list[dict[str, Any]]:
        """按用户指定的数量生成原创分镜细纲。"""
        scenes = request.story["scenes"]
        return [{"number": index, "duration_seconds": 3, "scene": scenes[(index - 1) % len(scenes)]["name"], "visual": f"镜头 {index}：围绕主角目标推进原创情节与关系变化。", "dialogue_or_voiceover": "用简短信息推进冲突。", "camera": "中近景，随情绪变化切换", "image_prompt": f"原创短剧分镜，{scenes[(index - 1) % len(scenes)]['name']}，人物关系紧张，电影感", "video_prompt": "角色动作克制，镜头平稳推进，保持角色和场景一致性"} for index in range(1, request.shot_count + 1)]


@dataclass(frozen=True)
class VideoGenerationInput:
    """单个视频片段适配器的标准输入。

    `image_urls` 已由服务层选定为每镜最新成功版本；接入任意中转站时，只需将本
    结构映射为该站的请求，不得让业务层依赖其私有字段或 API Key。
    """

    project_id: str
    group_number: int
    start_shot_number: int
    end_shot_number: int
    prompt: str
    image_urls: list[str]
    # 单机部署的本地关键帧不能由方舟云端直接读取。Commerce Worker 可在调用期间
    # 传入经过 LocalAssetStorage 校验的内存参考图；该对象不允许进入任何 JSON
    # 快照、日志或持久化模型。
    reference_images: list[LocalImageReference] = field(default_factory=list)


class VideoGenerationProvider(Protocol):
    """所有图生视频供应商应实现的异步片段渲染接口。

    图生视频普遍是异步任务：提交时通常只返回任务号，随后再轮询视频地址。因此
    业务服务不能假定一次 HTTP 请求就能拿到 MP4，也不能依赖任一中转站的状态值。
    """

    provider_key: str
    model_key: str

    def submit(self, request: VideoGenerationInput) -> "VideoTaskResult":
        """提交一个片段任务，返回标准化任务状态。"""

    def poll(self, provider_task_id: str) -> "VideoTaskResult":
        """查询一个已提交任务，返回 PENDING、SUCCEEDED 或 FAILED。"""


@dataclass(frozen=True)
class VideoTaskResult:
    """供应商任务的标准化快照。

    `status` 只允许使用平台内部的三种片段状态，供应商的 pending、processing、
    completed、failed 等原始值由适配器消化。`video_url` 只在成功时存在。
    """

    provider_task_id: str
    status: str
    video_url: Optional[str] = None
    error_message: Optional[str] = None


class MockVideoGenerationProvider:
    """无 Key 本地联调用的视频生成适配器。

    开发期只返回 `mock://` 地址而不假装产出真实 MP4；上线时替换为对应中转站
    适配器，并将模型 Key、超时、回调策略放入该步骤的配置快照。
    """

    provider_key = "mock_provider"
    model_key = "mock-video-generation-v1"

    def submit(self, request: VideoGenerationInput) -> VideoTaskResult:
        """立即返回成功的模拟结果，供端到端状态、版本与分组联调。"""

        task_id = f"mock-video-{request.project_id[:8]}-group-{request.group_number}"
        return VideoTaskResult(
            provider_task_id=task_id,
            status="SUCCEEDED",
            video_url=f"mock://video/{task_id}",
        )

    def poll(self, provider_task_id: str) -> VideoTaskResult:
        """模拟任务提交后已经完成；保留轮询方法以遵守统一异步接口。"""

        return VideoTaskResult(
            provider_task_id=provider_task_id,
            status="SUCCEEDED",
            video_url=f"mock://video/{provider_task_id}",
        )


class VolcengineArkVideoProvider:
    """火山方舟 Seedance 原生图生视频适配器。

    这不是让用户填写通用中转站路径的配置模板，而是按火山方舟 SDK 的固定协议封装：
    ``POST /contents/generations/tasks`` 创建任务，随后 ``GET`` 同一路径加任务号查询。
    因此制作人员只需选择已经开通的模型、画幅和时长；方舟地址、请求结构、首帧字段
    与状态映射均由代码托管，避免把付费视频接口参数暴露给非技术用户。
    """

    provider_key = "volcengine_ark_video"
    _BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"

    def __init__(self, model_profile_snapshot: dict[str, Any]) -> None:
        self.model_key = str(model_profile_snapshot["model_key"])
        self.provider_config = model_profile_snapshot.get("provider_config") or {}

    def submit(self, request: VideoGenerationInput) -> VideoTaskResult:
        """以视频提示词和首帧图片创建方舟异步视频任务。"""

        first_frame_url = self._first_frame_url(request)
        content: list[dict[str, Any]] = [
            {"type": "text", "text": request.prompt},
            {
                "type": "image_url",
                "image_url": {"url": first_frame_url},
                "role": "first_frame",
            },
        ]
        if self.provider_config.get("use_last_frame") and len(request.image_urls) > 1:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": self._public_image_url(request.image_urls[-1])},
                    "role": "last_frame",
                }
            )

        payload: dict[str, Any] = {"model": self.model_key, "content": content}
        # Seedance 对含 ``first_frame`` 的图生视频从首帧本身推导画幅。真实方舟会
        # 拒绝同发 ``ratio``（InvalidParameter.TaskTypeConstraint），因此此处只
        # 映射其余固定生产参数。保留 Profile 中的 ratio 是为了历史快照兼容，不能
        # 把它作为首帧图生视频请求字段发给供应商。
        for option in ("duration", "resolution", "generate_audio", "watermark", "return_last_frame", "seed"):
            value = self.provider_config.get(option)
            if value is not None:
                payload[option] = value

        response_payload = _post_json(
            self._endpoint(),
            self._api_key(),
            payload,
            _request_timeout_seconds(self.provider_config),
        )
        task_id = response_payload.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise RuntimeError("火山方舟创建视频任务后没有返回任务 ID")
        # 官方创建接口只保证返回任务 ID；实际结果统一由轮询接口确认。
        return VideoTaskResult(provider_task_id=task_id, status="PENDING")

    def poll(self, provider_task_id: str) -> VideoTaskResult:
        """读取方舟任务状态，并转换为平台统一状态。"""

        if not provider_task_id:
            raise RuntimeError("视频任务缺少火山方舟任务 ID")
        try:
            response_payload = _get_json(
                f"{self._BASE_URL}/contents/generations/tasks/{quote(provider_task_id, safe='')}",
                self._api_key(),
                _request_timeout_seconds(self.provider_config),
            )
        except ProviderTransportError as exc:
            # 已有 task ID 的 GET 网络波动不等同于供应商已失败，更不允许调用方
            # 因此再次 POST。上层可保留任务号，并在人工恢复时只继续查询同一任务。
            raise ProviderPollNetworkError(
                "供应商任务轮询暂时无法连接，可使用已保存的任务号恢复查询"
            ) from exc
        raw_status = response_payload.get("status")
        if not isinstance(raw_status, str) or not raw_status:
            raise RuntimeError("火山方舟任务查询响应缺少 status")
        normalized_status = raw_status.strip().lower()
        returned_task_id = response_payload.get("id")
        stable_task_id = returned_task_id if isinstance(returned_task_id, str) and returned_task_id else provider_task_id
        if normalized_status == "succeeded":
            content = response_payload.get("content")
            video_url = content.get("video_url") if isinstance(content, dict) else None
            if not isinstance(video_url, str) or not video_url.startswith(("https://", "http://")):
                raise RuntimeError("火山方舟任务已成功但未返回可访问的视频地址")
            return VideoTaskResult(provider_task_id=stable_task_id, status="SUCCEEDED", video_url=video_url)
        if normalized_status in {"failed", "cancelled", "canceled", "expired"}:
            error = response_payload.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            return VideoTaskResult(
                provider_task_id=stable_task_id,
                status="FAILED",
                error_message=message if isinstance(message, str) and message else f"火山方舟任务状态：{raw_status}",
            )
        if normalized_status in {"queued", "running"}:
            # queued、running 等非终态都由 Worker 按固定间隔继续轮询。
            return VideoTaskResult(provider_task_id=stable_task_id, status="PENDING")
        raise RuntimeError("火山方舟任务返回未知状态")

    def _endpoint(self) -> str:
        configured = self.provider_config.get("api_base_url")
        if not isinstance(configured, str) or configured.rstrip("/") != self._BASE_URL:
            raise RuntimeError("火山方舟视频模型 API 地址必须为 https://ark.cn-beijing.volces.com/api/v3")
        return f"{configured.rstrip('/')}/contents/generations/tasks"

    @staticmethod
    def _first_frame_url(request: VideoGenerationInput) -> str:
        """选取唯一首帧；本地资产仅能以短暂 Data URL 交给方舟。"""

        if request.reference_images:
            if len(request.reference_images) != 1:
                raise RuntimeError("方舟视频最小验收只支持一张首帧参考图")
            first_frame = request.reference_images[0]
            if first_frame.role != "first_frame" or not first_frame.data_url.startswith("data:image/"):
                raise RuntimeError("方舟视频首帧必须是经过本地资产校验的图片")
            return first_frame.data_url
        if not request.image_urls:
            raise RuntimeError("豆包图生视频缺少首帧图片")
        return VolcengineArkVideoProvider._public_image_url(request.image_urls[0])

    def _api_key(self) -> str:
        """只从服务器环境读取 ARK Key，不保存或回显真实密钥。"""

        secret_env_name = self.provider_config.get("secret_env_name")
        if secret_env_name != "ARK_API_KEY":
            raise RuntimeError("火山方舟视频必须使用 ARK_API_KEY")
        api_key = os.getenv(secret_env_name)
        if not api_key:
            raise RuntimeError(f"服务器环境变量 {secret_env_name} 未设置")
        return api_key

    @staticmethod
    def _public_image_url(image_url: str) -> str:
        """方舟服务端需能读取首帧，阻止将本机 data URL 发送到付费接口。"""

        if not isinstance(image_url, str) or not image_url.startswith("https://"):
            raise RuntimeError("豆包图生视频需要公网 HTTPS 首帧图片；请先使用真实图片模型或对象存储")
        return image_url


class ConfigurableAsyncVideoProvider:
    """可配置的中转站异步图生视频适配器。

    该适配器刻意不把某个供应商的请求字段写进业务层。每个模型配置都可以定义
    提交路径、查询路径、提示词字段、首帧包装方式、完成/失败状态与视频 URL
    的 JSON 路径。更换中转站时仅新增一版模型配置，已开始的工作流继续使用
    自己保存的快照。

    当前内置两种常见首帧协议：
    - ``top_level_url``：将首帧地址写入一个顶层字段，例如 ``image_url``；
    - ``luma_keyframe``：组装 ``keyframes.frame0 = {type: image, url: ...}``。

    真实图生视频服务需要其服务器能够访问的 HTTPS 图片地址。模拟图片的 data
    URL 仅用于本地联调，使用本适配器时会主动拒绝，避免无意义扣费。
    """

    provider_key = "configurable_async_video"

    def __init__(self, model_profile_snapshot: dict[str, Any]) -> None:
        """从一次运行冻结的配置构造客户端，不读取或暴露实时密钥。"""

        self.model_key = str(model_profile_snapshot["model_key"])
        self.provider_config = model_profile_snapshot.get("provider_config") or {}

    def submit(self, request: VideoGenerationInput) -> VideoTaskResult:
        """提交首帧驱动的视频任务，并提取供应商返回的任务号与即时结果。"""

        api_key = self._api_key()
        first_image_url = self._public_image_url(request.image_urls[0])
        payload = self._request_options()
        prompt_field = self._field_name("prompt_field", "user_prompt")
        payload[prompt_field] = request.prompt

        model_field = self.provider_config.get("model_field")
        if model_field is not None:
            if not isinstance(model_field, str) or not model_field.strip():
                raise RuntimeError("model_field 必须是字段名或不填写")
            payload[self._safe_field_name(model_field, "model_field")] = self.model_key

        image_mode = self.provider_config.get("image_input_mode", "top_level_url")
        if image_mode == "top_level_url":
            payload[self._field_name("image_field", "image_url")] = first_image_url
        elif image_mode == "luma_keyframe":
            keyframes_field = self._field_name("keyframes_field", "keyframes")
            keyframe_name = self._field_name("keyframe_name", "frame0")
            payload[keyframes_field] = {
                keyframe_name: {"type": "image", "url": first_image_url},
            }
        else:
            raise RuntimeError("image_input_mode 仅支持 top_level_url 或 luma_keyframe")

        end_image_field = self.provider_config.get("end_image_field")
        if end_image_field is not None and len(request.image_urls) > 1:
            if not isinstance(end_image_field, str) or not end_image_field.strip():
                raise RuntimeError("end_image_field 必须是字段名或不填写")
            payload[self._safe_field_name(end_image_field, "end_image_field")] = self._public_image_url(
                request.image_urls[-1]
            )

        response_payload = _post_json(
            _configured_endpoint(self.provider_config, "submit_path"),
            api_key,
            payload,
            _request_timeout_seconds(self.provider_config),
        )
        return self._task_result(response_payload, require_task_id=True)

    def poll(self, provider_task_id: str) -> VideoTaskResult:
        """按配置查询异步任务，并归一化中转站的状态和值。"""

        if not provider_task_id:
            raise RuntimeError("视频任务缺少供应商任务号")
        template = self.provider_config.get("query_path_template")
        if not isinstance(template, str) or "{task_id}" not in template:
            raise RuntimeError("视频模型配置缺少包含 {task_id} 的 query_path_template")
        endpoint = _configured_endpoint(
            self.provider_config,
            "query_path_template",
            template.replace("{task_id}", quote(provider_task_id, safe="")),
        )
        response_payload = _get_json(
            endpoint,
            self._api_key(),
            _request_timeout_seconds(self.provider_config),
        )
        result = self._task_result(response_payload, require_task_id=False)
        return VideoTaskResult(
            provider_task_id=result.provider_task_id or provider_task_id,
            status=result.status,
            video_url=result.video_url,
            error_message=result.error_message,
        )

    def _api_key(self) -> str:
        """只通过配置引用的环境变量读取密钥，杜绝密钥进入数据库或日志。"""

        secret_env_name = self.provider_config.get("secret_env_name")
        if not isinstance(secret_env_name, str) or not secret_env_name.strip():
            raise RuntimeError("视频模型配置缺少 secret_env_name")
        api_key = os.getenv(secret_env_name)
        if not api_key:
            raise RuntimeError(f"服务器环境变量 {secret_env_name} 未设置")
        return api_key

    def _request_options(self) -> dict[str, Any]:
        """读取非敏感固定参数，且不允许它覆盖适配器控制的输入字段。"""

        options = self.provider_config.get("video_request_options", {})
        if not isinstance(options, dict):
            raise RuntimeError("video_request_options 必须为 JSON 对象")
        return dict(options)

    def _field_name(self, config_key: str, default: str) -> str:
        """读取可替换的供应商字段名，并限制为简单 JSON 键。"""

        value = self.provider_config.get(config_key, default)
        return self._safe_field_name(value, config_key)

    @staticmethod
    def _safe_field_name(value: Any, config_key: str) -> str:
        """拒绝嵌套路径和空字段，避免配置意外改写未知请求结构。"""

        if not isinstance(value, str) or not value or not value.replace("_", "").isalnum():
            raise RuntimeError(f"{config_key} 必须是字母、数字或下划线组成的字段名")
        return value

    @staticmethod
    def _public_image_url(image_url: str) -> str:
        """确保中转站能从公网读取首帧图片，而非拿到浏览器专用 data URL。"""

        if not isinstance(image_url, str) or not image_url.startswith("https://"):
            raise RuntimeError("真实图生视频需要 HTTPS 图片地址；请先使用真实图片模型或对象存储")
        return image_url

    def _task_result(self, response_payload: dict[str, Any], *, require_task_id: bool) -> VideoTaskResult:
        """通过 JSON 路径读取任务号、状态、结果地址和可选失败原因。"""

        task_id_path = self.provider_config.get("task_id_path", "id")
        task_id = _read_json_path(response_payload, task_id_path)
        if require_task_id and (not isinstance(task_id, str) or not task_id):
            raise RuntimeError(f"视频中转站响应缺少任务号：{task_id_path}")
        state_path = self.provider_config.get("state_path", "state")
        raw_state = _read_json_path(response_payload, state_path)
        if not isinstance(raw_state, str) or not raw_state:
            raise RuntimeError(f"视频中转站响应缺少任务状态：{state_path}")
        normalized_state = raw_state.strip().lower()
        success_states = self._states("success_states", ["completed", "succeeded", "success"])
        failure_states = self._states("failure_states", ["failed", "error", "cancelled"])
        if normalized_state in success_states:
            video_url = self._video_url(response_payload)
            if not video_url:
                raise RuntimeError("视频任务已完成但响应中没有可用视频地址")
            return VideoTaskResult(str(task_id or ""), "SUCCEEDED", video_url=video_url)
        if normalized_state in failure_states:
            error_path = self.provider_config.get("error_message_path", "error.message")
            error = _read_json_path(response_payload, error_path)
            return VideoTaskResult(
                str(task_id or ""),
                "FAILED",
                error_message=str(error) if error else f"供应商任务状态：{raw_state}",
            )
        return VideoTaskResult(str(task_id or ""), "PENDING")

    def _states(self, key: str, default: list[str]) -> set[str]:
        """把可配置状态值统一转为小写集合。"""

        values = self.provider_config.get(key, default)
        if not isinstance(values, list) or not values or not all(isinstance(item, str) and item for item in values):
            raise RuntimeError(f"{key} 必须是非空字符串数组")
        return {item.strip().lower() for item in values}

    def _video_url(self, response_payload: dict[str, Any]) -> Optional[str]:
        """依次尝试多个 URL 路径，兼容各中转站不同的成功响应结构。"""

        paths = self.provider_config.get("video_url_paths", ["video.url", "artifact.video.url"])
        if not isinstance(paths, list) or not paths or not all(isinstance(item, str) and item for item in paths):
            raise RuntimeError("video_url_paths 必须是非空 JSON 路径数组")
        for path in paths:
            value = _read_json_path(response_payload, path)
            if isinstance(value, str) and value.startswith(("https://", "http://")):
                return value
        return None
