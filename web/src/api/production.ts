/** LemonFlow V1 生产台、人工审核和模型槽位接口。 */
import { request } from './http'
import type {
  CharacterReferenceImageV1,
  ModelSlot,
  ModelInvocationTrace,
  ModelQualityEvaluation,
  ProductionState,
  PromptTemplate,
  ReferenceAnalysis,
  ReviewActionPayload,
  SceneReferenceImageV1,
  ShotKeyframeV1,
  StoryProposalV1,
  VideoClipV1,
  V1ModelProfile,
  V1ModelProfileCreatePayload,
  V1ModelProfileUpdatePayload,
  WorkflowRun,
} from '@/types/domain'

const jsonPost = <T>(path: string, payload: object = {}): Promise<T> => request<T>(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload),
})

/** 读取唯一 V1 工作流的当前阶段和冻结对象指针。 */
export function getProductionState(projectId: string): Promise<ProductionState> {
  return request<ProductionState>(`/production/projects/${projectId}/state`)
}

/** 查询该项目每次生成冻结的 Workflow、模型和 Prompt 版本，便于人工追溯问题。 */
export function getProjectModelInvocations(projectId: string, limit = 200): Promise<ModelInvocationTrace[]> {
  return request<ModelInvocationTrace[]>(`/production/projects/${projectId}/model-invocations?limit=${limit}`)
}

/** 创建一个 V1 后台生成节点；模型、Prompt 和 Workflow 快照由服务端冻结。 */
export function startProductionRun(
  projectId: string,
  runKey: string,
  shotPlanIds: string[] = [],
  sourceAssetId?: string,
): Promise<WorkflowRun> {
  return jsonPost<WorkflowRun>(`/production/projects/${projectId}/generation-runs/${encodeURIComponent(runKey)}`, {
    shot_plan_ids: shotPlanIds,
    source_asset_id: sourceAssetId,
  })
}

export function getReferenceAnalyses(projectId: string): Promise<ReferenceAnalysis[]> {
  return request<ReferenceAnalysis[]>(`/production/projects/${projectId}/reference-analyses`)
}

export function lockReferenceAnalysis(id: string, payload: ReviewActionPayload): Promise<ReferenceAnalysis> {
  return jsonPost<ReferenceAnalysis>(`/production/reference-analyses/${id}/lock`, payload)
}

export function rejectReferenceAnalysis(id: string, payload: ReviewActionPayload): Promise<ReferenceAnalysis> {
  return jsonPost<ReferenceAnalysis>(`/production/reference-analyses/${id}/reject`, payload)
}

export function getStoryProposals(projectId: string): Promise<StoryProposalV1[]> {
  return request<StoryProposalV1[]>(`/production/projects/${projectId}/story-proposals`)
}

export function selectStoryProposal(id: string, payload: ReviewActionPayload): Promise<StoryProposalV1> {
  return jsonPost<StoryProposalV1>(`/production/story-proposals/${id}/select`, payload)
}

export function getCharacterReferenceImages(projectId: string): Promise<CharacterReferenceImageV1[]> {
  return request<CharacterReferenceImageV1[]>(`/production/projects/${projectId}/character-reference-images`)
}

export function lockCharacterReferenceImage(id: string, payload: ReviewActionPayload): Promise<CharacterReferenceImageV1> {
  return jsonPost<CharacterReferenceImageV1>(`/production/character-reference-images/${id}/lock`, payload)
}

export function getSceneReferenceImages(projectId: string): Promise<SceneReferenceImageV1[]> {
  return request<SceneReferenceImageV1[]>(`/production/projects/${projectId}/scene-reference-images`)
}

export function lockSceneReferenceImage(id: string, payload: ReviewActionPayload): Promise<SceneReferenceImageV1> {
  return jsonPost<SceneReferenceImageV1>(`/production/scene-reference-images/${id}/lock`, payload)
}

export function getShotKeyframes(projectId: string): Promise<ShotKeyframeV1[]> {
  return request<ShotKeyframeV1[]>(`/production/projects/${projectId}/shot-keyframes`)
}

export function lockShotKeyframe(id: string, payload: ReviewActionPayload): Promise<ShotKeyframeV1> {
  return jsonPost<ShotKeyframeV1>(`/production/shot-keyframes/${id}/lock`, payload)
}

export function getV1VideoClips(projectId: string): Promise<VideoClipV1[]> {
  return request<VideoClipV1[]>(`/production/projects/${projectId}/video-clips`)
}

