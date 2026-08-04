<script setup lang="ts">
/**
 * 模型配置中心。
 *
 * 这一页优先服务第一次接触模型配置的制作负责人：常用操作只需选择用途、填写模型名，
 * API 地址、密钥变量名和资源预算均使用安全模板。中转站差异较大的视频参数仍保留在
 * 折叠的高级设置中，避免普通用户误填并产生付费任务。
 */
import { computed, onMounted, ref, watch } from 'vue'
import ModelEvaluationPanel from '@/components/ModelEvaluationPanel.vue'
import { useModelProfilesStore } from '@/stores/model-profiles'

const store = useModelProfilesStore()
const stepKey = ref('generate_story_package')
const providerKey = ref('openai_compatible')
const modelKey = ref('')
const displayName = ref('云雾文本模型')
const apiBaseUrl = ref('https://yunwu.ai/v1')
const secretEnvName = ref('YUNWU_API_KEY')
const timeoutSeconds = ref<number | null>(null)
const imageSize = ref('1728x2304')
const frameSampleCount = ref(6)
const frameExtractionTimeoutSeconds = ref(120)
const frameMaxBytes = ref(2 * 1024 * 1024)
const visionRequestOptionsText = ref('{}')
const audioMaxDurationSeconds = ref(180)
const audioExtractionTimeoutSeconds = ref(120)
const audioMaxBytes = ref(8 * 1024 * 1024)
const transcriptionRequestOptionsText = ref('{}')
const finalVideoDownloadTimeoutSeconds = ref(120)
const finalVideoMaxClipBytes = ref(500 * 1024 * 1024)
const finalVideoMaxOutputBytes = ref(2 * 1024 * 1024 * 1024)
const finalVideoRenderTimeoutSeconds = ref(1800)
const videoRatio = ref('9:16')
const videoDuration = ref(5)
const activate = ref(false)
const comparisonStepKey = ref('generate_story_package')

/** 页面只展示中文用途；内部步骤标识仍由程序保存，避免用户接触无意义的英文 Key。 */
const steps = [
  ['transcribe_reference_audio', '参考视频里的语音'],
  ['analyze_reference_mechanisms', '参考视频的画面与爆点规律'],
  ['generate_original_topics', '原创选题'],
  ['generate_story_package', '故事创作'],
  ['generate_storyboard', '分镜细纲'],
  ['generate_storyboard_images', '分镜图片'],
  ['generate_storyboard_video_groups', '视频片段'],
  ['assemble_final_video', '合成完整 MP4'],
] as const
const stepLabels: Record<string, string> = Object.fromEntries(steps)
const stepName = computed(() => stepLabels[stepKey.value] || stepKey.value)
const isImageStep = computed(() => stepKey.value === 'generate_storyboard_images')
const isVideoStep = computed(() => stepKey.value === 'generate_storyboard_video_groups')
const isVisionStep = computed(() => stepKey.value === 'analyze_reference_mechanisms')
const isTranscriptionStep = computed(() => stepKey.value === 'transcribe_reference_audio')
const isFinalVideoStep = computed(() => stepKey.value === 'assemble_final_video')

/** 根据业务用途给出非技术化的说明，让使用者先理解“为什么需要这个模型”。 */
const stepHelp = computed(() => {
  if (isFinalVideoStep.value) {
    return '这一步不选 AI 模型。系统会把已经生成成功的视频片段按顺序合成为一个 MP4 文件。'
  }
  if (isVideoStep.value) {
    return '参考流程使用的是豆包 Seedance 2.0 Mini。系统已按火山方舟官方协议接好创建任务、查询结果和首帧图片，不需要填写 API 路径或 JSON。'
  }
  if (isImageStep.value) {
    return '填写你在中转站后台看到的图片模型名称。首次测试只生成 1–2 张图片，确认尺寸和风格后再启用。'
  }
  if (isVisionStep.value) {
    return '填写支持“看图片”的视觉模型名称。它只提炼开头钩子、冲突和节奏等抽象规律，不应复述原视频。'
  }
  if (isTranscriptionStep.value) {
    return '填写语音转写模型名称。系统只临时分析参考视频开头的声音，原台词不会保存到页面。'
  }
  return '填写支持中文和 JSON 输出的文本模型名称。这个模型负责生成原创内容，不会直接复制参考视频。'
})

