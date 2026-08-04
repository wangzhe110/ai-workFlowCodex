<script setup lang="ts">
/**
 * LemonFlow V1 模型中心。
 *
 * 这个页面只面向 V1 主流程的“模型槽位”，不再把旧选题、旧分镜等历史步骤混给
 * 制作人员。用户只需要选择一个用途、填中转站展示的模型名，再决定是否人工启用。
 * 真实 API Key 绝不会由本页面收集或发送。
 */
import { computed, onMounted, ref, watch } from 'vue'
import {
  createV1ModelProfile,
  getModelSlots,
  getV1ModelProfiles,
  setV1ModelProfileEnabled,
} from '@/api/production'
import type { ModelSlot, V1ModelProfile } from '@/types/domain'

const slots = ref<ModelSlot[]>([])
const profiles = ref<V1ModelProfile[]>([])
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const notice = ref('')

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
const adapterKey = computed(() => {
  if (isVision.value) return 'openai_compatible_vision'
  if (isText.value) return 'openai_compatible'
  if (isImage.value) return 'openai_compatible_image'
  if (isVideo.value) return 'volcengine_ark_video'
  return 'ffmpeg_concat'
})
const slotProfiles = computed(() => profiles.value.filter((item) => item.slot_key === slotKey.value))

/** 重新读取，确保“是否已启用”的显示来自服务端正式状态，而非浏览器猜测。 */
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
    error.value = cause instanceof Error ? cause.message : '模型中心加载失败，请确认后端服务已启动。'
  } finally {
    loading.value = false
  }
}

/** 切换用途时填入安全的推荐值；用户只需从中转站复制“模型名称”。 */
function applySlotTemplate(next: string) {
  error.value = ''
  notice.value = ''
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
watch(slotKey, applySlotTemplate)

function currentProviderConfig(): Record<string, unknown> {
  if (isVideo.value) {
    return withEstimatedCost({ secret_env_name: 'ARK_API_KEY', ratio: videoRatio.value, duration: videoDuration.value })
  }
  if (isFinalCompose.value) return withEstimatedCost({})
  const config: Record<string, unknown> = {
    api_base_url: apiBaseUrl.value.trim(),
    secret_env_name: secretEnvName.value.trim(),
  }
  if (isVision.value) {
    // 指定 V1 契约，确保审核页一定会得到五类正式分析字段。
    config.result_contract = 'V1_REFERENCE_ANALYSIS'
    config.frame_sample_count = 6
  }
  if (isImage.value) config.image_size = imageSize.value.trim()
  if (isKeyframe.value) config.reference_image_field = referenceImageField.value.trim()
  return withEstimatedCost(config)
}

/** 成本是人为填写的单次预估，不是模型厂商账单；未填写就不会显示成本平均值。 */
function withEstimatedCost(config: Record<string, unknown>): Record<string, unknown> {
  if (estimatedCost.value !== null && Number.isFinite(estimatedCost.value) && estimatedCost.value >= 0) {
    config.estimated_cost_per_call = estimatedCost.value
    config.currency = 'CNY'
  }
  return config
}

async function saveCandidate() {
  if (!modelKey.value.trim() || !displayName.value.trim()) {
    error.value = '请填写模型名称和给自己看的备注名称。模型名称请直接从中转站后台复制。'
    return
  }
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    const shouldReplace = Boolean(enableAfterSave.value && selectedSlot.value?.selection_mode === 'SINGLE')
    await createV1ModelProfile({
      slot_key: slotKey.value,
      adapter_key: adapterKey.value,
      model_key: modelKey.value.trim(),
      display_name: displayName.value.trim(),
      model_version: modelVersion.value.trim() || undefined,
      provider_config: currentProviderConfig(),
      enable_in_slot: enableAfterSave.value,
      replace_existing: shouldReplace,
      priority: 100,
    })
    notice.value = enableAfterSave.value
      ? '已保存并按你的确认启用。以前的版本仍保留，可随时人工切回。'
      : '已保存为候选。请确认服务器已经配置 Key，再点击下方“启用此版本”。'
    await load()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '保存失败，请检查填写内容。'
  } finally {
    saving.value = false
  }
}

/** 单模型槽位会在用户点击时替换旧版本；故事槽位则允许多个真实模型并行。 */
async function toggleProfile(profile: V1ModelProfile) {
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    const slot = slots.value.find((item) => item.slot_key === profile.slot_key)
    const enable = !profile.is_enabled_in_slot
    await setV1ModelProfileEnabled(
      profile.slot_key,
      profile.id,
      enable,
      Boolean(enable && slot?.selection_mode === 'SINGLE'),
    )
    notice.value = enable ? '已按你的确认启用该版本。' : '已停止在该槽位使用此版本，历史记录仍会保留。'
    await load()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '切换失败，请重试。'
  } finally {
    saving.value = false
  }
}

onMounted(load)
applySlotTemplate(slotKey.value)
</script>

