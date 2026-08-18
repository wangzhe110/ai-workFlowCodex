<script setup lang="ts">
/**
 * 内部模型测试台。
 *
 * 该页面只选择既有 Profile / Published Prompt 版本并创建冻结实验；没有 Key、
 * Adapter、Base URL、Header、任意路径或任意 JSON 输入入口。创建与开始分成两步，
 * 让操作者在看到预计调用数和公平性检查后再明确确认。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getProjects } from '@/api/projects'
import {
  createModelLabExperiment,
  evaluateModelLabVariant,
  getModelLabCatalog,
  getModelLabExperiments,
  pauseModelLabExperiment,
  preflightExistingModelLabExperiment,
  preflightModelLabExperiment,
  promoteModelLabWinner,
  resumeModelLabExperiment,
  resumeModelLabProviderTask,
  startModelLabExperiment,
} from '@/api/production'
import type { ModelLabCatalog, ModelLabExperiment, ModelLabExperimentCreatePayload, ModelLabPreflight, ModelLabVariantDraft, Project } from '@/types/domain'

type OperationOption = { label: string; operation: string; slot: string; capability: 'text' | 'image' | 'video'; source: ModelLabExperimentCreatePayload['input_source_type']; variable: string }

const operations: OperationOption[] = [
  { label: '原创故事方案', operation: 'V1_STORY_GENERATE', slot: 'STORY_GENERATE', capability: 'text', source: 'text', variable: 'locked_reference_analysis' },
  { label: '带货故事创意', operation: 'COMMERCE_STORY_IDEAS', slot: 'STORY_GENERATE', capability: 'text', source: 'text', variable: 'frozen_input' },
  { label: '角色设定', operation: 'COMMERCE_CHARACTER_DESIGN', slot: 'CHARACTER_DESIGN', capability: 'text', source: 'text', variable: 'commerce_context' },
  { label: '场景设定', operation: 'COMMERCE_SCENE_DESIGN', slot: 'SCENE_DESIGN', capability: 'text', source: 'text', variable: 'commerce_context' },
  { label: 'AI 导演分镜', operation: 'COMMERCE_STORYBOARD', slot: 'DIRECTOR_PLAN', capability: 'text', source: 'text', variable: 'commerce_context' },
  { label: '角色参考图', operation: 'V1_CHARACTER_IMAGE', slot: 'CHARACTER_IMAGE_GENERATE', capability: 'image', source: 'image_prompt', variable: 'image_subject' },
  { label: '场景基础图', operation: 'V1_SCENE_IMAGE', slot: 'SCENE_IMAGE_GENERATE', capability: 'image', source: 'image_prompt', variable: 'image_subject' },
  { label: '关键帧图', operation: 'V1_KEYFRAME_PROMPT_ORGANIZE', slot: 'SHOT_KEYFRAME_GENERATE', capability: 'image', source: 'image_prompt', variable: 'shot' },
  { label: '图生视频', operation: 'V1_VIDEO_PROMPT', slot: 'VIDEO_GENERATE', capability: 'video', source: 'locked_keyframe', variable: 'shot' },
]

const projects = ref<Project[]>([])
const experiments = ref<ModelLabExperiment[]>([])
const catalog = ref<ModelLabCatalog>({ slot_selection_mode: null, active_profiles: [], profiles: [], prompt_versions: [] })
const selectedExperimentId = ref('')
const selectedOperation = ref(operations[0].operation)
const projectId = ref('')
const name = ref('')
const comparisonMode = ref<ModelLabExperimentCreatePayload['comparison_mode']>('MODEL_ONLY')
const repeat = ref(1)
const maxCreateCalls = ref(2)
const inputText = ref('这是一个受控的内部模型测试输入。')
const promptVariableName = ref(operations[0].variable)
const imageAsset = ref({ asset_id: '', sha256: '', mime_type: 'image/png', width: 1024, height: 1024 })
const keyframeAsset = ref({ asset_id: '', sha256: '', mime_type: 'image/png', width: 1024, height: 1024 })
const variants = ref<ModelLabVariantDraft[]>([])
const preflight = ref<ModelLabPreflight | null>(null)
const loading = ref(false)
const submitting = ref(false)
const error = ref('')
const replaceProfileId = ref('')

const operation = computed(() => operations.find((item) => item.operation === selectedOperation.value) || operations[0])
const selectedExperiment = computed(() => experiments.value.find((item) => item.id === selectedExperimentId.value) || null)
const canStart = computed(() => selectedExperiment.value?.status === 'READY' && Boolean(selectedExperiment.value.preflight?.preflight_hash))
const canPause = computed(() => selectedExperiment.value?.status === 'RUNNING')
const canResume = computed(() => selectedExperiment.value?.status === 'PAUSED')
const estimatedCalls = computed(() => variants.value.length * repeat.value)
const fairnessText = computed(() => {
  if (comparisonMode.value === 'MODEL_ONLY') return '公平对比：只允许不同模型 Profile，Prompt 与有效参数必须完全一致。'
  if (comparisonMode.value === 'PROMPT_ONLY') return '公平对比：只允许不同的已发布 Prompt 版本。'
  if (comparisonMode.value === 'PARAMETER_ONLY') return '公平对比：只允许不同质量预设或参数。'
  return '自定义对比：页面会标记多个可能不同的维度，不将结果宣称为严格公平。'
})

function localMedia(value: unknown): string | null {
  if (!value || typeof value !== 'object') return null
  const url = (value as Record<string, unknown>).local_media_url
  return typeof url === 'string' && url.startsWith('/media/generated/') ? url : null
}

function variantDefaults(): ModelLabVariantDraft[] {
  const first = catalog.value.profiles[0]
  const second = catalog.value.profiles[1] || first
  const prompt = catalog.value.prompt_versions[0]
  if (!first || !prompt) return []
  return [
    { label: '候选 A', model_profile_id: first.id, prompt_template_version_id: prompt.id, parameter_preset: 'standard', requested_overrides: {} },
    { label: '候选 B', model_profile_id: second.id, prompt_template_version_id: prompt.id, parameter_preset: 'standard', requested_overrides: {} },
  ]
}

function addVariant() {
  if (variants.value.length >= 4 || !catalog.value.profiles[0] || !catalog.value.prompt_versions[0]) return
  variants.value.push({ label: `候选 ${String.fromCharCode(65 + variants.value.length)}`, model_profile_id: catalog.value.profiles[0].id, prompt_template_version_id: catalog.value.prompt_versions[0].id, parameter_preset: 'standard', requested_overrides: {} })
}

function removeVariant(index: number) {
  if (variants.value.length <= 2) return
  variants.value.splice(index, 1)
}

function inputPayload(): Record<string, unknown> {
  if (operation.value.capability === 'text') return { text: inputText.value }
  if (operation.value.capability === 'image') {
    const references = imageAsset.value.asset_id ? [{ ...imageAsset.value }] : []
    return { prompt: inputText.value, reference_assets: references }
  }
  return { video_prompt: inputText.value, keyframe_asset: { ...keyframeAsset.value } }
}

function payload(): ModelLabExperimentCreatePayload {
  return {
    project_id: projectId.value,
    name: name.value.trim() || `${operation.value.label} 对比实验`,
    description: '内部模型测试台创建的冻结对比实验。',
    operation_key: operation.value.operation,
    model_slot_key: operation.value.slot,
    capability: operation.value.capability,
    comparison_mode: comparisonMode.value,
    input_source_type: operation.value.source,
    input_payload: inputPayload(),
    prompt_variables: { [promptVariableName.value.trim() || operation.value.variable]: inputText.value },
    variants: variants.value.map((item) => ({ ...item, requested_overrides: {} })),
    repeat: repeat.value,
    max_create_calls: maxCreateCalls.value,
  }
}

async function loadCatalog() {
  catalog.value = await getModelLabCatalog(operation.value.operation, operation.value.slot, operation.value.capability)
  replaceProfileId.value = catalog.value.slot_selection_mode === 'MULTI_PARALLEL'
    ? (catalog.value.active_profiles[0]?.id || '')
    : ''
  variants.value = variantDefaults()
  maxCreateCalls.value = Math.max(2, estimatedCalls.value)
  promptVariableName.value = operation.value.variable
  preflight.value = null
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [loadedProjects, loadedExperiments] = await Promise.all([getProjects(), getModelLabExperiments()])
    projects.value = loadedProjects
    experiments.value = loadedExperiments
    projectId.value = projectId.value || loadedProjects[0]?.id || ''
    selectedExperimentId.value = selectedExperimentId.value || loadedExperiments[0]?.id || ''
    await loadCatalog()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '模型测试台加载失败。'
  } finally {
    loading.value = false
  }
}

async function runPreflight() {
  submitting.value = true
  error.value = ''
  try {
    preflight.value = await preflightModelLabExperiment(payload())
    ElMessage.success('预检通过：尚未创建工作流或模型调用。')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '预检失败。'
  } finally { submitting.value = false }
}

async function createExperiment() {
  submitting.value = true
  error.value = ''
  try {
    const created = await createModelLabExperiment(payload())
    const frozenPreflight = await preflightExistingModelLabExperiment(created.id)
    created.preflight = frozenPreflight
    experiments.value = [created, ...experiments.value]
    selectedExperimentId.value = created.id
    preflight.value = frozenPreflight
    ElMessage.success('实验已冻结并完成服务端预检。请核对确切调用数后再显式开始。')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '创建实验失败。'
  } finally { submitting.value = false }
}

async function refreshExperiment(id = selectedExperimentId.value) {
  experiments.value = await getModelLabExperiments()
  selectedExperimentId.value = id
}

async function startExperiment() {
  if (!selectedExperiment.value?.preflight?.preflight_hash) return
  const expected = selectedExperiment.value.preflight.expected_create_call_count
  await ElMessageBox.confirm(`将开始 ${expected} 个冻结候选执行。预检哈希会在服务端再次核对；实验不会自动切换正式生产模型。`, '确认开始模型测试', { confirmButtonText: '确认开始', cancelButtonText: '取消', type: 'warning' })
  submitting.value = true
  try {
    await startModelLabExperiment(selectedExperiment.value.id, expected, selectedExperiment.value.preflight.preflight_hash)
    await refreshExperiment()
    ElMessage.success('已投递实验。请刷新或稍后查看状态。')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '开始实验失败。'
  } finally { submitting.value = false }
}

async function pauseOrResume() {
  if (!selectedExperiment.value) return
  submitting.value = true
  try {
    if (canPause.value) await pauseModelLabExperiment(selectedExperiment.value.id)
    else if (canResume.value) await resumeModelLabExperiment(selectedExperiment.value.id)
    await refreshExperiment()
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '操作失败。' } finally { submitting.value = false }
}

async function scoreVariant(variantId: string, winner: boolean) {
  if (!selectedExperiment.value) return
  const dimensions: Record<string, number> = selectedExperiment.value.capability === 'text'
    ? { instruction_following: 3, structure: 3, story_quality: 3, commerce_integration: 3, executability: 3 }
    : selectedExperiment.value.capability === 'image'
      ? { prompt_alignment: 3, character_consistency: 3, scene_consistency: 3, product_fidelity: 3, visual_quality: 3 }
      : { motion_naturalness: 3, first_frame_consistency: 3, visual_consistency: 3, stability: 3, prompt_alignment: 3 }
  try {
    await evaluateModelLabVariant(selectedExperiment.value.id, variantId, { scores: dimensions, notes: '人工初步评分', is_winner: winner })
    await refreshExperiment()
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '评分保存失败。' }
}

async function recoverVariant(variantId: string) {
  if (!selectedExperiment.value) return
  try {
    await resumeModelLabProviderTask(selectedExperiment.value.id, variantId)
    await refreshExperiment()
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '无法恢复原供应商任务。' }
}

async function promoteVariant(variantId: string) {
  if (!selectedExperiment.value) return
  const oldProfiles = selectedExperiment.value.production_profiles.map((profile) => `${profile.name} · v${profile.version}`).join('、') || '当前槽位尚未绑定 Profile'
  await ElMessageBox.confirm(`将以优胜 Variant 的 Profile 替换：${oldProfiles}。不会自动激活 Prompt 或修改预设。`, '二次确认提升生产版本', { confirmButtonText: '确认提升', cancelButtonText: '取消', type: 'warning' })
  try {
    await promoteModelLabWinner(selectedExperiment.value.id, variantId, { confirmed: true, replace_profile_id: replaceProfileId.value || undefined })
    await refreshExperiment()
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '提升生产版本失败。' }
}

watch(selectedOperation, () => { void loadCatalog() })
watch(selectedExperimentId, () => {
  replaceProfileId.value = selectedExperiment.value?.slot_selection_mode === 'MULTI_PARALLEL'
    ? (selectedExperiment.value.production_profiles[0]?.id || '')
    : ''
})
watch([repeat, () => variants.value.length], () => { maxCreateCalls.value = Math.max(maxCreateCalls.value, estimatedCalls.value) })
onMounted(() => { void load() })
</script>

<template>
  <section class="page model-lab-page">
    <header class="page-header">
      <div>
        <RouterLink to="/">← 返回项目</RouterLink>
        <h1>模型测试台</h1>
        <p>在相同冻结输入下比较模型、已发布 Prompt 和参数。不会自动改动正式生产配置。</p>
      </div>
      <el-tag type="warning" effect="plain">内部实验 · Mock 结果会明确标记</el-tag>
    </header>

    <el-alert v-if="error" type="error" :closable="false" :title="error" class="section-alert" />
    <el-skeleton v-if="loading" :rows="8" animated />

    <template v-else>
      <el-card class="section-card" shadow="never">
        <template #header><div class="card-title">1. 新建冻结实验</div></template>
        <el-form label-position="top" class="experiment-form">
          <div class="form-grid">
            <el-form-item label="项目"><el-select v-model="projectId" placeholder="选择项目"><el-option v-for="item in projects" :key="item.id" :label="item.title" :value="item.id" /></el-select></el-form-item>
            <el-form-item label="业务操作"><el-select v-model="selectedOperation"><el-option v-for="item in operations" :key="item.operation" :label="item.label" :value="item.operation" /></el-select></el-form-item>
            <el-form-item label="实验名称"><el-input v-model="name" placeholder="例如：故事方案模型对比" /></el-form-item>
            <el-form-item label="对比模式"><el-select v-model="comparisonMode"><el-option label="仅比较模型" value="MODEL_ONLY" /><el-option label="仅比较 Prompt" value="PROMPT_ONLY" /><el-option label="仅比较参数" value="PARAMETER_ONLY" /><el-option label="自定义组合" value="CUSTOM" /><el-option label="原生最佳参数" value="NATIVE_PRESET" /></el-select></el-form-item>
          </div>
          <el-alert type="info" :closable="false" :title="fairnessText" />
          <el-form-item :label="operation.capability === 'video' ? '冻结视频 Prompt' : operation.capability === 'image' ? '图片 Prompt' : '受控文本输入'">
            <el-input v-model="inputText" type="textarea" :rows="3" maxlength="20000" show-word-limit />
          </el-form-item>
          <el-form-item label="Prompt 变量名"><el-input v-model="promptVariableName" maxlength="80" /></el-form-item>
          <template v-if="operation.capability === 'image'">
            <p class="hint">参考图只能使用本项目已锁定资产。填写资产页显示的 ID、SHA-256、格式和尺寸；系统会再次校验文件头。留空即为纯文本生图。</p>
            <div class="form-grid"><el-input v-model="imageAsset.asset_id" placeholder="可选：参考资产 ID" /><el-input v-model="imageAsset.sha256" placeholder="可选：SHA-256" /><el-select v-model="imageAsset.mime_type" aria-label="参考图格式"><el-option label="PNG" value="image/png" /><el-option label="JPEG" value="image/jpeg" /><el-option label="WebP" value="image/webp" /></el-select><el-input-number v-model="imageAsset.width" :min="1" :max="8192" aria-label="参考图宽度" /><el-input-number v-model="imageAsset.height" :min="1" :max="8192" aria-label="参考图高度" /></div>
          </template>
          <template v-if="operation.capability === 'video'">
            <p class="hint">视频测试必须使用本项目已锁定关键帧。填写资产页显示的 ID、SHA-256、格式和尺寸；系统会在创建调用前校验文件头。</p>
            <div class="form-grid"><el-input v-model="keyframeAsset.asset_id" placeholder="关键帧资产 ID" /><el-input v-model="keyframeAsset.sha256" placeholder="关键帧 SHA-256" /><el-select v-model="keyframeAsset.mime_type" aria-label="关键帧格式"><el-option label="PNG" value="image/png" /><el-option label="JPEG" value="image/jpeg" /><el-option label="WebP" value="image/webp" /></el-select><el-input-number v-model="keyframeAsset.width" :min="1" :max="8192" aria-label="关键帧宽度" /><el-input-number v-model="keyframeAsset.height" :min="1" :max="8192" aria-label="关键帧高度" /></div>
          </template>

          <div class="section-label">候选 Variant（2–4 个）</div>
          <div v-for="(variant, index) in variants" :key="index" class="variant-editor">
            <el-input v-model="variant.label" aria-label="Variant 名称" />
            <el-select v-model="variant.model_profile_id" placeholder="选择模型 Profile"><el-option v-for="profile in catalog.profiles" :key="profile.id" :label="`${profile.name} · v${profile.version}${profile.is_mock ? '（Mock）' : ''}`" :value="profile.id" /></el-select>
            <el-select v-model="variant.prompt_template_version_id" placeholder="选择已发布 Prompt"><el-option v-for="prompt in catalog.prompt_versions" :key="prompt.id" :label="`${prompt.display_name} · v${prompt.version} · ${prompt.content_hash}`" :value="prompt.id" /></el-select>
            <el-select v-model="variant.parameter_preset"><el-option label="预览" value="preview" /><el-option label="标准" value="standard" /><el-option label="高质量" value="high" /></el-select>
            <el-button :disabled="variants.length <= 2" text type="danger" @click="removeVariant(index)">移除</el-button>
          </div>
          <el-button :disabled="variants.length >= 4" @click="addVariant">增加候选</el-button>
          <div class="form-grid budget-row"><el-form-item label="重复次数"><el-input-number v-model="repeat" :min="1" :max="3" /></el-form-item><el-form-item label="最大创建调用预算"><el-input-number v-model="maxCreateCalls" :min="estimatedCalls" :max="12" /></el-form-item></div>
          <div class="form-actions"><el-button :loading="submitting" @click="runPreflight">预检（不调用模型）</el-button><el-button type="primary" :disabled="!projectId || variants.length < 2" :loading="submitting" @click="createExperiment">冻结并创建实验</el-button></div>
        </el-form>
        <el-descriptions v-if="preflight" :column="2" border class="preflight"><el-descriptions-item label="候选 / 重复">{{ preflight.variant_count }} / {{ preflight.repeat }}</el-descriptions-item><el-descriptions-item label="预计创建调用">{{ preflight.estimated_create_calls }}</el-descriptions-item><el-descriptions-item label="差异维度">{{ preflight.differing_dimensions.join('、') }}</el-descriptions-item><el-descriptions-item label="密钥状态">{{ preflight.key_checks.map((item) => item.key_status).join('、') }}</el-descriptions-item></el-descriptions>
      </el-card>

      <el-card class="section-card" shadow="never">
        <template #header><div class="card-title">2. 执行、结果与人工选择</div></template>
        <el-select v-model="selectedExperimentId" placeholder="选择实验" class="experiment-select"><el-option v-for="item in experiments" :key="item.id" :label="`${item.name} · ${item.status}`" :value="item.id" /></el-select>
        <template v-if="selectedExperiment">
          <div class="run-summary"><el-tag>{{ selectedExperiment.status }}</el-tag><span>冻结输入哈希：{{ selectedExperiment.input_hash.slice(0, 12) }}…</span><span v-if="selectedExperiment.preflight">预检：{{ selectedExperiment.preflight.expected_create_call_count }} 次 · {{ selectedExperiment.preflight.preflight_hash?.slice(0, 12) }}…</span><span>工作流：{{ selectedExperiment.workflow_run_id || '尚未开始' }}</span></div>
          <el-alert v-if="selectedExperiment.production_profiles.length" type="info" :closable="false" class="promotion-hint" title="提升生产版本前会明确替换以下当前正式 Profile">
            <template #default>{{ selectedExperiment.production_profiles.map((profile) => `${profile.name} · v${profile.version}`).join('、') }}</template>
          </el-alert>
          <el-select v-if="selectedExperiment.slot_selection_mode === 'MULTI_PARALLEL' && selectedExperiment.production_profiles.length > 1" v-model="replaceProfileId" class="replace-profile-select" placeholder="选择要替换的当前正式 Profile">
            <el-option v-for="profile in selectedExperiment.production_profiles" :key="profile.id" :label="`${profile.name} · v${profile.version}`" :value="profile.id" />
          </el-select>
          <el-alert v-if="selectedExperiment.status === 'READY' && !selectedExperiment.preflight" type="warning" :closable="false" title="此实验尚无有效预检，请刷新预检后再开始。" />
          <div class="form-actions"><el-button type="primary" :disabled="!canStart" :loading="submitting" @click="startExperiment">确认开始（{{ selectedExperiment.preflight?.expected_create_call_count || 0 }} 次）</el-button><el-button v-if="canPause || canResume" :loading="submitting" @click="pauseOrResume">{{ canPause ? '暂停未开始候选' : '继续未开始候选' }}</el-button><el-button @click="refreshExperiment">刷新状态</el-button></div>
          <div class="result-grid">
            <el-card v-for="variant in selectedExperiment.variants" :key="variant.id" shadow="never" class="result-card">
              <template #header><div class="result-title"><span>{{ variant.label }} · 第 {{ variant.repeat_index }} 次</span><el-tag :type="variant.status === 'SUCCEEDED' ? 'success' : variant.status === 'FAILED' ? 'danger' : 'info'">{{ variant.status }}</el-tag></div></template>
              <p><strong>Profile：</strong>{{ variant.profile_id }} · v{{ variant.profile_version }} <el-tag v-if="variant.is_mock" type="warning" size="small">Mock</el-tag></p>
              <p><strong>Prompt：</strong>v{{ variant.prompt_version }} · {{ variant.prompt_hash }}</p>
              <p><strong>参数：</strong>{{ variant.parameter_preset }}</p>
              <p v-if="variant.error_code" class="error-text">{{ variant.error_code }}：{{ variant.sanitized_error_summary }}</p>
              <img v-if="selectedExperiment.capability === 'image' && localMedia(variant.output_reference)" :src="localMedia(variant.output_reference) || undefined" alt="模型测试图片结果" class="media-preview" />
              <video v-if="selectedExperiment.capability === 'video' && localMedia(variant.output_reference)" :src="localMedia(variant.output_reference) || undefined" controls class="media-preview" />
              <pre v-if="selectedExperiment.capability === 'text' && variant.output_reference">{{ variant.output_reference }}</pre>
              <p v-if="variant.provider_task_id_short">供应商任务：{{ variant.provider_task_id_short }}</p>
              <div class="card-actions"><el-button :disabled="variant.status !== 'SUCCEEDED'" @click="scoreVariant(variant.id, false)">保存评分</el-button><el-button type="success" :disabled="variant.status !== 'SUCCEEDED'" @click="scoreVariant(variant.id, true)">选为优胜</el-button><el-button v-if="variant.status === 'FAILED' && variant.provider_task_id_short" @click="recoverVariant(variant.id)">恢复已有任务</el-button><el-button v-if="selectedExperiment.winner_variant_id === variant.id && variant.status === 'SUCCEEDED' && !variant.is_mock" type="warning" @click="promoteVariant(variant.id)">提升生产 Profile</el-button><el-tag v-else-if="selectedExperiment.winner_variant_id === variant.id && variant.is_mock" type="warning">Mock 结果不可提升生产</el-tag></div>
            </el-card>
          </div>
        </template>
        <el-empty v-else description="先创建一个冻结实验。" />
      </el-card>
    </template>
  </section>
</template>

<style scoped>
.page-header { display: flex; justify-content: space-between; gap: 24px; align-items: flex-start; margin-bottom: 20px; }
.page-header h1 { margin: 8px 0; color: #1f2a44; }
.page-header p, .hint { color: #667085; }
.section-card { margin-bottom: 20px; }
.card-title { font-weight: 700; color: #1f2a44; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.variant-editor { display: grid; grid-template-columns: 1fr 1.4fr 1.4fr 0.8fr auto; gap: 8px; align-items: center; margin: 10px 0; }
.section-label { margin: 18px 0 8px; font-weight: 600; }
.form-actions, .card-actions, .run-summary { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 16px; }
.budget-row { margin-top: 16px; }
.preflight { margin-top: 18px; }
.promotion-hint { margin-top: 12px; }
.replace-profile-select { width: min(520px, 100%); margin-top: 12px; }
.experiment-select { width: min(620px, 100%); margin-bottom: 14px; }
.result-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 14px; margin-top: 18px; }
.result-title { display: flex; justify-content: space-between; gap: 8px; align-items: center; }
.result-card p { word-break: break-word; }
.media-preview { display: block; width: 100%; max-height: 260px; object-fit: contain; background: #f5f7fa; border-radius: 6px; }
.error-text { color: #c45656; }
pre { max-height: 200px; overflow: auto; white-space: pre-wrap; background: #f7f8fa; padding: 10px; border-radius: 6px; }
@media (max-width: 900px) { .form-grid, .variant-editor { grid-template-columns: 1fr; } .page-header { flex-direction: column; } }
</style>