export function approveV1VideoClip(id: string, payload: ReviewActionPayload): Promise<VideoClipV1> {
  return jsonPost<VideoClipV1>(`/production/video-clips/${id}/approve`, payload)
}

export function rejectV1VideoClip(id: string, payload: ReviewActionPayload): Promise<VideoClipV1> {
  return jsonPost<VideoClipV1>(`/production/video-clips/${id}/reject`, payload)
}

/** 模型中心读取能力槽位；模型名称只在配置绑定层出现。 */
export function getModelSlots(): Promise<ModelSlot[]> {
  return request<ModelSlot[]>('/production/model-slots')
}

/** V1 模型候选与实际槽位启用状态；只显示 V1，不混入旧流程模型。 */
export function getV1ModelProfiles(): Promise<V1ModelProfile[]> {
  return request<V1ModelProfile[]>('/production/v1-model-profiles')
}

/** 读取每个“模型 + Prompt + 任务”的最新质量快照，不改变任何模型启用状态。 */
export function getModelQualityEvaluations(taskType?: string): Promise<ModelQualityEvaluation[]> {
  const query = taskType ? `?task_type=${encodeURIComponent(taskType)}` : ''
  return request<ModelQualityEvaluation[]>(`/production/model-quality-evaluations${query}`)
}

/** 手动生成质量快照；只统计已有调用和审核，绝不会重跑或自动更换模型。 */
export function refreshModelQualityEvaluations(taskType?: string): Promise<ModelQualityEvaluation[]> {
  return jsonPost<ModelQualityEvaluation[]>('/production/model-quality-evaluations/refresh', taskType ? { task_type: taskType } : {})
}

/** 新建一版 V1 候选模型，默认不启用；真实 Key 只能配置在服务器环境中。 */
export function createV1ModelProfile(payload: V1ModelProfileCreatePayload): Promise<V1ModelProfile> {
  return jsonPost<V1ModelProfile>('/production/v1-model-profiles', payload)
}

/** 编辑尚未产生调用记录的模型版本；历史生产版本必须先复制。 */
export function updateV1ModelProfile(
  profileId: string,
  payload: V1ModelProfileUpdatePayload,
): Promise<V1ModelProfile> {
  return request<V1ModelProfile>(`/production/v1-model-profiles/${encodeURIComponent(profileId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

/** 从任意历史版本复制出同槽位的下一版 Draft。 */
export function copyV1ModelProfile(profileId: string): Promise<V1ModelProfile> {
  return jsonPost<V1ModelProfile>(`/production/v1-model-profiles/${encodeURIComponent(profileId)}/copy`)
}

/** 删除未被生产历史或进行中 V1 任务引用的模型候选。 */
export function deleteV1ModelProfile(profileId: string): Promise<void> {
  return request<void>(`/production/v1-model-profiles/${encodeURIComponent(profileId)}`, { method: 'DELETE' })
}

/** 人工启用/停用一个已有版本；单模型槽位的替换必须显式传 replaceExisting。 */
export function setV1ModelProfileEnabled(
  slotKey: string,
  profileId: string,
  enabled: boolean,
  replaceExisting = false,
): Promise<unknown> {
  return jsonPost(`/production/model-slots/${encodeURIComponent(slotKey)}/bindings`, {
    model_profile_id: profileId,
    enabled,
    replace_existing: replaceExisting,
  })
}

export function getPromptTemplates(taskType?: string): Promise<PromptTemplate[]> {
  const query = taskType ? `?task_type=${encodeURIComponent(taskType)}` : ''
  return request<PromptTemplate[]>(`/production/prompt-templates${query}`)
}

/** 新建 Prompt 草稿；生产模板永远以新版本保存，不能在原版本上直接修改。 */
export function createPromptTemplate(payload: {
  task_type: string
  name: string
  content: string
  variables_schema: Record<string, unknown>
}): Promise<PromptTemplate> {
  return jsonPost<PromptTemplate>('/production/prompt-templates', payload)
}

/** 人工启用一个 Prompt 版本；同一生产任务原来的生效版本会被保留为历史。 */
export function activatePromptTemplate(id: string): Promise<PromptTemplate> {
  return jsonPost<PromptTemplate>(`/production/prompt-templates/${id}/activate`)
}

/** 仅归档草稿或历史版本，当前生效版本必须先有替代版本。 */
export function archivePromptTemplate(id: string): Promise<PromptTemplate> {
  return jsonPost<PromptTemplate>(`/production/prompt-templates/${id}/archive`)
}