<template>
  <section class="page-heading">
    <div>
      <RouterLink class="muted" to="/">← 返回项目</RouterLink>
      <h1>V1 模型中心</h1>
      <p>按“功能”配置模型，不需要理解程序代码。系统不会自动换模型，任何启用与替换都需要你点击确认。</p>
    </div>
  </section>

  <section class="panel stack">
    <h2>先理解这三件事</h2>
    <ol class="setup-steps">
      <li>先让负责人把 API Key 写入服务器的 <code>infra/backend.env</code>；这里不填写 Key。</li>
      <li>选择一个用途，把中转站后台显示的“模型名称”复制进来，先保存为候选。</li>
      <li>用小项目测试后，再点“启用此版本”。故事生成可启用多个模型并行出方案。</li>
    </ol>
    <p class="notice info">提示：真实视频需要公网 HTTPS 图片。正式生产建议开启图片对象存储转存；本地模拟模式不产生真实图片或视频。</p>
  </section>

  <div class="grid">
    <form class="panel stack" @submit.prevent="saveCandidate">
      <h2>添加 V1 候选模型</h2>
      <label class="field">它要完成什么功能？
        <select v-model="slotKey">
          <option v-for="slot in slots" :key="slot.id" :value="slot.slot_key">
            {{ slotLabels[slot.slot_key] || slot.description }}
          </option>
        </select>
      </label>
      <p class="notice info">{{ selectedSlot?.description || '正在读取功能说明…' }}</p>

      <template v-if="isVideo">
        <p class="notice info">系统使用火山方舟原生 Adapter：模型、任务提交、轮询和首帧字段均由系统处理，不用填写 API 地址。</p>
        <label class="field">模型名称<input v-model="modelKey" maxlength="160" /></label>
        <label class="field">视频画幅
          <select v-model="videoRatio"><option value="9:16">竖屏短剧（推荐）</option><option value="16:9">横屏</option><option value="1:1">方形</option></select>
        </label>
        <label class="field">每段时长
          <select v-model.number="videoDuration"><option :value="3">3 秒（测试）</option><option :value="5">5 秒（推荐）</option><option :value="8">8 秒</option></select>
        </label>
      </template>

      <template v-else-if="isFinalCompose">
        <p class="notice info">这一步不调用 AI。服务器用 FFmpeg 把人工审核通过的视频片段合成为完整 MP4。</p>
        <label class="field">合成器名称<input v-model="modelKey" readonly /></label>
      </template>

      <template v-else>
        <label class="field">模型名称
          <input v-model="modelKey" placeholder="从云雾或中转站后台复制" maxlength="160" />
        </label>
        <small class="muted">不要猜名称。中转站后台显示什么，就复制什么。</small>
        <label class="field">中转站 API 地址<input v-model="apiBaseUrl" type="url" placeholder="https://…/v1" /></label>
        <label class="field">服务器里保存 Key 的变量名<input v-model="secretEnvName" placeholder="例如 YUNWU_API_KEY" /></label>
        <p class="muted">这里只写变量名，不写 Key 内容。实际 Key 只能放在服务器的环境文件里。</p>
      </template>

      <template v-if="isImage">
        <label class="field">图片尺寸<input v-model="imageSize" placeholder="例如 1728x2304" /></label>
        <label v-if="isKeyframe" class="field">中转站文档中的“参考图字段名”
          <input v-model="referenceImageField" placeholder="通常是 images，以文档为准" />
        </label>
        <small v-if="isKeyframe" class="muted">关键帧必须使用已经锁定的角色图和场景图。若中转站不支持参考图，请不要用它生成关键帧。</small>
      </template>

      <label class="field">给自己看的名称<input v-model="displayName" maxlength="160" placeholder="例如：云雾 Claude 故事模型-测试版" /></label>
      <label class="field">模型版本（可不填）<input v-model="modelVersion" maxlength="160" placeholder="例如：preview 或供应商版本号" /></label>
      <label class="field">预计每次生成费用（元，可不填）<input v-model.number="estimatedCost" type="number" min="0" step="0.0001" placeholder="例如 0.8" /></label>
      <small class="muted">这是便于比较的预估值，不是平台扣费；不填就不会在质量报表中显示平均成本。</small>
      <label class="field"><span><input v-model="enableAfterSave" type="checkbox" /> 我已经测试过，保存后立即启用</span></label>
      <p class="muted">单模型功能会替换当前版本；“并行生成原创故事”会保留多个已启用模型同时出方案。</p>
      <RouterLink class="button secondary" to="/model-quality">查看模型质量与成本报表</RouterLink>
      <RouterLink class="button secondary" to="/prompt-templates">管理 Prompt 模板版本</RouterLink>

      <p v-if="error" class="notice error">{{ error }}</p>
      <p v-if="notice" class="notice success">{{ notice }}</p>
      <button class="button" :disabled="saving">{{ saving ? '保存中…' : `保存「${slotLabel}」候选` }}</button>
    </form>

    <section class="panel stack">
      <div class="meta-row"><h2>当前功能的模型版本</h2><span>{{ slotProfiles.length }} 项</span></div>
      <p v-if="loading" class="muted">正在读取…</p>
      <p v-else-if="!slotProfiles.length" class="muted">还没有候选模型。你可以先使用系统的本地模拟模式完成流程演示。</p>
      <article v-for="profile in slotProfiles" :key="profile.id" class="panel stack">
        <div class="meta-row">
          <strong>{{ profile.display_name }} · 第 {{ profile.version }} 版</strong>
          <span :class="profile.is_enabled_in_slot ? 'status success' : 'status muted'">{{ profile.is_enabled_in_slot ? '当前启用' : '候选未启用' }}</span>
        </div>
        <p class="muted">模型：{{ profile.model_key }}<span v-if="profile.model_version"> · {{ profile.model_version }}</span></p>
        <p class="muted">协议：{{ profile.adapter_key }} · 优先级：{{ profile.priority ?? '—' }}</p>
        <p v-if="profile.adapter_key === 'mock_v1'" class="notice info">这是本地模拟，不读取你的 Key，也不会生成真实视频。</p>
        <button class="button secondary" :disabled="saving" @click="toggleProfile(profile)">
          {{ profile.is_enabled_in_slot ? '停止使用此版本' : '启用此版本' }}
        </button>
      </article>
    </section>
  </div>
</template>
