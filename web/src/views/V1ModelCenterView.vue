<script setup lang="ts">
/**
 * LemonFlow V1 模型中心。
 *
 * 页面按“能力槽位”管理模型，不把 Gemini、Claude、Banana 或 Seedance 写进业务逻辑。
 * 所有删除开关均来自后端：浏览器只展示状态，不能绕过进行中任务与历史调用的保护。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  copyV1ModelProfile,
  createV1ModelProfile,
  deleteV1ModelProfile,
  getModelSlots,
  getV1ModelProfiles,
  preflightV1ModelProfile,
  setV1ModelProfileEnabled,
  updateV1ModelProfile,
} from '@/api/production'
import type { ModelParameterConfig, ModelProfilePreflight, ModelSlot, V1ModelProfile } from '@/types/domain'

const slots = ref<ModelSlot[]>([])
const profiles = ref<V1ModelProfile[]>([])
const loading = ref(true)
const saving = ref(false)
const error = ref('')

const slotKey = ref('VIDEO_ANALYSIS')
const modelKey = ref('')
const displayName = ref('')
const modelVersion = ref('')
const apiBaseUrl = ref('https://yunwu.ai/v1')
// 推理/视觉理解和图片生成是两条独立的供应商额度通道。这里只保存变量名，
// 绝不保存或展示变量的实际值。
const secretEnvName = ref('YUNWU_REASONING_API_KEY')
const imageSize = ref('2K')
const referenceImageField = ref('images')
const estimatedCost = ref<number | null>(null)
const enableAfterSave = ref(false)
const editingProfileId = ref<string | null>(null)
const editingAdapterKey = ref('')
const editingProviderConfig = ref<Record<string, unknown>>({})
// 参数字段由服务端返回的 capability 配置驱动。浏览器没有“自定义字段名”入口，
// 从而不能把任意供应商 JSON、Header 或密钥伪装成生成参数提交。
const editingParameterConfig = ref<ModelParameterConfig | null>(null)
const editingParameterDefaults = ref<Record<string, unknown>>({})
const editingParameterPresets = ref<Record<string, Record<string, unknown>>>({})
const preflightingProfileId = ref<string | null>(null)
const preflights = ref<Record<string, ModelProfilePreflight>>({})

const slotLabels: Record<string, string> = {
  VIDEO_ANALYSIS: '分析参考视频',
  STORY_GENERATE: '并行生成原创故事',
  CHARACTER_DESIGN: '设计角色文字资产',
  SCENE_DESIGN: '设计场景文字资产',
  DIRECTOR_PLAN: 'AI 导演生成分镜',
  CHARACTER_IMAGE_GENERATE: '生成角色参考图',
  SCENE_IMAGE_GENERATE: '生成场景参考图',
  SHOT_KEYFRAME_GENERATE: '生成分镜关键帧',
  VIDEO_GENERATE: '生成视频片段',
  FINAL_COMPOSE: '合成最终成片',
}

const selectedSlot = computed(() => slots.value.find((item) => item.slot_key === slotKey.value))
const slotLabel = computed(() => slotLabels[slotKey.value] || selectedSlot.value?.description || slotKey.value)
const isVision = computed(() => slotKey.value === 'VIDEO_ANALYSIS')
const isText = computed(() => ['STORY_GENERATE', 'CHARACTER_DESIGN', 'SCENE_DESIGN', 'DIRECTOR_PLAN'].includes(slotKey.value))
const isImage = computed(() => ['CHARACTER_IMAGE_GENERATE', 'SCENE_IMAGE_GENERATE', 'SHOT_KEYFRAME_GENERATE'].includes(slotKey.value))
const isKeyframe = computed(() => slotKey.value === 'SHOT_KEYFRAME_GENERATE')
const isVideo = computed(() => slotKey.value === 'VIDEO_GENERATE')
const isFinalCompose = computed(() => slotKey.value === 'FINAL_COMPOSE')
const defaultAdapterKey = computed(() => {
  if (isVision.value) return 'openai_compatible_vision'
  if (isText.value) return 'openai_compatible'
  // 新建图片候选默认走方舟官方 Seedream；编辑历史 Fal/OpenAI 兼容版本仍保持
  // 原 Adapter，绝不无提示覆盖历史模型配置或将其改成新通道。
  if (isImage.value) return 'volcengine_ark_image'
  if (isVideo.value) return 'volcengine_ark_video'
  return 'ffmpeg_concat'
})
const adapterKey = computed(() => editingAdapterKey.value || defaultAdapterKey.value)
const isFalImage = computed(() => isImage.value && adapterKey.value === 'fal_queue_image')
const isArkImage = computed(() => isImage.value && adapterKey.value === 'volcengine_ark_image')
const slotProfiles = computed(() => profiles.value.filter((item) => item.slot_key === slotKey.value))
const editingVersionLabel = computed(() => {
  const profile = profiles.value.find((item) => item.id === editingProfileId.value)
  return profile ? `保存第 ${profile.version} 版修改` : '保存当前版本修改'
})
const editingProfile = computed(() => profiles.value.find((item) => item.id === editingProfileId.value) ?? null)
const parameterPresetNames = ['preview', 'standard', 'high'] as const
const parameterNames = computed(() => Object.keys(editingParameterConfig.value?.supported_parameters ?? {}))
const parameterConfigLocked = computed(() => Boolean(editingProfile.value && editingProfile.value.profile_status !== 'DRAFT'))

function cloneParameterConfig(value: ModelParameterConfig): ModelParameterConfig {
  return JSON.parse(JSON.stringify(value)) as ModelParameterConfig
}

function parameterSpec(name: string): Record<string, unknown> {
  return editingParameterConfig.value?.supported_parameters[name] ?? {}
}

function isEnumParameter(name: string): boolean {
  return parameterSpec(name).kind === 'enum'
}

function enumValues(name: string): Array<string | number | boolean> {
  const values = parameterSpec(name).values
  return Array.isArray(values) ? values.filter((value): value is string | number | boolean => ['string', 'number', 'boolean'].includes(typeof value)) : []
}

function numericMinimum(name: string): number | undefined {
  const value = parameterSpec(name).minimum
  return typeof value === 'number' ? value : undefined
}

function numericMaximum(name: string): number | undefined {
  const value = parameterSpec(name).maximum
  return typeof value === 'number' ? value : undefined
}

function presetValues(name: typeof parameterPresetNames[number]): Record<string, unknown> {
  return editingParameterPresets.value[name] ?? {}
}

function currentParameterConfig(): ModelParameterConfig | undefined {
  if (!editingParameterConfig.value) return undefined
  return {
    ...cloneParameterConfig(editingParameterConfig.value),
    defaults: { ...editingParameterDefaults.value },
    presets: Object.fromEntries(
      parameterPresetNames
        .filter((preset) => Object.prototype.hasOwnProperty.call(editingParameterPresets.value, preset))
        .map((preset) => [preset, { ...presetValues(preset) }]),
    ) as ModelParameterConfig['presets'],
  }
}

/** 读取服务端正式配置；删除权限、活动任务数不能由浏览器自行判断。 */
async function load() {
  loading.value = true
  error.value = ''
  try {
    const [loadedSlots, loadedProfiles] = await Promise.all([getModelSlots(), getV1ModelProfiles()])
    slots.value = loadedSlots
    profiles.value = loadedProfiles
    if (!loadedSlots.some((item) => item.slot_key === slotKey.value) && loadedSlots[0]) {
      slotKey.value = loadedSlots[0].slot_key
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '模型中心加载失败，请确认后端服务与数据库迁移已启动。'
  } finally {
    loading.value = false
  }
}

/** 切换槽位时给出小白可理解的推荐输入，真实 Key 始终不在网页表单中出现。 */
function applySlotTemplate(next: string) {
  modelVersion.value = ''
  estimatedCost.value = null
  enableAfterSave.value = false
  apiBaseUrl.value = 'https://yunwu.ai/v1'
  secretEnvName.value = 'YUNWU_REASONING_API_KEY'
  if (next === 'VIDEO_ANALYSIS') {
    displayName.value = '参考视频分析模型'
    modelKey.value = ''
  } else if (['STORY_GENERATE', 'CHARACTER_DESIGN', 'SCENE_DESIGN', 'DIRECTOR_PLAN'].includes(next)) {
    displayName.value = next === 'STORY_GENERATE' ? '原创故事导演模型' : 'AI 导演文本模型'
    modelKey.value = ''
  } else if (['CHARACTER_IMAGE_GENERATE', 'SCENE_IMAGE_GENERATE', 'SHOT_KEYFRAME_GENERATE'].includes(next)) {
    displayName.value = next === 'SHOT_KEYFRAME_GENERATE' ? '方舟 Seedream 分镜关键帧模型' : '方舟 Seedream 图片资产模型'
    modelKey.value = 'doubao-seedream-5-0-260128'
    secretEnvName.value = 'ARK_API_KEY'
    apiBaseUrl.value = 'https://ark.cn-beijing.volces.com/api/v3'
  } else if (next === 'VIDEO_GENERATE') {
    displayName.value = '豆包 Seedance 视频模型'
    modelKey.value = 'doubao-seedance-2-0-mini-260615'
    secretEnvName.value = 'ARK_API_KEY'
    apiBaseUrl.value = ''
  } else {
    displayName.value = 'FFmpeg 最终成片合成'
    modelKey.value = 'ffmpeg-concat-v1'
    secretEnvName.value = ''
    apiBaseUrl.value = ''
  }
}

watch(slotKey, (next) => {
  // 编辑时槽位不可变，不能用模板覆盖原配置。
  if (!editingProfileId.value) applySlotTemplate(next)
})

function currentProviderConfig(): Record<string, unknown> {
  const preserved = { ...editingProviderConfig.value }
  delete preserved.estimated_cost_per_call
  delete preserved.currency
  if (adapterKey.value === 'mock_v1' || adapterKey.value === 'configurable_async_video') {
    return withEstimatedCost(preserved)
  }
  if (isVideo.value) {
    // 画幅、时长、分辨率、音频和水印都属于版本化 parameter_config；不能继续
    // 混在连接配置中。保留旧 Profile 中的字段只用于服务端兼容读取，新草稿保存
    // 后由 Worker 将冻结参数临时映射到供应商协议。
    for (const name of ['ratio', 'duration', 'resolution', 'generate_audio', 'watermark']) delete preserved[name]
    return withEstimatedCost({ ...preserved, secret_env_name: 'ARK_API_KEY' })
  }
  if (isFinalCompose.value) return withEstimatedCost(preserved)
  const config: Record<string, unknown> = {
    ...preserved,
    api_base_url: apiBaseUrl.value.trim(),
    secret_env_name: secretEnvName.value.trim(),
  }
  if (isVision.value) {
    config.result_contract = 'V1_REFERENCE_ANALYSIS'
    config.frame_sample_count = 6
  }
  if (isArkImage.value) {
    for (const name of ['size', 'sequential_image_generation', 'response_format', 'watermark']) delete config[name]
  } else if (isImage.value && !isFalImage.value) {
    config.image_size = imageSize.value.trim()
    if (isKeyframe.value) config.reference_image_field = referenceImageField.value.trim()
  }
  return withEstimatedCost(config)
}

/** 预估成本只用于报表对比，不是供应商账单或自动切换依据。 */
function withEstimatedCost(config: Record<string, unknown>): Record<string, unknown> {
  if (estimatedCost.value !== null && Number.isFinite(estimatedCost.value) && estimatedCost.value >= 0) {
    config.estimated_cost_per_call = estimatedCost.value
    config.currency = 'CNY'
  }
  return config
}

function resetToCandidate() {
  editingProfileId.value = null
  editingAdapterKey.value = ''
  editingProviderConfig.value = {}
  editingParameterConfig.value = null
  editingParameterDefaults.value = {}
  editingParameterPresets.value = {}
  applySlotTemplate(slotKey.value)
}

async function saveCandidate() {
  if (!modelKey.value.trim() || !displayName.value.trim()) {
    error.value = '请填写模型名称和给自己看的备注名称。模型名称请直接从中转站后台复制。'
    return
  }
  saving.value = true
  error.value = ''
  try {
    const payload = {
      adapter_key: adapterKey.value,
      model_key: modelKey.value.trim(),
      display_name: displayName.value.trim(),
      model_version: modelVersion.value.trim() || undefined,
      provider_config: currentProviderConfig(),
      ...(editingProfileId.value ? { parameter_config: currentParameterConfig() } : {}),
    }
    if (editingProfileId.value) {
      await updateV1ModelProfile(editingProfileId.value, payload)
      ElMessage.success('已保存当前模型版本的修改')
      resetToCandidate()
    } else {
      const shouldReplace = Boolean(enableAfterSave.value && selectedSlot.value?.selection_mode === 'SINGLE')
      await createV1ModelProfile({
        slot_key: slotKey.value,
        ...payload,
        enable_in_slot: enableAfterSave.value,
        replace_existing: shouldReplace,
        priority: 100,
      })
      ElMessage.success(enableAfterSave.value ? '模型已保存并启用' : '模型已保存为候选草稿')
    }
    await load()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '保存失败，请检查填写内容。'
  } finally {
    saving.value = false
  }
}

/** 加载可编辑版本；产生调用记录的历史版本只能复制，不能覆盖。 */
function beginEdit(profile: V1ModelProfile) {
  if (!profile.can_edit) {
    error.value = '该版本已经产生模型调用，不能覆盖修改；请复制创建新版本。'
    return
  }
  editingProfileId.value = profile.id
  editingAdapterKey.value = profile.adapter_key
  editingProviderConfig.value = { ...profile.provider_config }
  editingParameterConfig.value = cloneParameterConfig(profile.parameter_config)
  editingParameterDefaults.value = { ...profile.parameter_config.defaults }
  editingParameterPresets.value = Object.fromEntries(
    parameterPresetNames
      .filter((preset) => profile.parameter_config.presets[preset] !== undefined)
      .map((preset) => [preset, { ...(profile.parameter_config.presets[preset] ?? {}) }]),
  )
  slotKey.value = profile.slot_key
  modelKey.value = profile.model_key
  displayName.value = profile.display_name
  modelVersion.value = profile.model_version || ''
  apiBaseUrl.value = String(profile.provider_config.api_base_url || '')
  secretEnvName.value = String(profile.provider_config.secret_env_name || '')
  imageSize.value = String(profile.provider_config.size || profile.provider_config.image_size || '')
  referenceImageField.value = String(profile.provider_config.reference_image_field || '')
  estimatedCost.value = typeof profile.provider_config.estimated_cost_per_call === 'number'
    ? profile.provider_config.estimated_cost_per_call
    : null
  enableAfterSave.value = false
  error.value = ''
}

async function copyProfile(profile: V1ModelProfile) {
  saving.value = true
  error.value = ''
  try {
    const copied = await copyV1ModelProfile(profile.id)
    await load()
    beginEdit(copied)
    ElMessage.success(`已复制为第 ${copied.version} 版草稿，请修改并测试后启用`)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '复制新版本失败，请重试。'
  } finally {
    saving.value = false
  }
}

/**
 * 真实运行前的无扣费预检。它只校验 Adapter、非敏感配置、服务器密钥是否注入，
 * 并对 OpenAI 兼容中转站读取 /models；不会生成图片、视频或创建供应商任务。
 */
async function preflightProfile(profile: V1ModelProfile) {
  preflightingProfileId.value = profile.id
  error.value = ''
  try {
    const result = await preflightV1ModelProfile(profile.id)
    preflights.value = { ...preflights.value, [profile.id]: result }
    if (result.ready) ElMessage.success('基础预检通过；视频模型仍需使用公网首帧完成一次明确的小样本权限验收')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '基础预检失败，请检查后端服务。'
  } finally {
    preflightingProfileId.value = null
  }
}

/** 删除前再次向用户说明后果，实际安全校验仍由后端执行。 */
async function removeProfile(profile: V1ModelProfile) {
  try {
    const currentModelWarning = profile.is_enabled_in_slot
      ? '它当前已启用；删除后该功能可能暂时没有可用模型。'
      : '这会删除该候选配置及其槽位绑定。'
    await ElMessageBox.confirm(`${currentModelWarning} 删除后无法恢复，是否继续？`, '删除模型版本', {
      type: 'warning',
      confirmButtonText: '确认删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  saving.value = true
  error.value = ''
  try {
    await deleteV1ModelProfile(profile.id)
    if (editingProfileId.value === profile.id) resetToCandidate()
    await load()
    ElMessage.success('模型候选已删除')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '删除失败，请稍后重试。'
  } finally {
    saving.value = false
  }
}

/** 单模型槽位替换旧绑定，多模型故事槽位则可并行启用多个版本。 */
async function toggleProfile(profile: V1ModelProfile) {
  saving.value = true
  error.value = ''
  try {
    const slot = slots.value.find((item) => item.slot_key === profile.slot_key)
    const enable = !profile.is_enabled_in_slot
    await setV1ModelProfileEnabled(
      profile.slot_key,
      profile.id,
      enable,
      Boolean(enable && slot?.selection_mode === 'SINGLE'),
    )
    await load()
    ElMessage.success(enable ? '已启用此版本' : '已停止使用此版本')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '切换失败，请重试。'
  } finally {
    saving.value = false
  }
}

function profileStatusLabel(profile: V1ModelProfile): string {
  if (profile.is_enabled_in_slot) return '已启用'
  return profile.profile_status === 'DRAFT' ? '草稿候选' : '历史版本'
}

function profileStatusType(profile: V1ModelProfile): 'success' | 'info' | 'warning' {
  if (profile.is_enabled_in_slot) return 'success'
  return profile.profile_status === 'DRAFT' ? 'info' : 'warning'
}

onMounted(load)
applySlotTemplate(slotKey.value)
</script>

<template>
  <section class="model-center-page">
    <div class="page-topbar">
      <div>
        <RouterLink to="/"><el-button text>← 返回项目</el-button></RouterLink>
        <h1>模型配置中心</h1>
        <p>按功能管理模型版本。模型 Key 只保存在服务器环境变量中，不会出现在这里。</p>
      </div>
      <el-space wrap>
        <RouterLink to="/model-quality"><el-button>质量与成本报表</el-button></RouterLink>
        <RouterLink to="/prompt-templates"><el-button>Prompt 模板</el-button></RouterLink>
      </el-space>
    </div>

    <el-alert
      title="安全规则：未使用候选可编辑、可删除；正在运行的任务或历史调用会锁住对应版本。"
      type="info"
      :closable="false"
      show-icon
      class="guide-alert"
    />

    <el-row :gutter="20" class="model-layout">
      <el-col :xs="24" :xl="9">
        <el-card shadow="never" class="config-card">
          <template #header>
            <div class="card-heading">
              <div>
                <span class="eyebrow">{{ editingProfileId ? '正在编辑' : '新增候选' }}</span>
                <h2>{{ editingProfileId ? '编辑模型版本' : '添加模型版本' }}</h2>
              </div>
              <el-tag v-if="editingProfileId" type="warning" effect="plain">版本号与槽位不可改</el-tag>
            </div>
          </template>

          <el-form label-position="top" @submit.prevent="saveCandidate">
            <el-form-item label="它要完成什么功能？">
              <el-select v-model="slotKey" :disabled="Boolean(editingProfileId)" class="full-width">
                <el-option v-for="slot in slots" :key="slot.id" :label="slotLabels[slot.slot_key] || slot.description" :value="slot.slot_key" />
              </el-select>
              <div class="form-help">{{ selectedSlot?.description || '正在读取功能说明…' }}</div>
            </el-form-item>

            <template v-if="isVideo">
              <el-alert title="视频生成使用火山方舟原生协议。时长、分辨率、音频和画幅请在下方“模型能力与参数”中按版本配置。" type="info" :closable="false" show-icon class="compact-alert" />
              <el-form-item label="模型名称"><el-input v-model="modelKey" maxlength="160" /></el-form-item>
            </template>

            <template v-else-if="isFinalCompose">
              <el-alert title="这一环节由服务器 FFmpeg 合成已审核视频，不会调用 AI。" type="info" :closable="false" show-icon class="compact-alert" />
              <el-form-item label="合成器名称"><el-input v-model="modelKey" readonly /></el-form-item>
            </template>

            <template v-else>
              <el-form-item label="模型名称"><el-input v-model="modelKey" placeholder="从中转站后台直接复制" maxlength="160" /></el-form-item>
              <el-form-item label="中转站 API 地址"><el-input v-model="apiBaseUrl" :placeholder="isArkImage ? 'https://ark.cn-beijing.volces.com/api/v3' : (isFalImage ? 'https://yunwu.ai' : 'https://…/v1')" /></el-form-item>
              <el-form-item label="服务器密钥变量名"><el-input v-model="secretEnvName" :placeholder="isArkImage ? '图片填 ARK_API_KEY' : '推理填 YUNWU_REASONING_API_KEY；旧图片填 YUNWU_IMAGE_API_KEY'" /></el-form-item>
              <div class="form-help">只填变量名，绝不在网页粘贴 API Key。Seedream 图片与 Seedance 视频可共用 ARK_API_KEY，但不会互相调用。</div>
              <el-alert v-if="isFalImage" title="图片使用云雾 Nano Banana 队列：系统会先保存供应商任务号，再轮询并下载一张校验后的图片。无需填写图片尺寸、参考图字段或请求路径。" type="info" :closable="false" show-icon class="compact-alert" />
              <el-alert v-if="isArkImage" title="图片使用方舟官方 Seedream 5.0 Pro：只生成一张纯文本图片，系统立即下载并保存到本机资产目录；本轮不支持参考图、多图或流式生成。" type="info" :closable="false" show-icon class="compact-alert" />
            </template>

            <template v-if="isImage && !isFalImage && !isArkImage">
              <el-form-item label="图片尺寸"><el-input v-model="imageSize" placeholder="例如 1728x2304" /></el-form-item>
              <el-form-item v-if="isKeyframe" label="中转站文档中的参考图字段名"><el-input v-model="referenceImageField" placeholder="通常为 images，以文档为准" /></el-form-item>
            </template>

            <template v-if="editingParameterConfig">
              <el-divider content-position="left">模型能力与参数</el-divider>
              <el-alert
                :title="parameterConfigLocked ? '已启用版本的能力、默认值和质量预设不能原地修改；请先复制为草稿。' : '这里只能调整当前 Adapter 已声明的字段；保存后会随本 Profile 版本冻结。'"
                :type="parameterConfigLocked ? 'warning' : 'info'"
                :closable="false"
                show-icon
                class="compact-alert"
              />
              <div class="parameter-summary">
                <el-tag effect="plain">能力：{{ editingParameterConfig.capability }}</el-tag>
                <el-tag :type="editingProfile?.parameter_config_complete ? 'success' : 'warning'" effect="plain">
                  {{ editingProfile?.parameter_config_complete ? '已保存能力版本' : '旧版本兼容视图' }}
                </el-tag>
              </div>
              <template v-for="name in parameterNames" :key="`default-${name}`">
                <el-form-item :label="`${name} 默认值`">
                  <el-select v-if="isEnumParameter(name)" v-model="editingParameterDefaults[name]" :disabled="parameterConfigLocked" class="full-width">
                    <el-option v-for="value in enumValues(name)" :key="String(value)" :label="String(value)" :value="value" />
                  </el-select>
                  <el-input-number v-else v-model="editingParameterDefaults[name]" :min="numericMinimum(name)" :max="numericMaximum(name)" :step="parameterSpec(name).kind === 'number' ? 0.1 : 1" :precision="parameterSpec(name).kind === 'number' ? 2 : 0" :disabled="parameterConfigLocked" class="full-width" controls-position="right" />
                </el-form-item>
              </template>
              <div class="preset-grid">
                <section v-for="preset in parameterPresetNames" :key="preset" class="preset-card">
                  <div class="meta-row"><strong>{{ preset === 'preview' ? '预览' : preset === 'standard' ? '标准生产' : '高质量' }}</strong><el-tag v-if="!editingParameterPresets[preset]" type="warning" size="small">未支持</el-tag></div>
                  <p v-if="!editingParameterPresets[preset]" class="form-help">当前 Profile 没有该预设，生产页会明确禁止选择，不会自动降级。</p>
                  <template v-else>
                    <template v-for="name in parameterNames" :key="`${preset}-${name}`">
                      <label class="preset-field">{{ name }}
                        <el-select v-if="isEnumParameter(name)" v-model="presetValues(preset)[name]" :disabled="parameterConfigLocked" class="full-width">
                          <el-option v-for="value in enumValues(name)" :key="String(value)" :label="String(value)" :value="value" />
                        </el-select>
                        <el-input-number v-else v-model="presetValues(preset)[name]" :min="numericMinimum(name)" :max="numericMaximum(name)" :step="parameterSpec(name).kind === 'number' ? 0.1 : 1" :precision="parameterSpec(name).kind === 'number' ? 2 : 0" :disabled="parameterConfigLocked" class="full-width" controls-position="right" />
                      </label>
                    </template>
                  </template>
                </section>
              </div>
            </template>

            <el-divider />
            <el-form-item label="给自己看的名称"><el-input v-model="displayName" maxlength="160" placeholder="例如：云雾 Claude 故事模型-测试版" /></el-form-item>
            <el-form-item label="模型版本（可不填）"><el-input v-model="modelVersion" maxlength="160" placeholder="例如：preview 或供应商版本号" /></el-form-item>
            <el-form-item label="预计每次生成费用（元，可不填）"><el-input-number v-model="estimatedCost" :min="0" :precision="4" :step="0.1" class="full-width" controls-position="right" /></el-form-item>
            <div class="form-help">这是质量报表用的人工预估值，不是供应商账单。</div>
            <el-form-item v-if="!editingProfileId" class="enable-switch">
              <el-switch v-model="enableAfterSave" active-text="保存后立即启用" />
            </el-form-item>

            <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="compact-alert" />
            <div class="form-actions">
              <el-button type="primary" native-type="submit" :loading="saving">{{ editingProfileId ? editingVersionLabel : `保存「${slotLabel}」候选` }}</el-button>
              <el-button v-if="editingProfileId" :disabled="saving" @click="resetToCandidate">取消编辑</el-button>
            </div>
          </el-form>
        </el-card>
      </el-col>

      <el-col :xs="24" :xl="15" class="versions-column">
        <el-card shadow="never" class="versions-card">
          <template #header>
            <div class="card-heading">
              <div>
                <span class="eyebrow">{{ slotLabel }}</span>
                <h2>模型版本列表</h2>
              </div>
              <el-badge :value="slotProfiles.length" type="primary"><span class="count-label">个版本</span></el-badge>
            </div>
          </template>

          <el-table :data="slotProfiles" v-loading="loading" empty-text="当前功能还没有候选模型" class="profiles-table">
            <el-table-column label="版本 / 状态" min-width="180">
              <template #default="{ row: profile }">
                <div class="profile-title">{{ profile.display_name }}</div>
                <el-space size="small" wrap>
                  <el-tag size="small" effect="plain">v{{ profile.version }}</el-tag>
                  <el-tag size="small" :type="profileStatusType(profile)">{{ profileStatusLabel(profile) }}</el-tag>
                  <el-tag v-if="profile.active_run_count" size="small" type="danger">{{ profile.active_run_count }} 个任务进行中</el-tag>
                </el-space>
              </template>
            </el-table-column>
            <el-table-column label="模型信息" min-width="220">
              <template #default="{ row: profile }">
                <div class="model-key">{{ profile.model_key }}</div>
                <div class="table-subline">{{ profile.model_version || '未填写模型版本' }}</div>
                <div class="table-subline">{{ profile.adapter_key }} · 优先级 {{ profile.priority ?? '—' }}</div>
              </template>
            </el-table-column>
            <el-table-column label="能力 / 预设" min-width="175">
              <template #default="{ row: profile }">
                <el-space size="small" wrap>
                  <el-tag size="small" effect="plain">{{ profile.parameter_config.capability }}</el-tag>
                  <el-tag v-for="preset in Object.keys(profile.parameter_config.presets)" :key="`${profile.id}-${preset}`" size="small" type="info" effect="plain">{{ preset }}</el-tag>
                </el-space>
                <div class="table-subline">参数：{{ Object.keys(profile.parameter_config.supported_parameters).join('、') || '无可调参数' }}</div>
                <div v-if="!profile.parameter_config_complete" class="legacy-parameter-warning">旧版本能力配置不完整；建议复制创建新版本。</div>
              </template>
            </el-table-column>
            <el-table-column label="可操作性" min-width="180">
              <template #default="{ row: profile }">
                <el-tag v-if="profile.has_model_invocations" size="small" type="warning" effect="plain">已有历史调用</el-tag>
                <span v-else class="table-subline">尚无调用，可安全修改</span>
                <el-tooltip v-if="!profile.can_delete" :content="profile.delete_block_reason || '当前不可删除'" placement="top">
                  <div class="blocked-reason">{{ profile.delete_block_reason }}</div>
                </el-tooltip>
                <div v-if="preflights[profile.id]" class="preflight-result">
                  <el-tag :type="preflights[profile.id].ready ? 'success' : 'danger'" size="small" effect="plain">
                    {{ preflights[profile.id].ready ? '基础预检通过' : '基础预检未通过' }}
                  </el-tag>
                  <div v-for="check in preflights[profile.id].checks" :key="`${profile.id}-${check.key}`" class="table-subline">
                    {{ check.key }}：{{ check.message }}
                  </div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="270" fixed="right">
              <template #default="{ row: profile }">
                <div class="row-actions">
                  <el-button link type="primary" :loading="preflightingProfileId === profile.id" :disabled="saving" @click="preflightProfile(profile)">基础预检</el-button>
                  <el-button v-if="profile.can_edit" link type="primary" :disabled="saving" @click="beginEdit(profile)">编辑</el-button>
                  <el-button link type="primary" :disabled="saving" @click="copyProfile(profile)">复制</el-button>
                  <el-button link :type="profile.is_enabled_in_slot ? 'warning' : 'success'" :disabled="saving" @click="toggleProfile(profile)">{{ profile.is_enabled_in_slot ? '停用' : '启用' }}</el-button>
                  <el-tooltip v-if="!profile.can_delete" :content="profile.delete_block_reason || '当前不可删除'" placement="top">
                    <span><el-button link type="danger" disabled>删除</el-button></span>
                  </el-tooltip>
                  <el-button v-else link type="danger" :disabled="saving" @click="removeProfile(profile)">删除</el-button>
                </div>
              </template>
            </el-table-column>
          </el-table>

          <el-alert title="提示：已有调用记录的模型不能覆盖修改或删除。请复制为新草稿，再修改、测试并人工启用。" type="warning" :closable="false" show-icon class="bottom-tip" />
        </el-card>
      </el-col>
    </el-row>
  </section>
</template>

<style scoped>
.model-center-page { display: grid; gap: 20px; }
.page-topbar { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; }
.page-topbar h1 { margin: 4px 0 8px; color: #1f2d3d; font-size: 30px; letter-spacing: -0.5px; }
.page-topbar p { margin: 0; color: #718096; }
.guide-alert, .bottom-tip { border-radius: 12px; }
.model-layout { align-items: flex-start; }
.versions-column { margin-top: 0; }
.config-card, .versions-card { border: 0; border-radius: 16px; box-shadow: 0 8px 28px rgb(31 45 61 / 7%); }
.card-heading { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.card-heading h2 { margin: 2px 0 0; color: #303133; font-size: 20px; }
.eyebrow { color: #409eff; font-size: 12px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; }
.full-width { width: 100%; }
.form-help { margin-top: 6px; color: #909399; font-size: 12px; line-height: 1.5; }
.compact-alert { margin: 0 0 18px; }
.parameter-summary { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 12px; }
.preset-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin: 6px 0 18px; }
.preset-card { display: grid; gap: 8px; padding: 12px; border: 1px solid #e4e7ed; border-radius: 10px; background: #fafcff; }
.preset-field { display: grid; gap: 4px; color: #606266; font-size: 12px; }
.legacy-parameter-warning { margin-top: 6px; color: #e6a23c; font-size: 12px; line-height: 1.45; }
.enable-switch { margin-top: 18px; }
.form-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 22px; }
.count-label { padding: 0 3px; color: #606266; }
.profiles-table { width: 100%; }
.profile-title, .model-key { margin-bottom: 7px; color: #303133; font-weight: 600; overflow-wrap: anywhere; }
.table-subline { color: #909399; font-size: 12px; line-height: 1.65; overflow-wrap: anywhere; }
.blocked-reason { margin-top: 6px; color: #f56c6c; font-size: 12px; line-height: 1.45; }
.preflight-result { display: grid; gap: 4px; margin-top: 8px; }
.row-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 0 4px; }
.bottom-tip { margin-top: 18px; }
@media (max-width: 1199px) { .versions-column { margin-top: 20px; } }
@media (max-width: 900px) { .preset-grid { grid-template-columns: 1fr; } }
@media (max-width: 640px) { .page-topbar { flex-direction: column; } .page-topbar h1 { font-size: 26px; } }
</style>
