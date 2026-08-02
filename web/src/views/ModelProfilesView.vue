<script setup lang="ts">
/** 模型配置中心：管理每个步骤的候选配置，不接收真实 API Key。 */
import { computed, onMounted, ref, watch } from 'vue'
import ModelEvaluationPanel from '@/components/ModelEvaluationPanel.vue'
import { useModelProfilesStore } from '@/stores/model-profiles'

const store = useModelProfilesStore()
const stepKey = ref('generate_story_package')
const providerKey = ref('openai_compatible')
const modelKey = ref('')
const displayName = ref('云雾 OpenAI 兼容 API')
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
const videoSubmitPath = ref('/luma/generations')
const videoQueryPath = ref('/luma/generations/{task_id}')
const videoPromptField = ref('user_prompt')
const videoImageInputMode = ref<'top_level_url' | 'luma_keyframe'>('top_level_url')
const videoModelField = ref('')
const videoEndImageField = ref('')
const videoRequestOptionsText = ref('{}')
const videoResponseMappingText = ref('{\n  "task_id_path": "id",\n  "state_path": "state",\n  "video_url_paths": ["video.url", "artifact.video.url"]\n}')
const pollIntervalSeconds = ref(4)
const maxPollSeconds = ref(900)
const activate = ref(false)
const comparisonStepKey = ref('generate_story_package')

const steps = [
  ['transcribe_reference_audio', '参考视频语音转写'],
  ['analyze_reference_mechanisms', '参考视频分析'],
  ['generate_original_topics', '原创选题'],
  ['generate_story_package', '故事包'],
  ['generate_storyboard', '分镜细纲'],
  ['generate_storyboard_images', '分镜图片'],
  ['generate_storyboard_video_groups', '视频片段'],
  ['assemble_final_video', '完整成片导出'],
] as const
const stepLabels: Record<string, string> = Object.fromEntries(steps)
const stepName = computed(() => stepLabels[stepKey.value] || stepKey.value)
const isImageStep = computed(() => stepKey.value === 'generate_storyboard_images')
const isVideoStep = computed(() => stepKey.value === 'generate_storyboard_video_groups')
const isVisionStep = computed(() => stepKey.value === 'analyze_reference_mechanisms')
const isTranscriptionStep = computed(() => stepKey.value === 'transcribe_reference_audio')
const isFinalVideoStep = computed(() => stepKey.value === 'assemble_final_video')

onMounted(() => void store.load())
watch(stepKey, (nextStep) => {
  if (nextStep === 'transcribe_reference_audio') {
    providerKey.value = 'openai_compatible_transcription'
    displayName.value = 'OpenAI 兼容语音转写'
    apiBaseUrl.value = 'https://yunwu.ai/v1'
    return
  }
  if (nextStep === 'assemble_final_video') {
    providerKey.value = 'ffmpeg_concat'
    modelKey.value = 'ffmpeg-concat-v1'
    displayName.value = 'FFmpeg 完整成片合成'
    apiBaseUrl.value = ''
    secretEnvName.value = ''
    return
  }
  if (nextStep === 'analyze_reference_mechanisms') {
    providerKey.value = 'openai_compatible_vision'
    displayName.value = 'OpenAI 兼容视觉视频分析'
    apiBaseUrl.value = 'https://yunwu.ai/v1'
    return
  }
  if (nextStep === 'generate_storyboard_video_groups') {
    providerKey.value = 'configurable_async_video'
    displayName.value = '异步图生视频 API'
    apiBaseUrl.value = 'https://yunwu.ai'
    return
  }
  if (nextStep === 'generate_storyboard_images') {
    providerKey.value = 'openai_compatible_image'
    apiBaseUrl.value = 'https://yunwu.ai/v1'
    return
  }
  providerKey.value = 'openai_compatible'
  displayName.value = '云雾 OpenAI 兼容 API'
  apiBaseUrl.value = 'https://yunwu.ai/v1'
})