const providerLabels: Record<string, string> = {
  mock_provider: '本地模拟（不消耗模型额度）',
  openai_compatible: '文本模型（OpenAI 兼容）',
  openai_compatible_image: '图片模型（OpenAI 兼容）',
  openai_compatible_vision: '视觉分析模型（OpenAI 兼容）',
  openai_compatible_transcription: '语音转写模型（OpenAI 兼容）',
  configurable_async_video: '异步图生视频模型',
  volcengine_ark_video: '豆包 Seedance 2.0 Mini（火山方舟）',
  ffmpeg_concat: '本地 FFmpeg 合成',
}

function providerLabel(key: string): string {
  return providerLabels[key] || key
}

/**
 * 选择用途后恢复推荐模板。真实密钥不在这里出现；YUNWU_API_KEY 只是服务器里的变量名。
 * 切换步骤时清空模型名，防止把文本模型误用于图片或视频步骤。
 */
function applyStepTemplate(nextStep: string) {
  modelKey.value = ''
  timeoutSeconds.value = null
  secretEnvName.value = 'YUNWU_API_KEY'
  apiBaseUrl.value = 'https://yunwu.ai/v1'

  if (nextStep === 'transcribe_reference_audio') {
    providerKey.value = 'openai_compatible_transcription'
    displayName.value = '云雾语音转写模型'
    return
  }
  if (nextStep === 'analyze_reference_mechanisms') {
    providerKey.value = 'openai_compatible_vision'
    displayName.value = '云雾视觉分析模型'
    return
  }
  if (nextStep === 'generate_storyboard_images') {
    providerKey.value = 'openai_compatible_image'
    displayName.value = '云雾图片模型'
    return
  }
  if (nextStep === 'generate_storyboard_video_groups') {
    providerKey.value = 'volcengine_ark_video'
    modelKey.value = 'doubao-seedance-2-0-mini-260615'
    displayName.value = '豆包 Seedance 2.0 Mini 视频模型'
    // 火山方舟地址和任务协议由原生适配器固定管理，制作人员无需接触。
    apiBaseUrl.value = ''
    secretEnvName.value = ''
    return
  }
  if (nextStep === 'assemble_final_video') {
    providerKey.value = 'ffmpeg_concat'
    modelKey.value = 'ffmpeg-concat-v1'
    displayName.value = '完整 MP4 合成'
    apiBaseUrl.value = ''
    secretEnvName.value = ''
    return
  }
  providerKey.value = 'openai_compatible'
  displayName.value = '云雾文本模型'
}

onMounted(() => void store.load())
watch(stepKey, applyStepTemplate)

/** 仅组装非敏感配置；真实密钥只能由服务器读取，永远不进入浏览器。 */
function providerConfig(): Record<string, unknown> {
  const parseObject = (value: string, label: string): Record<string, unknown> => {
    try {
      const parsed: unknown = JSON.parse(value || '{}')
      if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('not-object')
      return parsed as Record<string, unknown>
    } catch {
      throw new Error(`${label}格式不正确。若你不确定，请保持系统提供的默认内容并联系有 API 文档的人。`)
    }
  }
  let visionRequestOptions: Record<string, unknown> = {}
  let transcriptionRequestOptions: Record<string, unknown> = {}
  if (isVisionStep.value) visionRequestOptions = parseObject(visionRequestOptionsText.value, '视觉模型高级参数')
  if (isTranscriptionStep.value) transcriptionRequestOptions = parseObject(transcriptionRequestOptionsText.value, '语音转写高级参数')
  return {
    ...(displayName.value.trim() ? { display_name: displayName.value.trim() } : {}),
    ...(apiBaseUrl.value.trim() ? { api_base_url: apiBaseUrl.value.trim() } : {}),
    ...(secretEnvName.value.trim() ? { secret_env_name: secretEnvName.value.trim() } : {}),
    ...(timeoutSeconds.value ? { timeout_seconds: timeoutSeconds.value } : {}),
    ...(isImageStep.value && imageSize.value.trim() ? { image_size: imageSize.value.trim() } : {}),
    ...(isVisionStep.value ? {
      frame_sample_count: frameSampleCount.value,
      frame_extraction_timeout_seconds: frameExtractionTimeoutSeconds.value,
      frame_max_bytes: frameMaxBytes.value,
      vision_request_options: visionRequestOptions,
    } : {}),
    ...(isTranscriptionStep.value ? {
      audio_max_duration_seconds: audioMaxDurationSeconds.value,
      audio_extraction_timeout_seconds: audioExtractionTimeoutSeconds.value,
      audio_max_bytes: audioMaxBytes.value,
      transcription_request_options: transcriptionRequestOptions,
    } : {}),
    ...(isFinalVideoStep.value ? {
      download_timeout_seconds: finalVideoDownloadTimeoutSeconds.value,
      max_clip_bytes: finalVideoMaxClipBytes.value,
      max_output_bytes: finalVideoMaxOutputBytes.value,
      render_timeout_seconds: finalVideoRenderTimeoutSeconds.value,
    } : {}),
    ...(isVideoStep.value ? {
      secret_env_name: 'ARK_API_KEY',
      ratio: videoRatio.value,
      duration: videoDuration.value,
    } : {}),
  }
}

