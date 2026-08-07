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
  setV1ModelProfileEnabled,
  updateV1ModelProfile,
} from '@/api/production'
import type { ModelSlot, V1ModelProfile } from '@/types/domain'

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
const secretEnvName = ref('YUNWU_API_KEY')
const imageSize = ref('1728x2304')
const referenceImageField = ref('images')
const videoRatio = ref('9:16')
const videoDuration = ref(5)
const estimatedCost = ref<number | null>(null)
const enableAfterSave = ref(false)
const editingProfileId = ref<string | null>(null)
const editingAdapterKey = ref('')
const editingProviderConfig = ref<Record<string, unknown>>({})

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
  if (isImage.value) return 'openai_compatible_image'
  if (isVideo.value) return 'volcengine_ark_video'
  return 'ffmpeg_concat'
})
const adapterKey = computed(() => editingAdapterKey.value || defaultAdapterKey.value)
const slotProfiles = computed(() => profiles.value.filter((item) => item.slot_key === slotKey.value))
const editingVersionLabel = computed(() => {
  const profile = profiles.value.find((item) => item.id === editingProfileId.value)
  return profile ? `保存第 ${profile.version} 版修改` : '保存当前版本修改'
})

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
  secretEnvName.value = 'YUNWU_API_KEY'
  if (next === 'VIDEO_ANALYSIS') {
    displayName.value = '参考视频分析模型'
    modelKey.value = ''
  } else if (['STORY_GENERATE', 'CHARACTER_DESIGN', 'SCENE_DESIGN', 'DIRECTOR_PLAN'].includes(next)) {
    displayName.value = next === 'STORY_GENERATE' ? '原创故事导演模型' : 'AI 导演文本模型'
    modelKey.value = ''
  } else if (['CHARACTER_IMAGE_GENERATE', 'SCENE_IMAGE_GENERATE', 'SHOT_KEYFRAME_GENERATE'].includes(next)) {
    displayName.value = next === 'SHOT_KEYFRAME_GENERATE' ? '参考生图关键帧模型' : '图片资产模型'
    modelKey.value = ''
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
    return withEstimatedCost({ ...preserved, secret_env_name: 'ARK_API_KEY', ratio: videoRatio.value, duration: videoDuration.value })
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
  if (isImage.value) config.image_size = imageSize.value.trim()
  if (isKeyframe.value) config.reference_image_field = referenceImageField.value.trim()
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
  slotKey.value = profile.slot_key
  modelKey.value = profile.model_key
  displayName.value = profile.display_name
  modelVersion.value = profile.model_version || ''
  apiBaseUrl.value = String(profile.provider_config.api_base_url || '')
  secretEnvName.value = String(profile.provider_config.secret_env_name || '')
  imageSize.value = String(profile.provider_config.image_size || '')
  referenceImageField.value = String(profile.provider_config.reference_image_field || '')
  videoRatio.value = String(profile.provider_config.ratio || '9:16')
  videoDuration.value = Number(profile.provider_config.duration || 5)
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
              <el-alert title="视频生成使用火山方舟原生协议，不需要填写 API 地址。" type="info" :closable="false" show-icon class="compact-alert" />
              <el-form-item label="模型名称"><el-input v-model="modelKey" maxlength="160" /></el-form-item>
              <el-form-item label="视频画幅">
                <el-select v-model="videoRatio" class="full-width">
                  <el-option label="竖屏短剧（9:16）" value="9:16" />
                  <el-option label="横屏（16:9）" value="16:9" />
                  <el-option label="方形（1:1）" value="1:1" />
                </el-select>
              </el-form-item>
              <el-form-item label="每段时长"><el-select v-model="videoDuration" class="full-width"><el-option :value="3" label="3 秒（测试）" /><el-option :value="5" label="5 秒（推荐）" /><el-option :value="8" label="8 秒" /></el-select></el-form-item>
            </template>

            <template v-else-if="isFinalCompose">
              <el-alert title="这一环节由服务器 FFmpeg 合成已审核视频，不会调用 AI。" type="info" :closable="false" show-icon class="compact-alert" />
              <el-form-item label="合成器名称"><el-input v-model="modelKey" readonly /></el-form-item>
            </template>

            <template v-else>
              <el-form-item label="模型名称"><el-input v-model="modelKey" placeholder="从中转站后台直接复制" maxlength="160" /></el-form-item>
              <el-form-item label="中转站 API 地址"><el-input v-model="apiBaseUrl" placeholder="https://…/v1" /></el-form-item>
              <el-form-item label="服务器密钥变量名"><el-input v-model="secretEnvName" placeholder="例如 YUNWU_API_KEY" /></el-form-item>
              <div class="form-help">只填变量名，绝不在网页粘贴 API Key。</div>
            </template>

            <template v-if="isImage">
              <el-form-item label="图片尺寸"><el-input v-model="imageSize" placeholder="例如 1728x2304" /></el-form-item>
              <el-form-item v-if="isKeyframe" label="中转站文档中的参考图字段名"><el-input v-model="referenceImageField" placeholder="通常为 images，以文档为准" /></el-form-item>
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
            <el-table-column label="可操作性" min-width="180">
              <template #default="{ row: profile }">
                <el-tag v-if="profile.has_model_invocations" size="small" type="warning" effect="plain">已有历史调用</el-tag>
                <span v-else class="table-subline">尚无调用，可安全修改</span>
                <el-tooltip v-if="!profile.can_delete" :content="profile.delete_block_reason || '当前不可删除'" placement="top">
                  <div class="blocked-reason">{{ profile.delete_block_reason }}</div>
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="210" fixed="right">
              <template #default="{ row: profile }">
                <div class="row-actions">
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
.enable-switch { margin-top: 18px; }
.form-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 22px; }
.count-label { padding: 0 3px; color: #606266; }
.profiles-table { width: 100%; }
.profile-title, .model-key { margin-bottom: 7px; color: #303133; font-weight: 600; overflow-wrap: anywhere; }
.table-subline { color: #909399; font-size: 12px; line-height: 1.65; overflow-wrap: anywhere; }
.blocked-reason { margin-top: 6px; color: #f56c6c; font-size: 12px; line-height: 1.45; }
.row-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 0 4px; }
.bottom-tip { margin-top: 18px; }
@media (max-width: 1199px) { .versions-column { margin-top: 20px; } }
@media (max-width: 640px) { .page-topbar { flex-direction: column; } .page-topbar h1 { font-size: 26px; } }
</style>