/** 仅组装非敏感配置；真实密钥只能在服务器环境变量中设置。 */
function providerConfig(): Record<string, unknown> {
  const parseObject = (value: string, label: string): Record<string, unknown> => {
    try {
      const parsed: unknown = JSON.parse(value || '{}')
      if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('not-object')
      return parsed as Record<string, unknown>
    } catch {
      throw new Error(`${label}必须是合法的 JSON 对象，例如 {"duration":"5s"}`)
    }
  }
  let videoRequestOptions: Record<string, unknown> = {}
  let videoResponseMapping: Record<string, unknown> = {}
  let visionRequestOptions: Record<string, unknown> = {}
  let transcriptionRequestOptions: Record<string, unknown> = {}
  if (isVideoStep.value) {
    videoRequestOptions = parseObject(videoRequestOptionsText.value, '视频固定请求参数')
    videoResponseMapping = parseObject(videoResponseMappingText.value, '视频响应映射')
  }
  if (isVisionStep.value) visionRequestOptions = parseObject(visionRequestOptionsText.value, '视觉模型扩展参数')
  if (isTranscriptionStep.value) transcriptionRequestOptions = parseObject(transcriptionRequestOptionsText.value, '语音转写扩展参数')
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
      submit_path: videoSubmitPath.value.trim(),
      query_path_template: videoQueryPath.value.trim(),
      prompt_field: videoPromptField.value.trim(),
      image_input_mode: videoImageInputMode.value,
      ...(videoModelField.value.trim() ? { model_field: videoModelField.value.trim() } : {}),
      ...(videoEndImageField.value.trim() ? { end_image_field: videoEndImageField.value.trim() } : {}),
      video_request_options: videoRequestOptions,
      ...videoResponseMapping,
      poll_interval_seconds: pollIntervalSeconds.value,
      max_poll_seconds: maxPollSeconds.value,
    } : {}),
  }
}

async function submit() {
  if (!providerKey.value.trim() || !modelKey.value.trim()) {
    store.error = '请填写供应商标识和模型标识'
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
    modelKey.value = ''; timeoutSeconds.value = null; activate.value = false
  }
}
</script>

