/** 模型配置中心接口；不传输或保存任何真实 API Key。 */
import { request } from './http'
import type { ModelEvaluation, ModelEvaluationComparison, ModelEvaluationPayload, ModelProfile, ModelProfilePreflight } from '@/types/domain'

export interface ModelProfilePayload {
  step_key: string
  provider_key: string
  model_key: string
  provider_config: Record<string, unknown>
  activate: boolean
}

/** 查询各工作流步骤的模型配置版本。 */
export function getModelProfiles(): Promise<ModelProfile[]> {
  return request<ModelProfile[]>('/model-profiles')
}

/** 新增一版配置；供应商未接入时服务端只允许保存为未启用。 */
export function createModelProfile(payload: ModelProfilePayload): Promise<ModelProfile> {
  return request<ModelProfile>('/model-profiles', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

/** 启用某个已接通的配置版本，并自动停用同步骤旧版本。 */
export function activateModelProfile(profileId: string): Promise<ModelProfile> {
  return request<ModelProfile>(`/model-profiles/${profileId}/activate`, { method: 'POST' })
}

/** 在启用前验证适配器、参数、密钥是否已注入；不会提交生成任务。 */
export function preflightModelProfile(profileId: string): Promise<ModelProfilePreflight> {
  return request<ModelProfilePreflight>(`/model-profiles/${profileId}/preflight`, { method: 'POST' })
}

/** 读取一版配置的人工小样本验收记录。 */
export function getModelEvaluations(profileId: string): Promise<ModelEvaluation[]> {
  return request<ModelEvaluation[]>(`/model-profiles/${profileId}/evaluations`)
}

/** 保存人工已经完成的样本统计，不上传原始视频、提示词或模型回复。 */
export function createModelEvaluation(profileId: string, payload: ModelEvaluationPayload): Promise<ModelEvaluation> {
  return request<ModelEvaluation>(`/model-profiles/${profileId}/evaluations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

/** 读取一个工作流步骤内的所有评测行；不同测试场景仅并列展示，不混合平均。 */
export function getModelEvaluationComparisons(stepKey: string): Promise<ModelEvaluationComparison[]> {
  return request<ModelEvaluationComparison[]>(`/model-profiles/evaluation-comparisons?step_key=${encodeURIComponent(stepKey)}`)
}