async function submit() {
  if (!modelKey.value.trim()) {
    store.error = isFinalVideoStep.value ? '请保留系统自动填写的合成器名称' : '请从中转站后台复制并填写“模型名称”'
    return
  }
  let config: Record<string, unknown>
  try {
    config = providerConfig()
  } catch (error) {
    store.error = error instanceof Error ? error.message : '模型配置格式不正确'
    return
  }
  const created = await store.create({
    step_key: stepKey.value,
    provider_key: providerKey.value,
    model_key: modelKey.value,
    provider_config: config,
    activate: activate.value,
  })
  if (created) {
    modelKey.value = isFinalVideoStep.value ? 'ffmpeg-concat-v1' : ''
    timeoutSeconds.value = null
    activate.value = false
  }
}
</script>

<template>
  <section class="page-heading">
    <div>
      <RouterLink class="muted" to="/">← 返回项目</RouterLink>
      <h1>模型配置中心</h1>
      <p>只要按用途选择、填模型名称、测试后启用。真实 API Key 不需要、也不能填写在本页。</p>
    </div>
  </section>

  <section class="panel stack">
    <h2>第一次配置，只做这 4 件事</h2>
    <ol class="setup-steps">
      <li>先由负责人把 API Key 写入服务器的 <code>infra/backend.env</code> 文件。</li>
      <li>在下方选择要使用的功能，例如“故事创作”。</li>
      <li>从云雾/中转站后台复制模型名称，粘贴到“模型名称”。</li>
      <li>保存为候选 → 点击基础预检 → 用小样本测试 → 确认后启用。</li>
    </ol>
    <p class="notice info">你不需要理解“供应商标识、JSON、请求路径”等词。文本、图片和豆包视频的常用配置均已内置。</p>
  </section>

  <div class="grid">
    <form class="panel stack" @submit.prevent="submit">
      <h2>添加一个模型</h2>
      <label class="field">这台模型要做什么？
        <select v-model="stepKey">
          <option v-for="[key, label] in steps" :key="key" :value="key">{{ label }}</option>
        </select>
      </label>
      <p class="notice info">{{ stepHelp }}</p>

      <template v-if="isFinalVideoStep">
        <p class="notice info">已自动选择本机 MP4 合成器。不消耗模型额度，也不需要填写 API Key。</p>
      </template>
      <template v-else-if="isVideoStep">
        <p class="notice info">当前固定模型：<strong>doubao-seedance-2-0-mini-260615</strong>（豆包 Seedance 2.0 Mini）。你不用填写模型名称或 API Key。</p>
        <label class="field">视频画幅
          <select v-model="videoRatio">
            <option value="9:16">竖屏短剧（9:16，推荐）</option>
            <option value="16:9">横屏（16:9）</option>
            <option value="1:1">方形（1:1）</option>
          </select>
        </label>
        <label class="field">每段视频时长
          <select v-model.number="videoDuration">
            <option :value="3">3 秒（测试）</option>
            <option :value="5">5 秒（推荐）</option>
            <option :value="8">8 秒</option>
          </select>
        </label>
        <p class="notice info">负责人只需在服务器配置一次 <code>ARK_API_KEY</code>。保存后先做预检；预检通过后，第一次只生成 1 组镜头。</p>
      </template>
      <template v-else>
        <label class="field">模型名称
          <input v-model="modelKey" placeholder="从云雾或中转站后台复制模型名称" maxlength="160" />
        </label>
        <small class="muted">不要猜模型名称；复制中转站后台实际显示的名称即可。</small>
      </template>

      <template v-if="isImageStep">
        <label class="field">图片尺寸
          <select v-model="imageSize">
            <option value="1728x2304">竖屏短剧（1728 × 2304，推荐）</option>
            <option value="1024x1536">竖屏测试（1024 × 1536，成本较低）</option>
            <option value="1024x1024">方图测试（1024 × 1024）</option>
          </select>
        </label>
        <small class="muted">如果模型后台不支持所选尺寸，请改选它支持的尺寸，或在高级设置中填写。</small>
      </template>

      <label class="field">给自己看的备注（可不填）
        <input v-model="displayName" placeholder="例如：云雾文本模型-测试版" />
      </label>
      <label class="field"><span><input v-model="activate" type="checkbox" /> 我已经测试过，保存后立即启用</span></label>
      <p class="muted">第一次请不要勾选。先保存为候选，预检和小样本都通过后，再点击右侧的“启用此版本”。</p>

      <details class="advanced-settings">
        <summary>高级设置（第一次通常不用打开）</summary>
        <div class="stack advanced-content">
          <p class="notice info">只有更换中转站、模型文档明确要求不同参数，或有技术人员协助时，才修改这里。API Key 的真实内容永远不能填在这里。</p>
          <template v-if="!isFinalVideoStep && !isVideoStep">
            <label class="field">中转站类型<input v-model="providerKey" maxlength="80" /></label>
            <label class="field">API 地址<input v-model="apiBaseUrl" type="url" placeholder="https://…" /></label>
            <label class="field">服务器密钥变量名<input v-model="secretEnvName" placeholder="例如 YUNWU_API_KEY" /></label>
            <label class="field">最长等待时间（秒，可不填）<input v-model.number="timeoutSeconds" type="number" min="1" max="1800" /></label>
          </template>
          <label v-if="isImageStep" class="field">自定义图片尺寸<input v-model="imageSize" placeholder="例如 1728x2304" /></label>

          <template v-if="isVisionStep">
            <label class="field">抽取多少张画面<input v-model.number="frameSampleCount" type="number" min="1" max="12" /></label>
            <label class="field">抽帧最长等待（秒）<input v-model.number="frameExtractionTimeoutSeconds" type="number" min="5" max="300" /></label>
            <label class="field">每张画面最大体积（字节）<input v-model.number="frameMaxBytes" type="number" min="65536" :max="8 * 1024 * 1024" /></label>
            <label class="field">模型额外参数（JSON）<textarea v-model="visionRequestOptionsText" rows="3" placeholder='例如 {"top_p":0.9}' /></label>
          </template>

          <template v-if="isTranscriptionStep">
            <label class="field">分析开头声音时长（秒）<input v-model.number="audioMaxDurationSeconds" type="number" min="5" max="600" /></label>
            <label class="field">提取声音最长等待（秒）<input v-model.number="audioExtractionTimeoutSeconds" type="number" min="5" max="300" /></label>
            <label class="field">声音文件最大体积（字节）<input v-model.number="audioMaxBytes" type="number" min="65536" :max="50 * 1024 * 1024" /></label>
            <label class="field">模型额外参数（JSON）<textarea v-model="transcriptionRequestOptionsText" rows="3" placeholder='例如 {"language":"zh"}' /></label>
          </template>

          <template v-if="isVideoStep">
            <p class="notice info">豆包视频已使用火山方舟原生适配器：系统自动提交视频任务、传入第一张分镜图、等待生成完成并保存视频地址。这里没有需要你填写的技术参数。</p>
          </template>

          <template v-if="isFinalVideoStep">
            <label class="field">单段视频下载最长等待（秒）<input v-model.number="finalVideoDownloadTimeoutSeconds" type="number" min="5" max="600" /></label>
            <label class="field">单段视频最大体积（字节）<input v-model.number="finalVideoMaxClipBytes" type="number" min="1048576" :max="2 * 1024 * 1024 * 1024" /></label>
            <label class="field">完整 MP4 最大体积（字节）<input v-model.number="finalVideoMaxOutputBytes" type="number" min="1048576" :max="10 * 1024 * 1024 * 1024" /></label>
            <label class="field">合成最长等待（秒）<input v-model.number="finalVideoRenderTimeoutSeconds" type="number" min="30" max="7200" /></label>
          </template>
        </div>
      </details>

      <p v-if="store.error" class="notice error">{{ store.error }}</p>
      <button class="button" :disabled="store.submitting">{{ store.submitting ? '保存中…' : `保存「${stepName}」模型` }}</button>
    </form>

    <section class="panel stack">
      <div class="meta-row"><h2>已经保存的模型</h2><span>{{ store.profiles.length }} 项</span></div>
      <p class="muted">建议顺序：先点击“基础预检”，再用一个小项目测试；确认没有问题后才启用。</p>
      <p v-if="store.loading" class="muted">正在加载…</p>
      <article v-for="profile in store.profiles" :key="profile.id" class="panel stack">
        <div class="meta-row">
          <strong>{{ stepLabels[profile.step_key] || profile.step_key }} · 第 {{ profile.version }} 版</strong>
          <span class="status" :class="profile.is_active ? 'SUCCEEDED' : 'PENDING'">{{ profile.is_active ? '正在使用' : '候选，尚未启用' }}</span>
        </div>
        <p>{{ profile.provider_config.display_name || profile.model_key }}</p>
        <small class="muted">模型名称：{{ profile.model_key }} · {{ providerLabel(profile.provider_key) }} · {{ profile.adapter_available ? '系统已支持' : '当前系统尚不支持' }}</small>
        <button class="button secondary" :disabled="store.preflightingProfileId === profile.id" @click="store.preflight(profile.id)">
          {{ store.preflightingProfileId === profile.id ? '正在检查…' : '第一步：基础预检（不生成、不扣费）' }}
        </button>
        <div v-if="store.preflights[profile.id]" class="stack">
          <p class="notice" :class="store.preflights[profile.id].ready ? 'info' : 'error'">
            {{ store.preflights[profile.id].ready ? '基础检查通过。现在请用少量内容测试，满意后再启用。' : '基础检查没有通过。请根据下方说明修正后重试。' }}
          </p>
          <ul class="muted">
            <li v-for="check in store.preflights[profile.id].checks" :key="check.key">
              {{ check.status === 'passed' ? '通过' : check.status === 'warning' ? '注意' : '需要处理' }}：{{ check.message }}
            </li>
          </ul>
        </div>
        <details class="advanced-settings">
          <summary>记录/查看这台模型的测试结果（可选）</summary>
          <div class="advanced-content">
            <ModelEvaluationPanel
              :profile-id="profile.id"
              :profile-label="`${stepLabels[profile.step_key] || profile.step_key} 第 ${profile.version} 版`"
              :evaluations="store.evaluations[profile.id] || []"
              :loading="store.evaluationLoadingProfileId === profile.id"
              :saving="store.evaluationSavingProfileId === profile.id"
              :load-evaluations="(profileId) => store.loadEvaluations(profileId)"
              :save-evaluation="(profileId, payload) => store.createEvaluation(profileId, payload)"
            />
          </div>
        </details>
        <button v-if="!profile.is_active" class="button secondary" :disabled="!profile.adapter_available || store.submitting" @click="store.activate(profile.id)">最后一步：启用此版本</button>
      </article>
    </section>
  </div>

  <details class="panel advanced-settings">
    <summary>模型测试结果对比（可选）</summary>
    <div class="stack advanced-content">
      <p class="muted">同一种用途、同一个测试场景的模型才适合比较。刚开始使用时可以先跳过这里。</p>
      <label class="field">想比较哪一种用途？
        <select v-model="comparisonStepKey">
          <option v-for="[key, label] in steps" :key="key" :value="key">{{ label }}</option>
        </select>
      </label>
      <button class="button secondary" :disabled="store.comparisonLoading" @click="store.loadComparisons(comparisonStepKey)">
        {{ store.comparisonLoading ? '读取中…' : '查看测试结果对比' }}
      </button>
      <p v-if="store.comparisons.length === 0 && !store.comparisonLoading" class="muted">选择用途后查看已保存的测试记录。</p>
      <article v-for="record in store.comparisons" :key="record.id" class="panel stack">
        <div class="meta-row">
          <strong>{{ record.display_name || record.model_key }} · 第 {{ record.profile_version }} 版</strong>
          <span>人工质量评分 {{ record.quality_score }}/100</span>
        </div>
        <p>{{ record.scenario }}</p>
        <small class="muted">成功率 {{ record.success_rate }}% · 平均耗时 {{ record.average_latency_seconds }} 秒 · 单样本 ¥{{ record.average_cost_yuan.toFixed(4) }} · 单成功样本 {{ record.cost_per_success_yuan === null ? '—' : `¥${record.cost_per_success_yuan.toFixed(4)}` }}</small>
      </article>
    </div>
  </details>
</template>

<style scoped>
.setup-steps {
  display: grid;
  gap: 0.5rem;
  margin: 0;
  padding-left: 1.35rem;
}

.advanced-settings {
  border: 1px solid var(--line, #d9dfeb);
  border-radius: 0.6rem;
  padding: 0.75rem 1rem;
}

.advanced-settings summary {
  cursor: pointer;
  font-weight: 600;
}

.advanced-content {
  margin-top: 1rem;
}
</style>