<template>
  <section class="page-heading">
    <div>
      <RouterLink class="muted" to="/">← 返回项目</RouterLink>
      <h1>模型配置中心</h1>
      <p>每个生产步骤独立配置和版本化；真实 API Key 永远不填写在这里。</p>
    </div>
  </section>

  <section class="panel stack">
    <div class="meta-row"><h2>模型实测对比</h2><span>仅比较相同测试场景</span></div>
    <label class="field">工作流步骤
      <select v-model="comparisonStepKey">
        <option v-for="[key, label] in steps" :key="key" :value="key">{{ label }}</option>
      </select>
    </label>
    <button class="button secondary" :disabled="store.comparisonLoading" @click="store.loadComparisons(comparisonStepKey)">
      {{ store.comparisonLoading ? '读取中…' : '查看此步骤实测对比' }}
    </button>
    <p v-if="store.comparisons.length === 0 && !store.comparisonLoading" class="muted">选择步骤后查看已保存的实测记录；不同测试场景会并列显示，不能直接混合比较。</p>
    <article v-for="record in store.comparisons" :key="record.id" class="panel stack">
      <div class="meta-row">
        <strong>{{ record.display_name || record.model_key }} · v{{ record.profile_version }}</strong>
        <span>质量 {{ record.quality_score }}/100</span>
      </div>
      <p>{{ record.scenario }}</p>
      <small class="muted">
        {{ record.provider_key }} / {{ record.model_key }} · 成功率 {{ record.success_rate }}% ·
        平均耗时 {{ record.average_latency_seconds }} 秒 · 单样本 ¥{{ record.average_cost_yuan.toFixed(4) }} ·
        单成功样本 {{ record.cost_per_success_yuan === null ? '—' : `¥${record.cost_per_success_yuan.toFixed(4)}` }}
      </small>
    </article>
  </section>

  <div class="grid">
    <form class="panel stack" @submit.prevent="submit">
      <h2>新增候选配置</h2>
      <label class="field">工作流步骤<select v-model="stepKey"><option v-for="[key, label] in steps" :key="key" :value="key">{{ label }}（{{ key }}）</option></select></label>
      <label class="field">供应商标识<input v-model="providerKey" placeholder="openai_compatible" maxlength="80" /></label>
      <label class="field">模型标识<input v-model="modelKey" placeholder="例如 video-model-2026" maxlength="160" /></label>
      <label class="field">显示名称（可选）<input v-model="displayName" placeholder="例如 云雾视频模型" /></label>
      <label class="field">API 地址（可选）<input v-model="apiBaseUrl" type="url" placeholder="https://…" /></label>
      <label class="field">密钥环境变量名（可选）<input v-model="secretEnvName" placeholder="例如 YUNWU_API_KEY" /></label>
      <label class="field">超时秒数（可选）<input v-model.number="timeoutSeconds" type="number" min="1" max="1800" /></label>
      <label v-if="isImageStep" class="field">图片尺寸（可选）<input v-model="imageSize" placeholder="例如 1728x2304" /></label>
      <template v-if="isVisionStep">
        <label class="field">抽帧数量<input v-model.number="frameSampleCount" type="number" min="1" max="12" /></label>
        <label class="field">单帧提取超时（秒）<input v-model.number="frameExtractionTimeoutSeconds" type="number" min="5" max="300" /></label>
        <label class="field">单帧请求体上限（字节）<input v-model.number="frameMaxBytes" type="number" min="65536" :max="8 * 1024 * 1024" /></label>
        <label class="field">视觉模型扩展参数（JSON）<textarea v-model="visionRequestOptionsText" rows="4" placeholder='例如 {"top_p":0.9}' /></label>
      </template>
      <template v-if="isTranscriptionStep">
        <label class="field">分析开头音频上限（秒）<input v-model.number="audioMaxDurationSeconds" type="number" min="5" max="600" /></label>
        <label class="field">音轨提取超时（秒）<input v-model.number="audioExtractionTimeoutSeconds" type="number" min="5" max="300" /></label>
        <label class="field">音频请求体上限（字节）<input v-model.number="audioMaxBytes" type="number" min="65536" :max="50 * 1024 * 1024" /></label>
        <label class="field">语音转写扩展参数（JSON）<textarea v-model="transcriptionRequestOptionsText" rows="4" placeholder='例如 {"language":"zh"}' /></label>
      </template>
      <template v-if="isFinalVideoStep">
        <label class="field">单片段下载超时（秒）<input v-model.number="finalVideoDownloadTimeoutSeconds" type="number" min="5" max="600" /></label>
        <label class="field">单片段体积上限（字节）<input v-model.number="finalVideoMaxClipBytes" type="number" min="1048576" :max="2 * 1024 * 1024 * 1024" /></label>
        <label class="field">完整成片体积上限（字节）<input v-model.number="finalVideoMaxOutputBytes" type="number" min="1048576" :max="10 * 1024 * 1024 * 1024" /></label>
        <label class="field">FFmpeg 合成超时（秒）<input v-model.number="finalVideoRenderTimeoutSeconds" type="number" min="30" max="7200" /></label>
      </template>
      <template v-if="isVideoStep">
        <label class="field">提交路径<input v-model="videoSubmitPath" placeholder="例如 /luma/generations" /></label>
        <label class="field">查询路径模板<input v-model="videoQueryPath" placeholder="例如 /luma/generations/{task_id}" /></label>
        <label class="field">提示词字段<input v-model="videoPromptField" placeholder="例如 user_prompt 或 prompt" /></label>
        <label class="field">首帧请求格式<select v-model="videoImageInputMode"><option value="top_level_url">顶层图片地址（image_url 等）</option><option value="luma_keyframe">keyframes.frame0 格式</option></select></label>
        <label class="field">模型字段（可选）<input v-model="videoModelField" placeholder="例如 model 或 model_name；不需要则留空" /></label>
        <label class="field">结束帧字段（可选）<input v-model="videoEndImageField" placeholder="例如 image_end_url；不支持则留空" /></label>
        <label class="field">固定请求参数（JSON）<textarea v-model="videoRequestOptionsText" rows="4" placeholder='例如 {"duration":"5s","aspect_ratio":"9:16"}' /></label>
        <label class="field">响应映射（JSON）<textarea v-model="videoResponseMappingText" rows="6" placeholder='例如 {"task_id_path":"id","state_path":"state","video_url_paths":["video.url"]}' /></label>
        <label class="field">轮询间隔（秒）<input v-model.number="pollIntervalSeconds" type="number" min="1" max="60" /></label>
        <label class="field">最长轮询（秒）<input v-model.number="maxPollSeconds" type="number" min="10" max="1800" /></label>
      </template>
      <label class="field"><span><input v-model="activate" type="checkbox" /> 创建后立即启用（仅已接入适配器可用）</span></label>
      <p v-if="isImageStep" class="notice info">分镜图片使用 <code>openai_compatible_image</code>，默认云雾地址与 <code>YUNWU_API_KEY</code>。图片模型名、尺寸和是否支持水印必须以云雾后台实际能力为准。</p>
      <p v-else-if="isFinalVideoStep" class="notice info">完整成片使用 <code>ffmpeg_concat</code>：Worker 下载按顺序冻结的 HTTPS 视频片段，用 FFmpeg 合成为 MP4，并保留成片版本。此步骤不需要 API 地址或密钥；生产环境需要 FFmpeg 和可持久化的媒体存储。</p>
      <p v-else-if="isTranscriptionStep" class="notice info">语音转写使用 <code>openai_compatible_transcription</code>：后端只提取开头有限时长的音轨，转写文本只在本次 Worker 内存中用于综合分析，不写入数据库或返回浏览器。请确认模型支持标准 <code>/v1/audio/transcriptions</code> JSON 响应。</p>
      <p v-else-if="isVisionStep" class="notice info">参考视频分析使用 <code>openai_compatible_vision</code>：后端 Worker 用 FFmpeg 均匀抽帧并发送给视觉模型，只保存抽象开头机制、冲突与节奏。请确认模型支持图片输入和 JSON 输出；不会把原视频帧保存到数据库或返回浏览器。</p>
      <p v-else-if="isVideoStep" class="notice info">视频使用 <code>configurable_async_video</code>：先提交任务、再按任务号轮询。请依据当前模型的云雾/API 文档确认路径、提示词字段、首帧格式和固定参数；真实运行要求分镜图片为供应商能访问的 HTTPS 地址。</p>
      <p v-else class="notice info">选题、故事、分镜已支持 <code>openai_compatible</code>。云雾可使用默认地址与 <code>YUNWU_API_KEY</code>；密钥只写服务器 .env，模型名以云雾后台实际列表为准。</p>
      <p v-if="store.error" class="notice error">{{ store.error }}</p>
      <button class="button" :disabled="store.submitting">{{ store.submitting ? '保存中…' : `新增「${stepName}」配置` }}</button>
    </form>

    <section class="panel stack">
      <div class="meta-row"><h2>配置版本</h2><span>{{ store.profiles.length }} 项</span></div>
      <p v-if="store.loading" class="muted">正在加载…</p>
      <article v-for="profile in store.profiles" :key="profile.id" class="panel stack">
        <div class="meta-row">
          <strong>{{ stepLabels[profile.step_key] || profile.step_key }} · v{{ profile.version }}</strong>
          <span class="status" :class="profile.is_active ? 'SUCCEEDED' : 'PENDING'">{{ profile.is_active ? '当前启用' : '候选配置' }}</span>
        </div>
        <p>{{ profile.provider_key }} / {{ profile.model_key }}</p>
        <small class="muted">{{ profile.adapter_available ? '适配器已接通' : '适配器尚未接通' }} · {{ profile.provider_config.display_name || '未命名' }}</small>
        <button class="button secondary" :disabled="store.preflightingProfileId === profile.id" @click="store.preflight(profile.id)">
          {{ store.preflightingProfileId === profile.id ? '预检中…' : '基础预检（不生成内容）' }}
        </button>
        <div v-if="store.preflights[profile.id]" class="stack">
          <p class="notice" :class="store.preflights[profile.id].ready ? 'info' : 'error'">
            {{ store.preflights[profile.id].ready ? '基础预检通过，可按需启用或进行小样本任务验收。' : '基础预检未通过，请先按下方提示修正。' }}
          </p>
          <ul class="muted">
            <li v-for="check in store.preflights[profile.id].checks" :key="check.key">
              {{ check.status === 'passed' ? '通过' : check.status === 'warning' ? '提示' : '未通过' }}：{{ check.message }}
            </li>
          </ul>
        </div>
        <ModelEvaluationPanel
          :profile-id="profile.id"
          :profile-label="`${stepLabels[profile.step_key] || profile.step_key} v${profile.version}`"
          :evaluations="store.evaluations[profile.id] || []"
          :loading="store.evaluationLoadingProfileId === profile.id"
          :saving="store.evaluationSavingProfileId === profile.id"
          :load-evaluations="(profileId) => store.loadEvaluations(profileId)"
          :save-evaluation="(profileId, payload) => store.createEvaluation(profileId, payload)"
        />
        <button v-if="!profile.is_active" class="button secondary" :disabled="!profile.adapter_available || store.submitting" @click="store.activate(profile.id)">启用此版本</button>
      </article>
    </section>
  </div>
</template>
