/** 模型配置中心状态：配置版本独立于具体项目，供所有项目的新任务使用。 */
import { defineStore } from 'pinia'
import {
  activateModelProfile,
  createModelEvaluation,
  createModelProfile,
  getModelEvaluationComparisons,
  getModelEvaluations,
  getModelProfiles,
  preflightModelProfile,
  type ModelProfilePayload,
} from '@/api/model-profiles'
import type { ModelEvaluation, ModelEvaluationComparison, ModelEvaluationPayload, ModelProfile, ModelProfilePreflight } from '@/types/domain'

export const useModelProfilesStore = defineStore('model-profiles', {
  state: () => ({
    profiles: [] as ModelProfile[],
    loading: false,
    submitting: false,
    preflightingProfileId: '',
    preflights: {} as Record<string, ModelProfilePreflight>,
    evaluations: {} as Record<string, ModelEvaluation[]>,
    evaluationLoadingProfileId: '',
    evaluationSavingProfileId: '',
    comparisons: [] as ModelEvaluationComparison[],
    comparisonLoading: false,
    error: '',
  }),
  actions: {
    /** 统一将接口失败转换为可读提示，页面无需解析 HTTP 异常。 */
    setError(error: unknown) {
      this.error = error instanceof Error ? error.message : '模型配置操作失败，请稍后重试'
    },

    async load() {
      this.loading = true
      this.error = ''
      try {
        this.profiles = await getModelProfiles()
      } catch (error) {
        this.setError(error)
      } finally {
        this.loading = false
      }
    },

    async create(payload: ModelProfilePayload): Promise<boolean> {
      this.submitting = true
      this.error = ''
      try {
        await createModelProfile(payload)
        await this.load()
        return true
      } catch (error) {
        this.setError(error)
        return false
      } finally {
        this.submitting = false
      }
    },

    async activate(profileId: string) {
      this.error = ''
      try {
        await activateModelProfile(profileId)
        await this.load()
      } catch (error) {
        this.setError(error)
      }
    },

    /** 预检不改变启用状态；结果按配置版本缓存，避免一次点击触发多次第三方目录查询。 */
    async preflight(profileId: string) {
      this.preflightingProfileId = profileId
      this.error = ''
      try {
        this.preflights[profileId] = await preflightModelProfile(profileId)
      } catch (error) {
        this.setError(error)
      } finally {
        this.preflightingProfileId = ''
      }
    },

    /** 按需读取统计记录，避免进入配置页时为每一个历史版本都请求一次。 */
    async loadEvaluations(profileId: string) {
      this.evaluationLoadingProfileId = profileId
      this.error = ''
      try {
        this.evaluations[profileId] = await getModelEvaluations(profileId)
      } catch (error) {
        this.setError(error)
      } finally {
        this.evaluationLoadingProfileId = ''
      }
    },

    /** 保存后立即刷新同一版本的统计，保证成功率和单位成本由后端统一计算。 */
    async createEvaluation(profileId: string, payload: ModelEvaluationPayload): Promise<boolean> {
      this.evaluationSavingProfileId = profileId
      this.error = ''
      try {
        await createModelEvaluation(profileId, payload)
        await this.loadEvaluations(profileId)
        return true
      } catch (error) {
        this.setError(error)
        return false
      } finally {
        this.evaluationSavingProfileId = ''
      }
    },

    /** 同一步骤内并列返回实测数据，页面按场景展示而不伪造跨场景综合分。 */
    async loadComparisons(stepKey: string) {
      this.comparisonLoading = true
      this.error = ''
      try {
        this.comparisons = await getModelEvaluationComparisons(stepKey)
      } catch (error) {
        this.setError(error)
      } finally {
        this.comparisonLoading = false
      }
    },
  },
})
